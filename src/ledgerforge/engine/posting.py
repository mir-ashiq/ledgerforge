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
from ..models import (
    Book,
    CostAllocation,
    CostCentre,
    DocCounter,
    Ledger,
    Period,
    Voucher,
    VoucherEntry,
    VoucherType,
)
from ..plugins import DraftCostAllocation, DraftLine, PluginManager, Resolver, VoucherDraft
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


def _validate_cost_allocations(draft: VoucherDraft) -> None:
    """The allocation half of the posting law (ADR-025): a line is fully
    allocated or left unallocated — Σallocation = amount, exact Decimal
    equality, no epsilon. Runs after every plugin hook, so a transformer
    that split a line without splitting its allocations is caught here."""
    for i, line in enumerate(draft.lines, start=1):
        if not line.cost_allocations:
            continue
        for j, alloc in enumerate(line.cost_allocations, start=1):
            if alloc.centre_id is None and not alloc.centre_name:
                raise ValidationError(f"Line {i}, allocation {j}: provide centre_id or centre_name")
            if alloc.amount is None or alloc.amount <= 0:
                raise ValidationError(
                    f"Line {i}, allocation {j}: the allocation amount must be positive"
                )
        allocated = sum((a.amount for a in line.cost_allocations), Decimal(0))
        if allocated != line.amount:
            raise ValidationError(
                f"Line {i}: cost allocations sum to {allocated} against an amount of "
                f"{line.amount} — a line is fully allocated or left unallocated, never partly"
            )


def _resolve_centres_by_id(session: Session, book: Book, lines: list[DraftLine]) -> None:
    """Resolve and NORMALISE every allocation's centre inside ``book``
    (ADR-025, with ADR-009's discipline: another book's centre id is a 404,
    never a cross-book allocation). Each allocation's ``centre_id`` is set
    to the resolved row (name lookups collapse to ids); the root centre is
    the tree's container and can never receive an allocation."""
    for i, line in enumerate(lines, start=1):
        for j, alloc in enumerate(line.cost_allocations, start=1):
            stmt = (
                select(CostCentre).where(
                    CostCentre.id == alloc.centre_id, CostCentre.book_id == book.id
                )
                if alloc.centre_id is not None
                else select(CostCentre).where(
                    CostCentre.book_id == book.id, CostCentre.name == alloc.centre_name
                )
            )
            centre = session.scalar(stmt)
            if centre is None:
                ref = f"id {alloc.centre_id}" if alloc.centre_id else f"name {alloc.centre_name!r}"
                raise NotFoundError(
                    f"Line {i}, allocation {j}: cost centre {ref} does not exist "
                    f"in book {book.name!r}"
                )
            if centre.is_root:
                raise ValidationError(
                    f"Line {i}, allocation {j}: the root cost centre is the tree's "
                    "container — allocate to a real centre"
                )
            alloc.centre_id = centre.id
            alloc.centre_name = None


# ------------------------------------------------------------------ posting
def post_voucher(
    session: Session,
    payload: VoucherCreate,
    manager: PluginManager,
    settings: Settings,
    book: Book,
    *,
    emit: bool = True,
) -> tuple[Voucher, bool]:
    """Post a voucher into ``book`` in ONE ACID transaction (ADR-009).
    Returns (voucher, created); ``created=False`` means an idempotent
    replay of the same client_token — replay resolves within the book.

    ``emit=False`` (ADR-030, the import pipeline) suppresses the
    after-commit event: an import runs many vouchers inside ONE outer
    transaction (each ``commit()`` a savepoint release), so the caller
    collects the payloads and emits them after the outer commit — the
    event's "after-commit" promise stays true."""
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
                cost_allocations=[
                    DraftCostAllocation(
                        centre_id=a.centre_id, centre_name=a.centre_name, amount=a.amount
                    )
                    for a in ln.cost_allocations
                ],
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

    # 4. Engine validation — the three laws, the FX rules, the allocation
    # law (ADR-025), the period gate
    _validate_draft(draft, settings)
    validate_fx_lines(session, draft, book.base_currency)
    _validate_cost_allocations(draft)
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
    _resolve_centres_by_id(session, book, draft.lines)

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
        meta=draft.meta or {},  # post-transform: plugin annotations included (ADR-010)
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
        entry = VoucherEntry(
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
        session.add(entry)
        session.flush()  # entry.id for the allocations that split it
        for alloc in line.cost_allocations:
            session.add(
                CostAllocation(
                    entry_id=entry.id,
                    centre_id=alloc.centre_id,
                    book_id=book.id,
                    dc=line.dc,  # Dr/Cr preserved from the entry (ADR-025)
                    amount=alloc.amount,
                    voucher_date=voucher.voucher_date,
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

    # 9. After-commit events (isolated; never break posting). The import
    # pipeline defers these past its outer commit (emit=False above).
    if emit:
        manager.emit(
            "voucher.posted",
            {
                "voucher_id": voucher.id,
                "voucher_no": voucher.voucher_no,
                "uuid": str(voucher.uuid),
                "book_id": book.id,
                "voucher_type": vtype.code,
                "voucher_date": voucher.voucher_date.isoformat(),
                "meta": dict(voucher.meta or {}),  # the full document for after-commit bridges
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
