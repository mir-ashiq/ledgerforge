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
