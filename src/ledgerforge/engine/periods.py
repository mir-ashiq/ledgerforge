"""Period locking and the financial-year close (issue #5, ADR-008).

The workflow is deliberately two-step:

    open ──lock──▶ locked ──close──▶ closed
      ▲              │
      └────unlock────┘

* **lock** freezes a period's dates for review — every posting into it is
  refused (409 here, and by the ``trg_vouchers_check_period`` trigger no
  matter who writes).
* **close** is the financial-year finish: it posts the P&L sweep computed by
  ``rpt.fy_close`` as an ``FYC`` voucher dated the FIRST day of the next
  period — so the closed year's own Profit & Loss report stays intact — and
  then flips the period to ``closed``, which is final.
* The sweep's ``client_token`` is derived from the period id (uuid5), so a
  retried close replays instead of double-sweeping, and **unlock refuses once
  a sweep exists** — otherwise a reopen between sweep and close could leave a
  stale sweep (vouchers are immutable; Law 2 has no take-backs).
* With zero profit and zero balances there is nothing to sweep; the close then
  just finalizes the period (no voucher is posted).
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..errors import ConflictError, NotFoundError, PeriodStateError, ValidationError
from ..models import Book, Ledger, Period, Voucher
from ..plugins import PluginManager
from ..schemas import PeriodCreate, VoucherCreate, VoucherLineIn
from ..settings import Settings
from .posting import post_voucher

PL_LEDGER_NAME = "Profit & Loss A/c"
FYC_VOUCHER_TYPE = "FYC"


def _sweep_token(period_id: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"ledgerforge:fy-close:{period_id}")


def _get_period(session: Session, period_id: int, book: Book) -> Period:
    """A period of ``book`` — another book's period id is a 404, never a
    cross-book action (ADR-009)."""
    period = session.get(Period, period_id)
    if period is None or period.book_id != book.id:
        raise NotFoundError(f"Period {period_id} does not exist in book {book.name!r}")
    return period


def _set_actor(session: Session, actor: str | None) -> None:
    """The audit triggers read app.actor; re-issued after any internal commit."""
    if actor:
        session.execute(text("SELECT set_config('app.actor', :actor, true)"), {"actor": actor})


def resolve_pl_ledger(session: Session, book: Book) -> Ledger:
    """The sweep target: TallyPrime's equity-side Profit & Loss A/c,
    resolved within the book."""
    ledger = session.scalar(
        select(Ledger).where(Ledger.name == PL_LEDGER_NAME, Ledger.book_id == book.id)
    )
    if ledger is None:
        raise NotFoundError(
            f"Ledger {PL_LEDGER_NAME!r} does not exist in book {book.name!r} — create the "
            "book with its seed (POST /api/v1/books does this) before closing a year"
        )
    return ledger


# --------------------------------------------------------------- the workflow
def create_period(session: Session, payload: PeriodCreate, book: Book) -> Period:
    if payload.starts_on > payload.ends_on:
        raise ValidationError("period starts_on must not be after ends_on")
    period = Period(
        book_id=book.id, name=payload.name, starts_on=payload.starts_on, ends_on=payload.ends_on
    )
    session.add(period)
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        message = str(getattr(exc, "orig", exc))
        if "overlap" in message.lower():
            raise ValidationError(
                f"Period {payload.name!r} overlaps an existing period of this book — "
                "periods must not share dates"
            ) from exc
        if "duplicate key" in message.lower():
            raise ValidationError(
                f"Period name {payload.name!r} already exists in this book"
            ) from exc
        raise
    session.refresh(period)
    return period


def lock_period(session: Session, period_id: int, book: Book, actor: str | None = None) -> Period:
    period = _get_period(session, period_id, book)
    if period.status != "open":
        raise PeriodStateError(
            f"Period {period.name!r} is {period.status}; only open periods can be locked"
        )
    _set_actor(session, actor)
    period.status = "locked"
    session.commit()  # trg_periods_guard validates the transition
    session.refresh(period)
    return period


def unlock_period(session: Session, period_id: int, book: Book, actor: str | None = None) -> Period:
    period = _get_period(session, period_id, book)
    if period.status != "locked":
        raise PeriodStateError(
            f"Period {period.name!r} is {period.status}; only locked periods can be reopened"
        )
    sweep_exists = session.scalar(
        select(Voucher.id).where(Voucher.client_token == _sweep_token(period.id))
    )
    if sweep_exists is not None:
        raise ConflictError(
            f"Period {period.name!r} already has a financial-year close sweep "
            f"(voucher client_token {_sweep_token(period.id)}); it cannot be reopened — "
            "close it, or post a correcting journal if the sweep is wrong"
        )
    _set_actor(session, actor)
    period.status = "open"
    session.commit()
    session.refresh(period)
    return period


def fy_close_preview(session: Session, period: Period) -> dict:
    """The sweep rows for a period (what close would post), as of its last day."""
    return fy_close_rows(session, period.book_id, period.ends_on)


def fy_close_rows(session: Session, book_id: int, as_of: date) -> dict:
    """rpt.fy_close shaped by the reporting layer (Decimals, balance-proved)."""
    from ..reporting.reports import fy_close  # local import: reporting layer

    return fy_close(session, book_id, as_of)


def close_period(
    session: Session,
    period_id: int,
    manager: PluginManager,
    settings: Settings,
    book: Book,
    actor: str | None = None,
) -> tuple[Period, Voucher | None, bool]:
    """Lock ➜ close is the contract, within ``book``. Returns (period,
    sweep_voucher, created); ``sweep_voucher`` is None when there was
    nothing to sweep."""
    period = _get_period(session, period_id, book)
    if period.status == "closed":
        raise PeriodStateError(f"Period {period.name!r} is already closed")
    if period.status != "locked":
        raise PeriodStateError(
            f"Period {period.name!r} is open — lock it (freeze for review) before closing"
        )

    sweep_date = period.ends_on + timedelta(days=1)
    next_period = session.scalar(
        select(Period).where(
            Period.book_id == book.id,
            Period.starts_on <= sweep_date,
            Period.ends_on >= sweep_date,
        )
    )
    if next_period is None:
        raise ValidationError(
            f"No period covers {sweep_date} (the day after {period.name!r} ends), "
            "where the close sweep must be posted — create the next period first"
        )
    if next_period.status != "open":
        raise PeriodStateError(
            f"Period {next_period.name!r} (which covers the sweep date {sweep_date}) "
            f"is {next_period.status}; the sweep needs an open period"
        )

    resolve_pl_ledger(session, book)  # fail early with a clear 404 if unsweptable
    report = fy_close_rows(session, book.id, period.ends_on)

    voucher: Voucher | None = None
    created = False
    if report["lines"]:
        payload = VoucherCreate(
            voucher_type=FYC_VOUCHER_TYPE,
            voucher_date=sweep_date,
            narration=f"Financial year close — {period.name}",
            client_token=_sweep_token(period.id),
            entries=[
                VoucherLineIn(
                    ledger_id=line["ledger_id"],
                    dc=line["dc"],
                    amount=line["amount"],
                    narration=f"FY close: {line['ledger_name']}",
                )
                for line in report["lines"]
            ],
        )
        # The sweep date is the next period's first day — future-dated when a
        # year is closed early. The engine chose that date deliberately; the
        # allow_future_dates guard protects against client typos, not against
        # the close action (ADR-008).
        sweep_settings = settings.model_copy(update={"allow_future_dates": True})
        voucher, created = post_voucher(session, payload, manager, sweep_settings, book)

    # Re-issue the actor: post_voucher committed the transaction above.
    _set_actor(session, actor)
    period.status = "closed"
    period.closed_by = actor
    session.commit()  # trg_periods_guard validates the transition and stamps closed_at
    session.refresh(period)
    return period, voucher, created
