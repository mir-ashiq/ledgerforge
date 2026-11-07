"""Typed views over the engine's JSON — the native app never sums strings.

Every amount crosses the wire as a string (Law 3 — the client never sums);
here it becomes a ``Decimal`` exactly once, at the boundary, and stays one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def dec(value: Any) -> Decimal:
    """JSON number-or-string -> Decimal, never float."""
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class VoucherLine:
    line_no: int
    ledger_id: int
    ledger_name: str
    dc: str  # "D" | "C"
    amount: Decimal
    narration: str | None
    currency: str | None = None
    amount_fx: Decimal | None = None
    fx_rate: Decimal | None = None

    @classmethod
    def from_json(cls, raw: dict) -> VoucherLine:
        return cls(
            line_no=raw["line_no"],
            ledger_id=raw["ledger_id"],
            ledger_name=raw["ledger_name"],
            dc=raw["dc"],
            amount=dec(raw["amount"]),
            narration=raw.get("narration"),
            currency=raw.get("currency"),
            amount_fx=dec(raw["amount_fx"]) if raw.get("amount_fx") is not None else None,
            fx_rate=dec(raw["fx_rate"]) if raw.get("fx_rate") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class Voucher:
    id: int
    uuid: str
    voucher_no: str
    voucher_type: str
    voucher_date: date
    amount: Decimal  # the debit-side total, engine-quantized (ADR-015)
    narration: str | None
    reversal_of: int | None
    posted_at: datetime
    client_token: str
    meta: dict[str, Any] = field(default_factory=dict)
    entries: tuple[VoucherLine, ...] = ()

    @classmethod
    def from_json(cls, raw: dict) -> Voucher:
        return cls(
            id=raw["id"],
            uuid=raw["uuid"],
            voucher_no=raw["voucher_no"],
            voucher_type=raw.get("voucher_type") or "",
            voucher_date=date.fromisoformat(raw["voucher_date"]),
            amount=dec(raw.get("amount", 0)),
            narration=raw.get("narration"),
            reversal_of=raw.get("reversal_of"),
            posted_at=datetime.fromisoformat(raw["posted_at"]),
            client_token=raw.get("client_token", ""),
            meta=raw.get("meta") or {},
            entries=tuple(VoucherLine.from_json(e) for e in raw.get("entries", ())),
        )

    @property
    def is_reversal(self) -> bool:
        return self.reversal_of is not None


@dataclass(frozen=True, slots=True)
class Ledger:
    id: int
    name: str
    code: str | None
    group_id: int
    group_name: str
    is_system: bool

    @classmethod
    def from_json(cls, raw: dict) -> Ledger:
        return cls(
            id=raw["id"],
            name=raw["name"],
            code=raw.get("code"),
            group_id=raw["group_id"],
            group_name=raw["group_name"],
            is_system=raw.get("is_system", False),
        )


@dataclass(frozen=True, slots=True)
class Group:
    id: int
    name: str
    code: str | None
    parent_id: int | None
    nature: str
    affects_gross: bool
    is_root: bool

    @classmethod
    def from_json(cls, raw: dict) -> Group:
        return cls(
            id=raw["id"],
            name=raw["name"],
            code=raw.get("code"),
            parent_id=raw.get("parent_id"),
            nature=raw["nature"],
            affects_gross=raw.get("affects_gross", False),
            is_root=raw.get("is_root", False),
        )


@dataclass(frozen=True, slots=True)
class Book:
    id: int
    name: str
    slug: str
    base_currency: str
    fy_start_month: int
    is_archived: bool

    @classmethod
    def from_json(cls, raw: dict) -> Book:
        return cls(
            id=raw["id"],
            name=raw["name"],
            slug=raw["slug"],
            base_currency=raw["base_currency"],
            fy_start_month=raw["fy_start_month"],
            is_archived=raw.get("is_archived", False),
        )


@dataclass(frozen=True, slots=True)
class Period:
    id: int
    name: str
    starts_on: date
    ends_on: date
    status: str  # "open" | "locked" | "closed"
    locked_at: datetime | None
    closed_at: datetime | None
    closed_by: str | None

    @classmethod
    def from_json(cls, raw: dict) -> Period:
        return cls(
            id=raw["id"],
            name=raw["name"],
            starts_on=date.fromisoformat(raw["starts_on"]),
            ends_on=date.fromisoformat(raw["ends_on"]),
            status=raw["status"],
            locked_at=datetime.fromisoformat(raw["locked_at"]) if raw.get("locked_at") else None,
            closed_at=datetime.fromisoformat(raw["closed_at"]) if raw.get("closed_at") else None,
            closed_by=raw.get("closed_by"),
        )


@dataclass(frozen=True, slots=True)
class Slot:
    key: str
    title: str
    plugin: str
    source: str
    trust: str
    url: str | None

    @classmethod
    def from_json(cls, raw: dict) -> Slot:
        return cls(
            key=raw["key"],
            title=raw["title"],
            plugin=raw["plugin"],
            source=raw["source"],
            trust=raw["trust"],
            url=raw.get("url"),
        )


@dataclass(frozen=True, slots=True)
class TrialBalanceRow:
    ledger_name: str
    group_name: str
    nature: str
    dr: Decimal
    cr: Decimal
    balance: Decimal
    dc: str | None

    @classmethod
    def from_json(cls, raw: dict) -> TrialBalanceRow:
        return cls(
            ledger_name=raw["ledger_name"],
            group_name=raw["group_name"],
            nature=raw["nature"],
            dr=dec(raw["dr"]),
            cr=dec(raw["cr"]),
            balance=dec(raw["balance"]),
            dc=raw.get("dc"),
        )


def money(value: Decimal | str | int) -> str:
    """Plain, locale-free money text: 1,234,567.89 — the tables' cell format."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return f"{d:,.2f}"
