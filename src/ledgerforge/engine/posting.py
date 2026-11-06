"""The ACID posting engine (ADR-002, docs/analysis.md §4, §8.2)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, getcontext
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import (
    NotFoundError,
    PeriodStateError,
    UnbalancedVoucherError,
    ValidationError,
)
from ..models import DocCounter, Ledger, Period, Voucher, VoucherEntry, VoucherType
from ..plugins import DraftLine, PluginManager, Resolver, VoucherDraft
from ..schemas import VoucherCreate, _coerce_amount
from ..settings import Settings
from .currency import base_currency_code, validate_fx_lines

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


def _fy_start_month(session: Session) -> int:
    raw = session.execute(text("SELECT value FROM settings WHERE key = 'fy_start_month'")).scalar()
    try:
        return int(raw or 4)
    except (TypeError, ValueError):
        return 4


# ------------------------------------------------------- period gate (issue #5)
def _check_period_open(session: Session, voucher_date: date) -> None:
    """App-level half of the posting gate (ADR-008): with no periods defined
    every date posts; once periods exist, a date must fall in an OPEN period.
    The BEFORE INSERT trigger on vouchers enforces the same rule at commit
    time — this check exists to give a clean 409/422 instead of a 500 when a
    lock races the posting."""
    has_periods = session.scalar(select(func.count()).select_from(Period))
    if not has_periods:
        return
    period = session.scalar(
        select(Period).where(Period.starts_on <= voucher_date, Period.ends_on >= voucher_date)
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
def _resolve_ledgers(session: Session, lines: list[DraftLine]) -> list[tuple[DraftLine, Ledger]]:
    resolved: list[tuple[DraftLine, Ledger]] = []
    for i, line in enumerate(lines, start=1):
        stmt = (
            select(Ledger).where(Ledger.id == line.ledger_id)
            if line.ledger_id is not None
            else select(Ledger).where(Ledger.name == line.ledger_name)
        )
        ledger = session.scalar(stmt)
        if ledger is None:
            ref = f"id {line.ledger_id}" if line.ledger_id else f"name {line.ledger_name!r}"
            raise NotFoundError(
                f"Line {i}: ledger {ref} does not exist (create it first, "
                "or use Alt+C-style master creation)"
            )
        resolved.append((line, ledger))
    return resolved


# ------------------------------------------------------------------ posting
def post_voucher(
    session: Session,
    payload: VoucherCreate,
    manager: PluginManager,
    settings: Settings,
) -> tuple[Voucher, bool]:
    """Post a voucher in ONE ACID transaction. Returns (voucher, created);
    ``created=False`` means an idempotent replay of the same client_token."""
    # 1. Idempotency
    if payload.client_token is not None:
        existing = session.scalar(
            select(Voucher).where(Voucher.client_token == payload.client_token)
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
    resolver = Resolver(session)
    draft = manager.transform(draft, resolver)
    manager.validate(draft, resolver)

    # 4. Engine validation — the three laws, the FX rules, the period gate
    _validate_draft(draft, settings)
    validate_fx_lines(session, draft, base_currency_code(session))
    _check_period_open(session, draft.voucher_date)

    # 5. Resolve masters
    vtype = session.scalar(select(VoucherType).where(VoucherType.code == draft.voucher_type))
    if vtype is None:
        raise NotFoundError(f"Voucher type {draft.voucher_type!r} does not exist")
    resolved = _resolve_ledgers(session, draft.lines)

    # 6. Gapless numbering: lock the counter row (the only locked row — ADR-002)
    fy_label = (
        fiscal_year_label(draft.voucher_date, _fy_start_month(session))
        if vtype.restart_yearly
        else None
    )
    scope = f"{vtype.id}:{fy_label if vtype.restart_yearly else 'ALL'}"
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
        client_token=draft.client_token or uuid4(),
        voucher_type_id=vtype.id,
        voucher_no=voucher_no,
        sequence=sequence,
        voucher_date=draft.voucher_date,
        narration=draft.narration,
        reversal_of=draft.reversal_of,
    )
    session.add(voucher)
    session.flush()

    for line_no, (line, ledger) in enumerate(resolved, start=1):
        session.add(
            VoucherEntry(
                voucher_id=voucher.id,
                line_no=line_no,
                ledger_id=ledger.id,
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
    except IntegrityError as exc:
        session.rollback()
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == "23505" and draft.client_token is not None:
            existing = session.scalar(
                select(Voucher).where(Voucher.client_token == draft.client_token)
            )
            if existing is not None:
                return existing, False
        raise

    # 9. After-commit events (isolated; never break posting)
    manager.emit(
        "voucher.posted",
        {
            "voucher_id": voucher.id,
            "voucher_no": voucher.voucher_no,
            "uuid": str(voucher.uuid),
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
