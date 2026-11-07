"""Reports API: Trial Balance, Profit & Loss, Balance Sheet, FY close,
pack-registered reports (ADR-014)."""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from ..errors import NotFoundError
from ..models import CostCentre
from ..plugins import Resolver
from ..reporting import (
    balance_sheet,
    cost_category_summary,
    cost_centre_breakup,
    cost_centre_summary,
    fy_close,
    group_rollups,
    profit_loss,
    trial_balance,
)
from ..reporting.printing import render_view
from . import BookDep, ReaderDep, SessionDep

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/trial-balance")
def get_trial_balance(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    as_of: Annotated[date | None, Query()] = None,
    fast: Annotated[
        bool, Query(description="ADR-018: source sums from the balance cache (always exact)")
    ] = False,
):
    as_of = as_of or date.today()
    response = trial_balance(session, book.id, as_of, fast=fast)
    response["groups"] = group_rollups(session, book.id, as_of, fast=fast)
    return response


@router.get("/profit-loss")
def get_profit_loss(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
    fast: Annotated[
        bool, Query(description="ADR-018: S(to) − S(from − 1) from the balance cache")
    ] = False,
):
    return profit_loss(session, book.id, date_from, date_to, fast=fast)


@router.get("/balance-sheet")
def get_balance_sheet(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    as_of: Annotated[date | None, Query()] = None,
    fast: Annotated[
        bool, Query(description="ADR-018: source sums from the balance cache (always exact)")
    ] = False,
):
    as_of = as_of or date.today()
    return balance_sheet(session, book.id, as_of, fast=fast)


@router.get("/fy-close")
def get_fy_close(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    as_of: Annotated[date, Query(description="sweep as of this date (a period's last day)")],
):
    """The financial-year close preview (issue #5): the exact lines that zero
    every income/expense ledger as of ``as_of`` and move the net result to
    Profit & Loss A/c. ``POST /api/v1/periods/{id}/close`` posts them."""
    return fy_close(session, book.id, as_of)


# ------------------------------------------------------------- cost centres
@router.get("/cost-centres")
def get_cost_centre_summary(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
):
    """The cost-centre break-up's top view (ADR-025): every centre's
    allocation rollup through the closure, plus the Unallocated mass —
    allocated + unallocated is the window's whole entry mass."""
    return cost_centre_summary(session, book.id, date_from, date_to)


@router.get("/cost-centres/{centre_id}/breakup")
def get_cost_centre_breakup(
    centre_id: int,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
):
    """Ledger-wise within one centre (ADR-025): the centre's own allocations
    per ledger, with the total the shaper proves against the rows."""
    centre = session.get(CostCentre, centre_id)
    if centre is None or centre.book_id != book.id or centre.is_root:
        raise NotFoundError(f"Cost centre {centre_id} does not exist in book {book.name!r}")
    payload = cost_centre_breakup(session, book.id, centre_id, date_from, date_to)
    payload["centre_name"] = centre.name
    return payload


@router.get("/cost-categories")
def get_cost_category_summary(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
):
    """The category rollup (ADR-025): allocations through the centre→
    category edge and the category closure."""
    return cost_category_summary(session, book.id, date_from, date_to)


def _safe_stem(name: str) -> str:
    """A pack's filename hint, made download-safe (printing.py's rule)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned[:80] or "report"


@router.get("/pack/{key}")
def pack_report(
    key: str,
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
):
    """A pack-registered report (ADR-014, plugin API 0.6) — the statutory
    bridge serves ``pf`` and ``esi`` here. PDF through the shared
    print-bridge layout by default; ``?format=csv`` for the pack's machine
    format; ``?format=json`` for the pack's structured payload (plugin API
    0.8, ADR-023 — the rows a UI consumes, the same fold the PDF renders
    from). The kernel stays report-blind: the pack validates its own
    parameters (caller errors surface as 422). Reader role: reports read."""
    registry = request.app.state.plugin_manager.registry
    factory = registry.report_views.get(key)
    if factory is None:
        known = ", ".join(sorted(registry.report_views)) or "none registered"
        raise NotFoundError(f"No pack report registered for {key!r} (known: {known})")
    report = factory(Resolver(session, book), dict(request.query_params))
    stem = _safe_stem(report.filename)
    if report.data is not None and request.query_params.get("format") == "json":
        return report.data  # plugin API 0.8 (ADR-023) — the UI surface
    if report.view is not None:
        return Response(
            content=render_view(report.view),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{stem}.pdf"'},
        )
    return Response(
        content=report.csv or "",
        media_type=report.media_type,
        headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'},
    )
