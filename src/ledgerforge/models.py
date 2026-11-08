"""SQLAlchemy ORM models. The database DDL (db/schema.sql) is the source of
truth for constraints; these mappings give the engine typed access."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


#: shared column kwargs — keeps every line under 100 chars
NOW_DEFAULT = {"server_default": text("now()")}


class Book(Base):
    """One company / legal entity's complete accounting context (ADR-009,
    migration 0004): its chart of accounts, voucher types, numbering,
    periods, base currency and fiscal-year start. ``slug`` is the stable
    machine key (the bootstrap default book is ``slug='default'``)."""

    __tablename__ = "books"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    base_currency: Mapped[str] = mapped_column(CHAR(3), ForeignKey("currencies.code"))
    fy_start_month: Mapped[int] = mapped_column(SmallInteger, default=4)
    is_archived: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    name: Mapped[str] = mapped_column(Text)  # unique per book (uq_groups_book_name)
    code: Mapped[str | None]
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="RESTRICT"))
    nature: Mapped[str]
    affects_gross: Mapped[bool] = mapped_column(default=False)
    is_root: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class Ledger(Base):
    __tablename__ = "ledgers"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    name: Mapped[str] = mapped_column(Text)  # unique per book (uq_ledgers_book_name)
    code: Mapped[str | None]
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="RESTRICT"))
    is_system: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class VoucherType(Base):
    __tablename__ = "voucher_types"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    code: Mapped[str] = mapped_column(Text)  # unique per book (uq_voucher_types_book_code)
    name: Mapped[str]
    number_prefix: Mapped[str] = mapped_column(Text, default="")
    restart_yearly: Mapped[bool] = mapped_column(default=True)
    is_system: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class Voucher(Base):
    __tablename__ = "vouchers"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    uuid: Mapped[str] = mapped_column(
        UUID(as_uuid=True), unique=True, server_default=text("gen_random_uuid()")
    )
    client_token: Mapped[str] = mapped_column(UUID(as_uuid=True))  # unique per book
    voucher_type_id: Mapped[int] = mapped_column(ForeignKey("voucher_types.id"))
    voucher_no: Mapped[str]
    sequence: Mapped[int] = mapped_column(BigInteger)
    voucher_date: Mapped[date] = mapped_column(Date)
    narration: Mapped[str | None]
    reversal_of: Mapped[int | None] = mapped_column(ForeignKey("vouchers.id"))
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class VoucherEntry(Base):
    __tablename__ = "voucher_entries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    voucher_id: Mapped[int] = mapped_column(ForeignKey("vouchers.id", ondelete="RESTRICT"))
    line_no: Mapped[int]
    ledger_id: Mapped[int] = mapped_column(ForeignKey("ledgers.id", ondelete="RESTRICT"))
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    dc: Mapped[str] = mapped_column(Text, CheckConstraint("dc IN ('D', 'C')"))
    amount: Mapped[Decimal]
    voucher_date: Mapped[date] = mapped_column(Date)
    narration: Mapped[str | None]
    # FX snapshot (issue #4, ADR-007): all three or none; amount is and stays
    # the base-currency figure — the trio is proven consistent by the
    # chk_entry_fx_consistent database constraint.
    currency: Mapped[str | None] = mapped_column(CHAR(3), ForeignKey("currencies.code"))
    amount_fx: Mapped[Decimal | None]
    fx_rate: Mapped[Decimal | None]


class DocCounter(Base):
    __tablename__ = "doc_counters"

    scope_key: Mapped[str] = mapped_column(Text, primary_key=True)
    last_value: Mapped[int] = mapped_column(BigInteger)


class Setting(Base):
    """Reserved for genuine system-wide keys. Per-company facts (base
    currency, FY start, company name) moved to ``books`` with 0004/ADR-009."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class Currency(Base):
    """ISO 4217 reference currency, shared across books. Each book's base
    currency is a pointer on its row (``books.base_currency``, ADR-009);
    every other currency here is postable with an FX snapshot (issue #4,
    ADR-007)."""

    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    name: Mapped[str]
    symbol: Mapped[str | None]
    minor_unit: Mapped[int] = mapped_column(SmallInteger, default=2)
    is_system: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class FxRate(Base):
    """A dated reference rate *within a book*: 1 unit of ``from_ccy`` =
    ``rate`` units of ``to_ccy`` (NUMERIC(30,10), Law 3). Advisory input
    data — postings snapshot the applied rate on the entry row (ADR-007).
    Book-scoped by 0004/ADR-009."""

    __tablename__ = "fx_rates"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    from_ccy: Mapped[str] = mapped_column(CHAR(3), ForeignKey("currencies.code"))
    to_ccy: Mapped[str] = mapped_column(CHAR(3), ForeignKey("currencies.code"))
    rate: Mapped[Decimal]
    valid_on: Mapped[date] = mapped_column(Date)
    source: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class Period(Base):
    """A named, non-overlapping fiscal range — within one book — with a
    close state machine: ``open -> locked -> closed`` (open/locked
    reversible, closed final). Issue #5, ADR-008; book-scoped by 0004."""

    __tablename__ = "periods"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    name: Mapped[str]
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, default="open")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)
    actor: Mapped[str | None]
    table_name: Mapped[str]
    operation: Mapped[str]
    record_id: Mapped[int | None] = mapped_column(BigInteger)
    row_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class CostCategory(Base):
    """The classification axis for cost centres (ADR-025, migration 0007):
    "Department", "Location", "Project"… A tree with one root ("Primary")
    per book — the same adjacency+closure pair as groups (ADR-001)."""

    __tablename__ = "cost_categories"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    name: Mapped[str] = mapped_column(Text)  # unique per book
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("cost_categories.id", ondelete="RESTRICT")
    )
    is_root: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class CostCentre(Base):
    """The allocation tree (ADR-025, migration 0007). Every real centre
    answers to exactly one :class:`CostCategory` (TallyPrime's rule); the
    root is the tree's container and receives no allocations."""

    __tablename__ = "cost_centres"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    name: Mapped[str] = mapped_column(Text)  # unique per book
    code: Mapped[str | None]
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("cost_centres.id", ondelete="RESTRICT")
    )
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("cost_categories.id", ondelete="RESTRICT")
    )
    is_root: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class CostAllocation(Base):
    """One (voucher entry, cost centre, amount) split (ADR-025, migration
    0007) — the first core financial table since the baseline that is not a
    voucher. Dr/Cr is preserved from the entry; the conservation law
    (Σallocation = entry amount, exactly) is enforced by the database at
    commit, the same deferrable discipline as Law 1. Append-only."""

    __tablename__ = "cost_allocations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("voucher_entries.id", ondelete="RESTRICT"))
    centre_id: Mapped[int] = mapped_column(ForeignKey("cost_centres.id", ondelete="RESTRICT"))
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    dc: Mapped[str] = mapped_column(Text, CheckConstraint("dc IN ('D', 'C')"))
    amount: Mapped[Decimal]
    voucher_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class BankStatementBatch(Base):
    """One statement import's receipt (ADR-026, migration 0008): the format,
    the source label, and the opening/closing balance the statement itself
    carried (MT940 :60F:/:62F:; CSV callers may supply them). The anchor the
    reconciliation fold uses when the accountant does not hand it the as-of
    statement balance. Append-only — a receipt is history the moment the
    import lands."""

    __tablename__ = "bank_statement_batches"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    ledger_id: Mapped[int] = mapped_column(ForeignKey("ledgers.id", ondelete="RESTRICT"))
    format: Mapped[str] = mapped_column(Text, CheckConstraint("format IN ('csv', 'mt940')"))
    source: Mapped[str | None]
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    opening_balance: Mapped[Decimal | None]
    closing_balance: Mapped[Decimal | None]
    line_count: Mapped[int] = mapped_column(default=0)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class BankStatementLine(Base):
    """One imported bank-statement fact (ADR-026, migration 0008). ``dc`` is
    the side the books would post: C = money into the bank (statement
    credit), D = money out (statement debit) — so a matched pair is
    side-equal by nature. ``fingerprint`` is the content identity
    (date, direction, amount, reference, narrative, occurrence-within-batch):
    UNIQUE (book_id, fingerprint) makes re-importing the same statement a
    clean no-op while two genuinely identical transactions (occurrence 0, 1,
    …) both survive. Append-only, trigger-guarded."""

    __tablename__ = "bank_statement_lines"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    ledger_id: Mapped[int] = mapped_column(ForeignKey("ledgers.id", ondelete="RESTRICT"))
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("bank_statement_batches.id", ondelete="RESTRICT")
    )
    value_date: Mapped[date] = mapped_column(Date)
    dc: Mapped[str] = mapped_column(Text, CheckConstraint("dc IN ('D', 'C')"))
    amount: Mapped[Decimal]
    reference: Mapped[str | None]
    narrative: Mapped[str | None]
    cheque_no: Mapped[str | None]
    fingerprint: Mapped[str] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class BankLineMatch(Base):
    """The reconciliation annotation (ADR-026, migration 0008) — the
    kernel's first deliberately mutable table, and the stated boundary of
    Law 2: financial history is append-only; *which line matches which
    entry* is revisable judgement. One row per statement line ever: un-match
    sets ``unmatched_at``, re-match rewrites the target. The entry is
    referenced by validated id (the BEFORE INSERT/UPDATE trigger proves its
    voucher, date, book and ledger on both ``voucher_entries`` shapes), so
    the ADR-018 partition swap never touches this table. DELETE never."""

    __tablename__ = "bank_line_matches"

    statement_line_id: Mapped[int] = mapped_column(
        ForeignKey("bank_statement_lines.id", ondelete="RESTRICT"), primary_key=True
    )
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"))
    voucher_id: Mapped[int] = mapped_column(ForeignKey("vouchers.id", ondelete="RESTRICT"))
    entry_id: Mapped[int]
    voucher_date: Mapped[date] = mapped_column(Date)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)
    matched_by: Mapped[str | None]
    unmatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
