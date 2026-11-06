"""Reports API: Trial Balance, Profit & Loss, Balance Sheet, FY close."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from ..reporting import balance_sheet, fy_close, group_rollups, profit_loss, trial_balance
from . import ReaderDep, SessionDep

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/trial-balance")
def get_trial_balance(
    session: SessionDep,
    _: ReaderDep,
    as_of: Annotated[date | None, Query()] = None,
):
    as_of = as_of or date.today()
    response = trial_balance(session, as_of)
    response["groups"] = group_rollups(session, as_of)
    return response


@router.get("/profit-loss")
def get_profit_loss(
    session: SessionDep,
    _: ReaderDep,
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
):
    return profit_loss(session, date_from, date_to)


@router.get("/balance-sheet")
def get_balance_sheet(
    session: SessionDep,
    _: ReaderDep,
    as_of: Annotated[date | None, Query()] = None,
):
    as_of = as_of or date.today()
    return balance_sheet(session, as_of)


@router.get("/fy-close")
def get_fy_close(
    session: SessionDep,
    _: ReaderDep,
    as_of: Annotated[date, Query(description="sweep as of this date (a period's last day)")],
):
    """The financial-year close preview (issue #5): the exact lines that zero
    every income/expense ledger as of ``as_of`` and move the net result to
    Profit & Loss A/c. ``POST /api/v1/periods/{id}/close`` posts them."""
    return fy_close(session, as_of)
