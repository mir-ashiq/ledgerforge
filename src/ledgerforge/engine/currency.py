"""Multi-currency engine rules (issue #4, ADR-007; book-scoped by 0004).

Every book has ONE base currency (``books.base_currency``, default INR).
Every voucher entry posts a base-currency ``amount`` — that is what balances
(Law 1) and what every report aggregates. A foreign-currency line additionally
snapshots the trio (``currency``, ``amount_fx``, ``fx_rate``) on the immutable
entry row, and the engine — and, independently, the database CHECK
``chk_entry_fx_consistent`` — proves:

    amount == round_half_up(amount_fx * fx_rate, 8)

``fx_rate`` is quoted as base units per ONE unit of the foreign currency
(``USD 100 @ 83.505`` posts a base amount of ``8350.50``). Rounding is
half-away-from-zero to 8 decimal places, matching PostgreSQL's ``round()``
on numerics exactly — the engine and the database can never disagree (Law 3).

FX reference rates live per book (ADR-009): each book loads the pairs it
actually posts against.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, getcontext

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import NotFoundError, ValidationError
from ..models import Book, Currency, FxRate
from ..plugins import VoucherDraft

# Exact-arithmetic headroom (Law 3): amount_fx (≤10^18) × fx_rate (≤10^20)
# needs at most 38 significant digits.
getcontext().prec = 50

DEFAULT_BASE_CURRENCY = "INR"

#: the scale every base amount carries (NUMERIC(26,8))
BASE_SCALE = Decimal("0.00000001")


def base_currency_of(book: Book) -> str:
    """The book's base currency (books.base_currency, a 3-letter code)."""
    code = (book.base_currency or DEFAULT_BASE_CURRENCY).strip().upper()
    if len(code) != 3:
        return DEFAULT_BASE_CURRENCY
    return code


def expected_base_amount(amount_fx: Decimal, fx_rate: Decimal) -> Decimal:
    """round(amount_fx × fx_rate, 8) with PostgreSQL's numeric semantics
    (half away from zero; amounts are positive by earlier validation)."""
    return (amount_fx * fx_rate).quantize(BASE_SCALE, rounding=ROUND_HALF_UP)


def resolve_rate(
    session: Session, book: Book, from_ccy: str, to_ccy: str, on: date
) -> FxRate | None:
    """The reference rate for a pair on a day, within ``book`` (ADR-009):
    the exact row if present, else the latest one on or before the day.
    Direct pairs only — never a silent inversion (1/rate re-rounds and
    would lie about what was applied)."""
    from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
    if from_ccy == to_ccy:
        raise ValidationError("fx rate pairs must differ (from_ccy != to_ccy)")
    return session.scalar(
        select(FxRate)
        .where(
            FxRate.book_id == book.id,
            FxRate.from_ccy == from_ccy,
            FxRate.to_ccy == to_ccy,
            FxRate.valid_on <= on,
        )
        .order_by(FxRate.valid_on.desc())
        .limit(1)
    )


def validate_fx_lines(session: Session, draft: VoucherDraft, base: str) -> None:
    """The base-currency posting rules, applied to the transformed draft:

    1. all three FX fields or none (checked again post-transformer);
    2. the currency must exist and must not be the base (base lines carry no
       FX trio — ``amount`` alone is the truth);
    3. ``amount`` must equal ``round(amount_fx × fx_rate, 8)`` exactly — the
       error carries the expected figure so the client can fix and resend.
    """
    for i, line in enumerate(draft.lines, start=1):
        trio = (line.currency, line.amount_fx, line.fx_rate)
        if not any(f is not None for f in trio):
            continue
        if not all(f is not None for f in trio):
            raise ValidationError(
                f"Line {i}: foreign-currency lines need currency, amount_fx and fx_rate together"
            )
        currency, amount_fx, fx_rate = trio  # all non-None here
        code = currency.upper()
        if code == base:
            raise ValidationError(
                f"Line {i}: {base} is the base currency — post the plain amount without an FX trio"
            )
        exists = session.scalar(select(Currency.code).where(Currency.code == code))
        if exists is None:
            raise NotFoundError(
                f"Line {i}: currency {code!r} does not exist "
                f"(create it via POST /api/v1/currencies first)"
            )
        expected = expected_base_amount(amount_fx, fx_rate)
        if line.amount != expected:
            # plain decimal notation throughout — the API speaks decimal
            # strings, and str(Decimal) would render tiny values as "2E-8"
            raise ValidationError(
                f"Line {i}: base amount {format(line.amount, 'f')} does not equal "
                f"round(amount_fx × fx_rate, 8) = {format(expected, 'f')} "
                f"({format(amount_fx, 'f')} {code} @ {format(fx_rate, 'f')} {base} per {code})"
            )


def get_currency(session: Session, code: str) -> Currency:
    currency = session.get(Currency, code.upper())
    if currency is None:
        raise NotFoundError(f"Currency {code.upper()!r} does not exist")
    return currency
