"""Reports API: Trial Balance, Profit & Loss, Balance Sheet, FY close,
pack-registered reports (ADR-014)."""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from ..errors import NotFoundError
from ..plugins import Resolver
from ..reporting import balance_sheet, fy_close, group_rollups, profit_loss, trial_balance
from ..reporting.printing import render_view
from . import BookDep, ReaderDep, SessionDep

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/trial-balance")
def get_trial_balance(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    as_of: Annotated[date | None, Query()] = None,
):
    as_of = as_of or date.today()
    response = trial_balance(session, book.id, as_of)
    response["groups"] = group_rollups(session, book.id, as_of)
    return response


@router.get("/profit-loss")
def get_profit_loss(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
):
    return profit_loss(session, book.id, date_from, date_to)


@router.get("/balance-sheet")
def get_balance_sheet(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    as_of: Annotated[date | None, Query()] = None,
):
    as_of = as_of or date.today()
    return balance_sheet(session, book.id, as_of)


@router.get("/fy-close")
def get_fy_close(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    as_of: Annotated[date, Query(description="sweep as of this date (a period's last day)")],
):
    """The financial-year close preview (issue #5): the exact lines that zero
    every income/expense ledger as of ``as_of`` and move the net result to
    Profit & Loss A/c. ``POST /api/v1/periods/{id}/close`` posts them."""
    return fy_close(session, book.id, as_of)


def _safe_stem(name: str) -> str:
    """A pack's filename hint, made download-safe (printing.py's rule)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned[:80] or "report"


@router.get("/pack/{key}")
def pack_report(
    key: str,
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
):
    """A pack-registered report (ADR-014, plugin API 0.6) — the statutory
    bridge serves ``pf`` and ``esi`` here. PDF through the shared
    print-bridge layout by default; ``?format=csv`` for the pack's machine
    format. The kernel stays report-blind: the pack validates its own
    parameters (caller errors surface as 422). Reader role: reports read."""
    registry = request.app.state.plugin_manager.registry
    factory = registry.report_views.get(key)
    if factory is None:
        known = ", ".join(sorted(registry.report_views)) or "none registered"
        raise NotFoundError(f"No pack report registered for {key!r} (known: {known})")
    report = factory(Resolver(session, book), dict(request.query_params))
    stem = _safe_stem(report.filename)
    if report.view is not None:
        return Response(
            content=render_view(report.view),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{stem}.pdf"'},
        )
    return Response(
        content=report.csv or "",
        media_type=report.media_type,
        headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'},
    )
