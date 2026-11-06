"""The ACID posting engine (ADR-002, docs/analysis.md §4, §8.2)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, getcontext
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from ..errors import (
    EngineError,
    NotFoundError,
    PeriodStateError,
    UnbalancedVoucherError,
    ValidationError,
)
from ..models import Book, DocCounter, Ledger, Period, Voucher, VoucherEntry, VoucherType
from ..plugins import DraftLine, PluginManager, Resolver, VoucherDraft
from ..schemas import VoucherCreate, _coerce_amount
from ..settings import Settings
from .currency import validate_fx_lines

# Exact-arithmetic headroom (Law 3)
getcontext().prec = 50


# --------------------------------------------------------------- small utils
def fiscal_year_label(d: date, start_month: int) -> str:
    """Indian-style label for a fiscal year starting ``start_month``."""
    if start_month == 1:
        return str(d.year)
    year = d.year if d.month >= start_month else d.year - 1
    return f"{year}-{(year + 1) % 100:02d}"


def format_voucher_no(prefix: str, fy_label: str | None, seq: int) -> str:
    parts = [p for p in (prefix, fy_label, f"{seq:05d}") if p]
    return "-".join(parts)


# ------------------------------------------------------- period gate (issue #5)
def _check_period_open(session: Session, book: Book, voucher_date: date) -> None:
    """App-level half of the posting gate (ADR-008), scoped to ``book``
    (ADR-009): with no periods defined for the book every date posts; once
    they exist, a date must fall in an OPEN period. The BEFORE INSERT
    trigger on vouchers enforces the same rule at commit time — this check
    exists to give a clean 409/422 instead of a 500 when a lock races the
    posting."""
    has_periods = session.scalar(
        select(func.count()).select_from(Period).where(Period.book_id == book.id)
    )
    if not has_periods:
        return
    period = session.scalar(
        select(Period).where(
            Period.book_id == book.id,
            Period.starts_on <= voucher_date,
            Period.ends_on >= voucher_date,
        )
    )
    if period is None:
        raise ValidationError(
            f"No period covers voucher date {voucher_date}: create the period first"
        )
    if period.status == "locked":
        raise PeriodStateError(
            f"Period {period.name!r} is locked: postings are frozen until it is closed or reopened"
        )
    if period.status == "closed":
        raise PeriodStateError(f"Period {period.name!r} is closed: its dates are final")


# ---------------------------------------------------------------- validation
def _validate_draft(draft: VoucherDraft, settings: Settings) -> None:
    if len(draft.lines) < 2:
        raise ValidationError("A voucher must contain at least 2 entries")
    for i, line in enumerate(draft.lines, start=1):
        if line.ledger_id is None and not line.ledger_name:
            raise ValidationError(f"Line {i}: provide ledger_id or ledger_name")
        try:
            _coerce_amount(line.amount)  # re-checks positivity/scale/finite (Law 3)
        except ValueError as exc:
            raise ValidationError(f"Line {i}: {exc}") from exc
    dr = sum((ln.amount for ln in draft.lines if ln.dc == "D"), Decimal(0))
    cr = sum((ln.amount for ln in draft.lines if ln.dc == "C"), Decimal(0))
    if dr != cr:  # exact Decimal equality — no epsilon (Law 1)
        raise UnbalancedVoucherError(
            f"Unbalanced voucher: total debit {dr} does not equal total credit {cr}"
        )
    if not settings.allow_future_dates and draft.voucher_date > date.today() + timedelta(days=1):
        raise ValidationError(
            f"Voucher date {draft.voucher_date} is in the future "
            "(set LF_ALLOW_FUTURE_DATES to permit)"
        )


# ---------------------------------------------------------------- resolution
def _resolve_ledgers(
    session: Session, book: Book, lines: list[DraftLine]
) -> list[tuple[DraftLine, Ledger]]:
    resolved: list[tuple[DraftLine, Ledger]] = []
    for i, line in enumerate(lines, start=1):
        stmt = (
            select(Ledger).where(Ledger.id == line.ledger_id, Ledger.book_id == book.id)
            if line.ledger_id is not None
            else select(Ledger).where(Ledger.book_id == book.id, Ledger.name == line.ledger_name)
        )
        ledger = session.scalar(stmt)
        if ledger is None:
            ref = f"id {line.ledger_id}" if line.ledger_id else f"name {line.ledger_name!r}"
            raise NotFoundError(
                f"Line {i}: ledger {ref} does not exist in book {book.name!r} "
                "(create it first, or use Alt+C-style master creation)"
            )
        resolved.append((line, ledger))
    return resolved


def _period_gate_error(exc: Exception) -> EngineError | None:
    """Translate the DB posting-gate trigger's verdict (issue #5, ADR-008)
    into the same clean engine error the app-level check raises. A lock or
    close can commit between ``_check_period_open`` and the voucher INSERT;
    the trigger then fires at INSERT time and its raw P0001 must never leak
    out as a driver error (an HTTP 500 at the API)."""
    message = str(getattr(exc, "orig", exc))
    lowered = message.lower()
    if "is locked" in lowered or "is closed" in lowered:
        return PeriodStateError(message)
    if "no period covers" in lowered:
        return ValidationError(message)
    return None


# ------------------------------------------------------------------ posting
def post_voucher(
    session: Session,
    payload: VoucherCreate,
    manager: PluginManager,
    settings: Settings,
    book: Book,
) -> tuple[Voucher, bool]:
    """Post a voucher into ``book`` in ONE ACID transaction (ADR-009).
    Returns (voucher, created); ``created=False`` means an idempotent
    replay of the same client_token — replay resolves within the book."""
    # 1. Idempotency (book-scoped: the same offline client may retry into
    # two books without interference — ADR-009)
    if payload.client_token is not None:
        existing = session.scalar(
            select(Voucher).where(
                Voucher.client_token == payload.client_token, Voucher.book_id == book.id
            )
        )
        if existing is not None:
            return existing, False

    draft = VoucherDraft(
        voucher_type=payload.voucher_type,
        voucher_date=payload.voucher_date,
        lines=[
            DraftLine(
                dc=ln.dc,
                amount=ln.amount,
                ledger_id=ln.ledger_id,
                ledger_name=ln.ledger_name,
                narration=ln.narration,
                currency=ln.currency,
                amount_fx=ln.amount_fx,
                fx_rate=ln.fx_rate,
            )
            for ln in payload.entries
        ],
        narration=payload.narration,
        client_token=payload.client_token,
        meta=payload.meta,
        reversal_of=payload.reversal_of,
    )

    # 2-3. Plugin transformers + validators (inside the transaction)
    resolver = Resolver(session, book)
    draft = manager.transform(draft, resolver)
    manager.validate(draft, resolver)

    # 4. Engine validation — the three laws, the FX rules, the period gate
    _validate_draft(draft, settings)
    validate_fx_lines(session, draft, book.base_currency)
    _check_period_open(session, book, draft.voucher_date)

    # 5. Resolve masters (every lookup lives inside the book)
    vtype = session.scalar(
        select(VoucherType).where(
            VoucherType.code == draft.voucher_type, VoucherType.book_id == book.id
        )
    )
    if vtype is None:
        raise NotFoundError(
            f"Voucher type {draft.voucher_type!r} does not exist in book {book.name!r}"
        )
    resolved = _resolve_ledgers(session, book, draft.lines)

    # 6. Gapless numbering: lock the counter row (the only locked row —
    # ADR-002). Scope is '{book}:{vtype}:{fy}' — every book numbers from 1.
    fy_label = (
        fiscal_year_label(draft.voucher_date, book.fy_start_month) if vtype.restart_yearly else None
    )
    scope = f"{book.id}:{vtype.id}:{fy_label if vtype.restart_yearly else 'ALL'}"
    session.execute(
        pg_insert(DocCounter)
        .values(scope_key=scope, last_value=0)
        .on_conflict_do_nothing(index_elements=[DocCounter.scope_key])
    )
    counter = session.execute(
        select(DocCounter).where(DocCounter.scope_key == scope).with_for_update()
    ).scalar_one()
    counter.last_value += 1
    sequence = counter.last_value
    voucher_no = format_voucher_no(vtype.number_prefix, fy_label, sequence)

    # 7. Insert (append-only)
    voucher = Voucher(
        book_id=book.id,
        client_token=draft.client_token or uuid4(),
        voucher_type_id=vtype.id,
        voucher_no=voucher_no,
        sequence=sequence,
        voucher_date=draft.voucher_date,
        narration=draft.narration,
        reversal_of=draft.reversal_of,
    )
    session.add(voucher)
    try:
        session.flush()  # the period gate trigger fires HERE if a lock raced us
    except ProgrammingError as exc:
        session.rollback()
        gate = _period_gate_error(exc)
        if gate is not None:
            raise gate from exc
        raise

    for line_no, (line, ledger) in enumerate(resolved, start=1):
        session.add(
            VoucherEntry(
                voucher_id=voucher.id,
                line_no=line_no,
                ledger_id=ledger.id,
                book_id=book.id,
                dc=line.dc,
                amount=line.amount,
                voucher_date=voucher.voucher_date,
                narration=line.narration,
                currency=line.currency,
                amount_fx=line.amount_fx,
                fx_rate=line.fx_rate,
            )
        )

    # 8. Commit — the deferred constraint trigger re-verifies Law 1 here
    try:
        session.commit()
    except (IntegrityError, ProgrammingError) as exc:
        session.rollback()
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == "23505" and draft.client_token is not None:
            existing = session.scalar(
                select(Voucher).where(
                    Voucher.client_token == draft.client_token, Voucher.book_id == book.id
                )
            )
            if existing is not None:
                return existing, False
        gate = _period_gate_error(exc)
        if gate is not None:
            raise gate from exc
        raise

    # 9. After-commit events (isolated; never break posting)
    manager.emit(
        "voucher.posted",
        {
            "voucher_id": voucher.id,
            "voucher_no": voucher.voucher_no,
            "uuid": str(voucher.uuid),
            "book_id": book.id,
            "voucher_type": vtype.code,
            "voucher_date": voucher.voucher_date.isoformat(),
        },
    )
    return voucher, True


def get_voucher_with_entries(
    session: Session, voucher_id: int
) -> tuple[Voucher, list[VoucherEntry]]:
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:
        raise NotFoundError(f"Voucher {voucher_id} does not exist")
    entries = list(
        session.scalars(
            select(VoucherEntry)
            .where(VoucherEntry.voucher_id == voucher_id)
            .order_by(VoucherEntry.line_no)
        )
    )
    return voucher, entries
