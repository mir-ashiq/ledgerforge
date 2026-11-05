"""Pydantic API schemas.

Law 3 lives on the wire here: ``Amount`` accepts only decimal strings and
integers — JSON floats are rejected with a 422 because a float has already
lost precision by the time any JSON parser is done with it. Responses
serialize amounts back as strings.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    PlainSerializer,
    model_validator,
)

MAX_AMOUNT = Decimal(10) ** 18
MAX_SCALE = 8  # matches NUMERIC(26,8); PostgreSQL would round beyond this


def _coerce_amount(v: Any) -> Decimal:
    if isinstance(v, bool) or v is None:
        raise ValueError('amount must be a decimal string like "1250.00"')
    if isinstance(v, float):
        raise ValueError(
            'amount must be sent as a string (e.g. "1250.00"), not a JSON number — '
            "floats lose precision (financial law: exact decimals only)"
        )
    try:
        if isinstance(v, Decimal):
            d = v
        elif isinstance(v, int):
            d = Decimal(v)
        elif isinstance(v, str):
            d = Decimal(v.strip())
        else:
            raise ValueError("amount must be a decimal string")
    except InvalidOperation as exc:
        raise ValueError(f"amount {v!r} is not a valid decimal") from exc
    if not d.is_finite():
        raise ValueError("amount must be finite")
    if d <= 0:
        raise ValueError("amount must be positive")
    if -d.as_tuple().exponent > MAX_SCALE:
        raise ValueError(f"amount supports at most {MAX_SCALE} decimal places")
    if d >= MAX_AMOUNT:
        raise ValueError("amount is too large (max 10^18)")
    return d


Amount = Annotated[Decimal, BeforeValidator(_coerce_amount)]
AmountOut = Annotated[Decimal, PlainSerializer(lambda v: format(v, "f"), return_type=str)]
Dc = Literal["D", "C"]


# ---------------------------------------------------------------- masters in
class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str | None = None
    parent_id: int | None = None
    parent_name: str | None = None
    nature: Literal["asset", "liability", "equity", "income", "expense"] | None = None
    affects_gross: bool | None = None

    @model_validator(mode="after")
    def _parent_given(self):
        if self.parent_id is None and self.parent_name is None:
            raise ValueError("provide parent_id or parent_name")
        return self


class GroupUpdate(BaseModel):
    parent_id: int | None = None
    parent_name: str | None = None
    nature: Literal["asset", "liability", "equity", "income", "expense"] | None = None
    affects_gross: bool | None = None
    code: str | None = None


class LedgerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str | None = None
    group_id: int | None = None
    group_name: str | None = None

    @model_validator(mode="after")
    def _group_given(self):
        if self.group_id is None and self.group_name is None:
            raise ValueError("provide group_id or group_name")
        return self


# --------------------------------------------------------------- masters out
class GroupOut(BaseModel):
    id: int
    name: str
    code: str | None
    parent_id: int | None
    nature: str
    affects_gross: bool
    is_root: bool


class LedgerOut(BaseModel):
    id: int
    name: str
    code: str | None
    group_id: int
    group_name: str
    is_system: bool


# ----------------------------------------------------------------- vouchers
class VoucherLineIn(BaseModel):
    ledger_id: int | None = None
    ledger_name: str | None = None
    dc: Dc
    amount: Amount
    narration: str | None = None

    @model_validator(mode="after")
    def _ledger_given(self):
        if self.ledger_id is None and self.ledger_name is None:
            raise ValueError("provide ledger_id or ledger_name on every line")
        return self


class VoucherCreate(BaseModel):
    voucher_type: str = Field(description="voucher type code, e.g. SAL, PYT, JV, OPEN")
    voucher_date: date
    narration: str | None = None
    client_token: UUID | None = Field(
        default=None, description="idempotency key; resend to replay safely"
    )
    meta: dict[str, Any] = Field(default_factory=dict, description="plugin input, e.g. gst")
    entries: list[VoucherLineIn] = Field(min_length=1)
    reversal_of: int | None = Field(default=None, exclude=False)


class VoucherLineOut(BaseModel):
    line_no: int
    ledger_id: int
    ledger_name: str
    dc: Dc
    amount: AmountOut
    narration: str | None


class VoucherOut(BaseModel):
    id: int
    uuid: UUID
    voucher_no: str
    voucher_type: str
    voucher_date: date
    narration: str | None
    reversal_of: int | None
    posted_at: datetime
    client_token: UUID
    entries: list[VoucherLineOut]
    replayed: bool = False


class ReversalCreate(BaseModel):
    reversal_date: date | None = None
    narration: str | None = None
    client_token: UUID | None = None


class OpeningCreate(BaseModel):
    voucher_date: date
    narration: str | None = "Opening balances"
    client_token: UUID | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    entries: list[VoucherLineIn] = Field(min_length=2)
