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
MAX_FX_RATE = Decimal(10) ** 20
MAX_FX_SCALE = 10  # matches NUMERIC(30,10)


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


def _coerce_fx_rate(v: Any) -> Decimal:
    """FX rates are decimal strings too (Law 3) — same rules as amounts,
    scaled to NUMERIC(30,10)."""
    if isinstance(v, bool) or v is None:
        raise ValueError('fx_rate must be a decimal string like "83.5050000000"')
    if isinstance(v, float):
        raise ValueError(
            "fx_rate must be sent as a string, not a JSON number — floats lose "
            "precision (financial law: exact decimals only)"
        )
    try:
        if isinstance(v, Decimal):
            d = v
        elif isinstance(v, int):
            d = Decimal(v)
        elif isinstance(v, str):
            d = Decimal(v.strip())
        else:
            raise ValueError("fx_rate must be a decimal string")
    except InvalidOperation as exc:
        raise ValueError(f"fx_rate {v!r} is not a valid decimal") from exc
    if not d.is_finite():
        raise ValueError("fx_rate must be finite")
    if d <= 0:
        raise ValueError("fx_rate must be positive")
    if -d.as_tuple().exponent > MAX_FX_SCALE:
        raise ValueError(f"fx_rate supports at most {MAX_FX_SCALE} decimal places")
    if d >= MAX_FX_RATE:
        raise ValueError("fx_rate is too large (max 10^20)")
    return d


FxRateIn = Annotated[Decimal, BeforeValidator(_coerce_fx_rate)]
RateOut = Annotated[Decimal, PlainSerializer(lambda v: format(v, "f"), return_type=str)]


def _upper_code(v: str | None) -> str | None:
    return v.upper() if isinstance(v, str) else v


CurrencyCode = Annotated[str, BeforeValidator(_upper_code)]


# --------------------------------------------------------------------- books
class BookCreate(BaseModel):
    """POST /api/v1/books (ADR-009): creates the book AND seeds its chart
    of accounts (TallyPrime's 28 groups, system ledgers, 10 voucher
    types) in one action."""

    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="stable machine key; derived from the name when omitted",
    )
    base_currency: CurrencyCode = Field(default="INR", min_length=3, max_length=3)
    fy_start_month: int = Field(default=4, ge=1, le=12)


class BookUpdate(BaseModel):
    """PATCH /api/v1/books/{id}. ``base_currency`` and ``fy_start_month``
    are immutable once the book has vouchers (amounts are stored in the
    base currency — changing it would restate history, which Law 2
    forbids)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_currency: CurrencyCode | None = Field(default=None, min_length=3, max_length=3)
    fy_start_month: int | None = Field(default=None, ge=1, le=12)
    is_archived: bool | None = None


class BookOut(BaseModel):
    id: int
    name: str
    slug: str
    base_currency: str
    fy_start_month: int
    is_archived: bool
    created_at: datetime


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
    # FX snapshot (issue #4, ADR-007): all three or none. ``amount`` is ALWAYS
    # the base-currency figure and must equal round(amount_fx * fx_rate, 8).
    currency: CurrencyCode | None = Field(default=None, min_length=3, max_length=3)
    amount_fx: Amount | None = None
    fx_rate: FxRateIn | None = None

    @model_validator(mode="after")
    def _ledger_given(self):
        if self.ledger_id is None and self.ledger_name is None:
            raise ValueError("provide ledger_id or ledger_name on every line")
        fx_fields = (self.currency, self.amount_fx, self.fx_rate)
        if any(f is not None for f in fx_fields) and not all(f is not None for f in fx_fields):
            raise ValueError("foreign-currency lines need currency, amount_fx and fx_rate together")
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
    currency: str | None = None
    amount_fx: AmountOut | None = None
    fx_rate: RateOut | None = None


class VoucherOut(BaseModel):
    id: int
    uuid: UUID
    voucher_no: str
    voucher_type: str
    voucher_date: date
    amount: AmountOut = Field(
        default=Decimal("0.00"),
        description=(
            "the voucher's debit-side total in book currency, quantized to cents"
            " by the engine — the daybook's amount column (ADR-015). A string,"
            " so the client never sums (Law 3)."
        ),
    )
    narration: str | None
    reversal_of: int | None
    posted_at: datetime
    client_token: UUID
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description="the posted document: plugin input plus computed annotations (ADR-010)",
    )
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


# ------------------------------------------------------------- currencies (#4)
class CurrencyCreate(BaseModel):
    code: CurrencyCode = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    name: str = Field(min_length=1, max_length=100)
    symbol: str | None = Field(default=None, max_length=16)
    minor_unit: int = Field(default=2, ge=0, le=8)


class CurrencyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    symbol: str | None = Field(default=None, max_length=16)
    minor_unit: int | None = Field(default=None, ge=0, le=8)


class CurrencyOut(BaseModel):
    code: str
    name: str
    symbol: str | None
    minor_unit: int
    is_system: bool


class FxRateCreate(BaseModel):
    from_ccy: CurrencyCode = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    to_ccy: CurrencyCode = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    rate: FxRateIn
    valid_on: date
    source: str | None = Field(default=None, max_length=100)


class FxRateUpdate(BaseModel):
    """A correction: same pair and day, a better number."""

    rate: FxRateIn
    source: str | None = Field(default=None, max_length=100)


class FxRateOut(BaseModel):
    id: int
    from_ccy: str
    to_ccy: str
    rate: RateOut
    valid_on: date
    source: str | None


# --------------------------------------------------------------- periods (#5)
class PeriodCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    starts_on: date
    ends_on: date


class PeriodOut(BaseModel):
    id: int
    name: str
    starts_on: date
    ends_on: date
    status: Literal["open", "locked", "closed"]
    locked_at: datetime | None
    closed_at: datetime | None
    closed_by: str | None


class FyCloseLineOut(BaseModel):
    bucket: Literal["income", "expense", "profit_loss"]
    ledger_id: int
    ledger_name: str
    dc: Dc
    amount: AmountOut


class FyCloseReportOut(BaseModel):
    as_of: date
    lines: list[FyCloseLineOut]
    net_profit: AmountOut
    totals: dict[str, AmountOut]
