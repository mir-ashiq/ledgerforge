"""The attendance kernel seam (ADR-029, roadmap Phase 8): shop-floor day
counts — storage, idempotent import and the live-register fold the payroll
pack's per-day pay heads read.

The ADR-026 argument restated for the shop floor: present, absent,
half-day and leave-with-pay counts per employee per period are
**observations about the world**, not double-entry movements — there is no
honest Dr/Cr pair for "Asha was present 22 days" — and the books must
remember them exactly so per-day pay heads can derive from them. They need
database-enforced identity (re-entering the same register is a no-op), a
correction path that never edits a fact a payroll run already derived pay
from, and book scoping (ADR-009). ``vouchers.meta`` cannot promise any of
that — a voucher must balance, and Law 1 forbids inventing lines to dress
an observation as accounting — so migration ``0011_payroll_depth`` lands
the banking pack's exact shape: a batch (the register receipt) and lines
(the day counts), INSERT-only and trigger-guarded append-only.

The kernel's boundary is the one banking and the GST comparison drew:
**storage and fold, never interpretation**. Payable days (``present +
half × 0.5 + leave_paid``) are the payroll pack's arithmetic — the kernel
hands over raw counts and decides nothing about what a day means
(``Resolver.attendance_lines``, plugin API 0.14). The payroll run is the
only consumer that turns days into money; this module never sees a rate.

Revisions are honest, not edits (the 2B discipline): a corrected register
imports as a new batch beside the old (append-only history, Law 2) and
``live_register`` reads the period's **latest** batch only. An unchanged
re-entry reproduces every fingerprint — a clean no-op, no batch — while a
changed one re-states the whole register into the new batch (a partial
re-statement would drop the carried-forward employees from the live
register, so whole-or-nothing is correctness, not convenience). A day
count a run already derived pay from never changes underneath anyone; the
register moves exactly when the shop floor moved it.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import ValidationError
from .models import AttendanceBatch, AttendanceLine, Book

logger = logging.getLogger("ledgerforge.attendance")

_PERIOD_RE = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])$")


def _days_in_period(period: str) -> int:
    """The period's calendar days — the honesty ceiling every day count
    sum is checked against (nobody worked 32 days of October)."""
    match = _PERIOD_RE.fullmatch(period)
    if match is None:
        raise ValidationError(f"attendance: period must be YYYY-MM (e.g. 2026-10), got {period!r}")
    return calendar.monthrange(int(match[1]), int(match[2]))[1]


def _count(value: object, field_name: str) -> Decimal:
    """A day count: a finite non-negative number with at most one decimal
    (0.5 is the only fraction the shop floor uses)."""
    raw = value if value is not None else 0
    try:
        count = Decimal(str(raw))
    except (ArithmeticError, InvalidOperation):
        raise ValidationError(f"attendance: {field_name} must be a number, got {value!r}") from None
    if not count.is_finite() or count < 0:
        raise ValidationError(f"attendance: {field_name} must be a finite count >= 0")
    return count


@dataclass(frozen=True)
class AttendanceEntry:
    """One employee's day counts — the parsed shape before identity is
    assigned. Raw counts only; payable days are the pack's arithmetic."""

    employee: str
    present: Decimal
    absent: Decimal
    half: Decimal
    leave_paid: Decimal
    note: str | None


def parse_register(payload: dict) -> tuple[str, list[AttendanceEntry]]:
    """Parse + validate one register payload: ``{period, entries: [{employee,
    present, absent, half, leave_paid, note?}]}``. The counts default to 0;
    the sums must fit the period's calendar days (the honesty guard) and an
    employee may appear once."""
    if not isinstance(payload, dict):
        raise ValidationError("attendance: the register must be an object")

    period = str(payload.get("period") or "").strip()
    days_in_month = _days_in_period(period)

    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValidationError("attendance: entries must be a non-empty list")

    parsed: list[AttendanceEntry] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValidationError(f"attendance: entries[{i}] must be an object")
        employee = str(entry.get("employee") or "").strip()
        if not employee:
            raise ValidationError(f"attendance: entries[{i}].employee is required")
        if employee in seen:
            raise ValidationError(
                f"attendance: {employee!r} appears twice in this register "
                "(one row per employee — merge the counts)"
            )
        seen.add(employee)
        present = _count(entry.get("present", 0), f"{employee} present")
        absent = _count(entry.get("absent", 0), f"{employee} absent")
        half = _count(entry.get("half", 0), f"{employee} half")
        leave_paid = _count(entry.get("leave_paid", 0), f"{employee} leave_paid")
        # A half-day occupies one calendar day (split 50/50 paid/unpaid);
        # a leave-with-pay day occupies one — the honesty ceiling is the
        # calendar, not a policy.
        occupied = present + absent + half + leave_paid
        if occupied > Decimal(days_in_month):
            raise ValidationError(
                f"attendance: {employee!r}'s counts occupy {occupied} days of "
                f"{period} — a {days_in_month}-day month (present {present} + "
                f"absent {absent} + half {half} + leave {leave_paid})"
            )
        note = str(entry.get("note") or "").strip() or None
        parsed.append(
            AttendanceEntry(
                employee=employee,
                present=present,
                absent=absent,
                half=half,
                leave_paid=leave_paid,
                note=note,
            )
        )
    return period, parsed


def _fingerprint(period: str, entry: AttendanceEntry, occurrence: int, revision: int) -> str:
    """The content identity of one register row: the period, the employee
    (with occurrence — a duplicate name is already rejected, but the shape
    matches the portal rows), the raw counts and the register's revision.
    Revision-carrying is what makes idempotency price the LATEST register:
    an unchanged re-entry reproduces every fingerprint (a no-op), a
    corrected one re-states whole into the new revision."""
    blob = json.dumps(
        {
            "period": period,
            "employee": entry.employee,
            "occurrence": occurrence,
            "present": str(entry.present),
            "absent": str(entry.absent),
            "half": str(entry.half),
            "leave_paid": str(entry.leave_paid),
            "note": entry.note,
            "revision": revision,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def import_register(
    session: Session,
    *,
    book: Book,
    payload: dict,
    source: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Validate and (unless ``dry_run``) insert one attendance register —
    all-or-nothing, idempotent by revision-carrying fingerprint. Returns
    the receipt the UIs show and the ``payroll.attendance.imported`` event
    carries: the period, the parse counts, the revision, and the batch id
    (absent when the register was an unchanged re-entry — the receipt IS
    the answer)."""
    period, entries = parse_register(payload)

    # The register revision: this period's snapshot number — 1 for the
    # first, one more per changed re-entry. The fold reads the latest
    # batch only, so the live register moves exactly when the register
    # content moved.
    prior_batches = (
        session.execute(
            select(AttendanceBatch.id).where(
                AttendanceBatch.book_id == book.id,
                AttendanceBatch.period == period,
            )
        )
        .scalars()
        .all()
    )
    revision = len(prior_batches) + 1
    # Idempotency prices the LATEST register: unchanged rows carry the
    # newest batch's revision in their fingerprints — a re-entry of what
    # the book already holds is a no-op, while any row the latest register
    # states differently lands whole in the new batch.
    latest_revision = len(prior_batches)

    seen: dict[str, int] = {}
    new_lines: list[AttendanceLine] = []
    duplicates = 0
    for entry in entries:
        occurrence = seen.get(entry.employee, 0)
        seen[entry.employee] = occurrence + 1
        already = None
        if latest_revision:
            already = session.execute(
                select(AttendanceLine.id).where(
                    AttendanceLine.book_id == book.id,
                    AttendanceLine.fingerprint
                    == _fingerprint(period, entry, occurrence, latest_revision),
                )
            ).scalar_one_or_none()
        if already is not None:
            duplicates += 1
            continue
        new_lines.append(
            AttendanceLine(
                book_id=book.id,
                batch_id=0,  # filled after the batch row flushes
                employee=entry.employee,
                days_present=entry.present,
                days_absent=entry.absent,
                days_half=entry.half,
                days_leave_paid=entry.leave_paid,
                note=entry.note,
                fingerprint=_fingerprint(period, entry, occurrence, revision),
            )
        )

    label = source or str(payload.get("source") or "").strip() or None
    receipt = {
        "period": period,
        "source": label,
        "parsed": len(entries),
        "new": len(new_lines),
        "duplicates": duplicates,
        "revision": revision,
        "dry_run": dry_run,
    }
    if dry_run or not new_lines:
        return receipt  # an unchanged re-entry imports no batch — the receipt IS the answer

    batch = AttendanceBatch(
        book_id=book.id,
        period=period,
        source=label,
        line_count=len(new_lines),
    )
    session.add(batch)
    session.flush()  # batch.id for the lines
    for row in new_lines:
        row.batch_id = batch.id
        session.add(row)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    receipt["batch_id"] = batch.id
    logger.info(
        "attendance: imported %s — %d new, %d duplicates (batch %s, revision %d)",
        period,
        len(new_lines),
        duplicates,
        batch.id,
        revision,
    )
    return receipt


@dataclass(frozen=True)
class AttendanceRow:
    """One employee's live day counts for one period — the latest
    register's row, raw and meaning-free (the pack computes payable
    days). ``payable`` is deliberately absent."""

    employee: str
    present: Decimal
    absent: Decimal
    half: Decimal
    leave_paid: Decimal
    note: str | None
    batch_id: int


def live_register(session: Session, *, book: Book, period: str) -> list[AttendanceRow]:
    """The period's **live** register: the latest batch's lines, employees
    in stable (name) order — the 2B latest-snapshot semantics. Superseded
    batches stay as history (append-only, Law 2) but reads never see
    them. Exact counts as stored; zero arithmetic happens here."""
    batch_id = session.execute(
        select(AttendanceBatch.id)
        .where(AttendanceBatch.book_id == book.id, AttendanceBatch.period == period)
        .order_by(AttendanceBatch.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if batch_id is None:
        return []
    lines = (
        session.execute(
            select(AttendanceLine)
            .where(
                AttendanceLine.book_id == book.id,
                AttendanceLine.batch_id == batch_id,
            )
            .order_by(AttendanceLine.employee)
        )
        .scalars()
        .all()
    )
    return [
        AttendanceRow(
            employee=line.employee,
            present=Decimal(str(line.days_present)),
            absent=Decimal(str(line.days_absent)),
            half=Decimal(str(line.days_half)),
            leave_paid=Decimal(str(line.days_leave_paid)),
            note=line.note,
            batch_id=line.batch_id,
        )
        for line in lines
    ]


def register_history(session: Session, *, book: Book, period: str) -> list[dict]:
    """Every batch receipt for the period, newest first — the honest
    provenance of every day count the live register shows."""
    batches = (
        session.execute(
            select(AttendanceBatch)
            .where(AttendanceBatch.book_id == book.id, AttendanceBatch.period == period)
            .order_by(AttendanceBatch.id.desc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": b.id,
            "period": b.period,
            "source": b.source,
            "line_count": b.line_count,
            "imported_at": b.imported_at.isoformat() if b.imported_at else None,
        }
        for b in batches
    ]
