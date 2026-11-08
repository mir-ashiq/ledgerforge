"""Reports API: Trial Balance, Profit & Loss, Balance Sheet, FY close,
pack-registered reports (ADR-014)."""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from .. import exporting
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


def _export(request, payload, projection, stem):
    """ADR-030's format seam: ``?format=csv|xlsx`` on a report surface."""
    fmt = exporting.export_format(request)
    if fmt is None:
        return None
    columns, rows = projection(payload)
    return exporting.export_response(fmt, columns=columns, rows=rows, stem=stem)


@router.get("/trial-balance")
def get_trial_balance(
    request: Request,
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
    exported = _export(request, response, exporting.trial_balance_projection, "trial-balance")
    return exported if exported is not None else response


@router.get("/profit-loss")
def get_profit_loss(
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
    fast: Annotated[
        bool, Query(description="ADR-018: S(to) − S(from − 1) from the balance cache")
    ] = False,
):
    payload = profit_loss(session, book.id, date_from, date_to, fast=fast)
    exported = _export(request, payload, exporting.profit_loss_projection, "profit-loss")
    return exported if exported is not None else payload


@router.get("/balance-sheet")
def get_balance_sheet(
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    as_of: Annotated[date | None, Query()] = None,
    fast: Annotated[
        bool, Query(description="ADR-018: source sums from the balance cache (always exact)")
    ] = False,
):
    as_of = as_of or date.today()
    payload = balance_sheet(session, book.id, as_of, fast=fast)
    exported = _export(request, payload, exporting.balance_sheet_projection, "balance-sheet")
    return exported if exported is not None else payload


@router.get("/fy-close")
def get_fy_close(
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    as_of: Annotated[date, Query(description="sweep as of this date (a period's last day)")],
):
    """The financial-year close preview (issue #5): the exact lines that zero
    every income/expense ledger as of ``as_of`` and move the net result to
    Profit & Loss A/c. ``POST /api/v1/periods/{id}/close`` posts them."""
    payload = fy_close(session, book.id, as_of)
    exported = _export(request, payload, exporting.fy_close_projection, "fy-close")
    return exported if exported is not None else payload


# ------------------------------------------------------------- cost centres
@router.get("/cost-centres")
def get_cost_centre_summary(
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
):
    """The cost-centre break-up's top view (ADR-025): every centre's
    allocation rollup through the closure, plus the Unallocated mass —
    allocated + unallocated is the window's whole entry mass."""
    payload = cost_centre_summary(session, book.id, date_from, date_to)
    exported = _export(request, payload, exporting.cost_summary_projection, "cost-centres")
    return exported if exported is not None else payload


@router.get("/cost-centres/{centre_id}/breakup")
def get_cost_centre_breakup(
    centre_id: int,
    request: Request,
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
    exported = _export(
        request, payload, exporting.cost_breakup_projection, f"cost-breakup-{centre.name}"
    )
    return exported if exported is not None else payload


@router.get("/cost-categories")
def get_cost_category_summary(
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
):
    """The category rollup (ADR-025): allocations through the centre→
    category edge and the category closure."""
    payload = cost_category_summary(session, book.id, date_from, date_to)
    exported = _export(request, payload, exporting.cost_summary_projection, "cost-categories")
    return exported if exported is not None else payload


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
    # ADR-030: the format param is the SEAM's vocabulary; the pack speaks
    # pdf/csv/json. xlsx asks the pack for its CSV (the columns verbatim)
    # and re-renders it — one seam, every pack. Every other format passes
    # through to the pack untouched (the pre-seam contract).
    query = dict(request.query_params)
    requested = query.pop("format", None)
    if requested == "xlsx":
        query["format"] = "csv"
    elif requested is not None:
        query["format"] = requested
    report = factory(Resolver(session, book), query)
    stem = _safe_stem(report.filename)
    if report.data is not None and requested == "json":
        return report.data  # plugin API 0.8 (ADR-023) — the UI surface
    if requested == "xlsx":
        # the pack's own CSV re-rendered as xlsx — the pack's columns
        # verbatim, one seam for every pack.
        if report.csv is None:
            raise NotFoundError(f"pack report {key!r} has no tabular form to export as xlsx")
        columns, rows = exporting.csv_to_rows(report.csv)
        return exporting.export_response("xlsx", columns=columns, rows=rows, stem=stem, sheet=stem)
    if requested == "csv" and report.csv is not None:
        # the machine format the docstring always promised, honoured even
        # when a print view exists (ADR-026: the reconciliation CSV)
        return Response(
            content=report.csv,
            media_type=report.media_type,
            headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'},
        )
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
