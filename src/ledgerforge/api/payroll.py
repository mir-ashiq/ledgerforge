"""The payroll depth API (ADR-029, roadmap Phase 8): the attendance
register import, its receipts and the live register feed.

Roles follow the codebase's rule: reads need the reader role, a register
import is a bookkeeper write. Every route is book-scoped through the
standard header. The register is the operator's own record (not a portal
download), but the discipline is the bank-statement one: the kernel
validates all-or-nothing and imports idempotently by revision-carrying
content fingerprint, and the after-commit event
(``payroll.attendance.imported``) fires through the plugin registry —
isolated, never breaking the request it describes.

The payroll meaning of a day count (payable days, per-day rates) is the
payroll pack's fold — the live register reads through the pack's
``payroll-attendance`` report (``GET /api/v1/reports/pack/payroll-attendance``);
this router owns the facts, that fold owns the meaning. Loans, recoveries
and settlements are payroll **documents** — they post through
``POST /api/v1/vouchers`` with ``meta.payroll`` (ADR-012's grammar), not
through this router.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from .. import attendance, exporting
from . import BookDep, BookkeeperDep, ReaderDep, SessionDep

router = APIRouter(prefix="/payroll", tags=["payroll"])


def _emit(request: Request, event: str, payload: dict) -> None:
    request.app.state.plugin_manager.emit(event, payload)


class AttendanceImport(BaseModel):
    period: str = Field(description="the work month the register covers (YYYY-MM, strict)")
    entries: list[dict] = Field(
        description=(
            "one row per employee: {employee, present?, absent?, half?, "
            "leave_paid?, note?} — counts default to 0, a half-day is 0.5"
        )
    )
    source: str | None = Field(
        default=None, max_length=200, description="a label for the receipt (who keyed it)"
    )
    dry_run: bool = Field(default=False, description="validate and preview — nothing is written")


@router.post("/attendance", status_code=201)
def post_attendance(
    payload: AttendanceImport,
    request: Request,
    session: SessionDep,
    book: BookDep,
    principal: BookkeeperDep,
):
    """Import one period's attendance register: validated, all-or-nothing,
    idempotent by content fingerprint (an unchanged re-entry is a no-op
    reporting ``new: 0``). ``dry_run`` is the preview. The receipt is the
    response and the ``payroll.attendance.imported`` payload."""
    receipt = attendance.import_register(
        session,
        book=book,
        payload=payload.model_dump(),
        source=payload.source,
        dry_run=payload.dry_run,
    )
    if not payload.dry_run and receipt.get("new", 0) > 0:
        _emit(request, "payroll.attendance.imported", {"book_id": book.id, **receipt})
    return receipt


@router.get("/attendance")
def get_attendance(
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    period: str = Query(description="the work month (YYYY-MM)"),
):
    """The period's **live** register — the latest batch's rows, raw day
    counts, payable days deliberately absent (the pack computes those).
    Superseded batches stay as history (see /attendance/history) but reads
    never see them."""
    payload = {
        "period": period,
        "entries": [
            {
                "employee": row.employee,
                "present": str(row.present),
                "absent": str(row.absent),
                "half": str(row.half),
                "leave_paid": str(row.leave_paid),
                "note": row.note,
            }
            for row in attendance.live_register(session, book=book, period=period)
        ],
    }
    fmt = exporting.export_format(request)
    exported = (
        exporting.export_response(
            fmt,
            columns=["employee", "present", "absent", "half", "leave_paid", "note"],
            rows=exporting.model_rows(
                payload["entries"],
                ["employee", "present", "absent", "half", "leave_paid", "note"],
            ),
            stem=f"attendance-{period}",
        )
        if fmt is not None
        else None
    )
    return exported if exported is not None else payload


@router.get("/attendance/history")
def get_attendance_history(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    period: str = Query(description="the work month (YYYY-MM)"),
):
    """Every register receipt for the period, newest first — the honest
    provenance of every day count the live register shows."""
    return {
        "period": period,
        "batches": attendance.register_history(session, book=book, period=period),
    }
