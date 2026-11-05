"""SQLAlchemy ORM models. The database DDL (db/schema.sql) is the source of
truth for constraints; these mappings give the engine typed access."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Identity, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


#: shared column kwargs — keeps every line under 100 chars
NOW_DEFAULT = {"server_default": text("now()")}


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
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
    name: Mapped[str] = mapped_column(Text, unique=True)
    code: Mapped[str | None]
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="RESTRICT"))
    is_system: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class VoucherType(Base):
    __tablename__ = "voucher_types"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str]
    number_prefix: Mapped[str] = mapped_column(Text, default="")
    restart_yearly: Mapped[bool] = mapped_column(default=True)
    is_system: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class Voucher(Base):
    __tablename__ = "vouchers"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    uuid: Mapped[str] = mapped_column(
        UUID(as_uuid=True), unique=True, server_default=text("gen_random_uuid()")
    )
    client_token: Mapped[str] = mapped_column(UUID(as_uuid=True), unique=True)
    voucher_type_id: Mapped[int] = mapped_column(ForeignKey("voucher_types.id"))
    voucher_no: Mapped[str]
    sequence: Mapped[int] = mapped_column(BigInteger)
    voucher_date: Mapped[date] = mapped_column(Date)
    narration: Mapped[str | None]
    reversal_of: Mapped[int | None] = mapped_column(ForeignKey("vouchers.id"))
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class VoucherEntry(Base):
    __tablename__ = "voucher_entries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    voucher_id: Mapped[int] = mapped_column(ForeignKey("vouchers.id", ondelete="RESTRICT"))
    line_no: Mapped[int]
    ledger_id: Mapped[int] = mapped_column(ForeignKey("ledgers.id", ondelete="RESTRICT"))
    dc: Mapped[str] = mapped_column(Text, CheckConstraint("dc IN ('D', 'C')"))
    amount: Mapped[Decimal]
    voucher_date: Mapped[date] = mapped_column(Date)
    narration: Mapped[str | None]


class DocCounter(Base):
    __tablename__ = "doc_counters"

    scope_key: Mapped[str] = mapped_column(Text, primary_key=True)
    last_value: Mapped[int] = mapped_column(BigInteger)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **NOW_DEFAULT)
    actor: Mapped[str | None]
    table_name: Mapped[str]
    operation: Mapped[str]
    record_id: Mapped[int | None] = mapped_column(BigInteger)
    row_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
