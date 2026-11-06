"""Periods API: fiscal periods, locking and the financial-year close (issue #5).

Mutations are admin-gated: freezing or closing a year is a book-wide control
action, not a bookkeeping one. The posting gate itself lives in the engine
(and in the database), so a reader-level caller can never sneak past a lock
by any path.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..engine.periods import (
    close_period,
    create_period,
    fy_close_preview,
    lock_period,
    unlock_period,
)
from ..models import Period
from ..plugins import PluginManager
from ..schemas import FyCloseReportOut, PeriodCreate, PeriodOut
from ..settings import get_settings
from . import AdminDep, ReaderDep, SessionDep

router = APIRouter(prefix="/periods", tags=["periods"])


def _period_out(period: Period) -> PeriodOut:
    return PeriodOut(
        id=period.id,
        name=period.name,
        starts_on=period.starts_on,
        ends_on=period.ends_on,
        status=period.status,
        locked_at=period.locked_at,
        closed_at=period.closed_at,
        closed_by=period.closed_by,
    )


def _plugin_manager(request: Request) -> PluginManager:
    return request.app.state.plugin_manager


@router.post("", response_model=PeriodOut, status_code=201)
def post_period(payload: PeriodCreate, session: SessionDep, _: AdminDep):
    return _period_out(create_period(session, payload))


@router.get("", response_model=list[PeriodOut])
def list_periods(session: SessionDep, _: ReaderDep):
    periods = session.query(Period).order_by(Period.starts_on).all()
    return [_period_out(p) for p in periods]


@router.get("/{period_id}", response_model=PeriodOut)
def get_period(period_id: int, session: SessionDep, _: ReaderDep):
    from ..errors import NotFoundError

    period = session.get(Period, period_id)
    if period is None:
        raise NotFoundError(f"Period {period_id} does not exist")
    return _period_out(period)


@router.post("/{period_id}/lock", response_model=PeriodOut)
def lock(period_id: int, session: SessionDep, principal: AdminDep):
    """Freeze a period's dates for review; postings into it get 409."""
    return _period_out(lock_period(session, period_id, actor=principal.sub))


@router.post("/{period_id}/unlock", response_model=PeriodOut)
def unlock(period_id: int, session: SessionDep, principal: AdminDep):
    """Reopen a locked period. Refused once a close sweep exists."""
    return _period_out(unlock_period(session, period_id, actor=principal.sub))


@router.get("/{period_id}/close-preview", response_model=FyCloseReportOut)
def close_preview(period_id: int, session: SessionDep, _: ReaderDep):
    """Exactly what close would post: every income/expense ledger's zeroing
    line plus the Profit & Loss A/c balancing line, as of the period's end."""
    from ..errors import NotFoundError

    period = session.get(Period, period_id)
    if period is None:
        raise NotFoundError(f"Period {period_id} does not exist")
    return fy_close_preview(session, period)


@router.post("/{period_id}/close")
def close(
    period_id: int,
    request: Request,
    session: SessionDep,
    principal: AdminDep,
):
    """Lock first, then close. Posts the FY sweep as an FYC voucher dated the
    first day of the next period (which must exist and be open), then marks
    the period closed — final. Idempotent on retry via the sweep's
    deterministic client_token."""
    period, voucher, created = close_period(
        session,
        period_id,
        _plugin_manager(request),
        get_settings(),
        actor=principal.sub,
    )
    from ..api.vouchers import _voucher_out

    return {
        "period": _period_out(period),
        "sweep_voucher": _voucher_out(session, voucher) if voucher is not None else None,
        "sweep_posted": created,
    }
