"""The data-exchange API (ADR-030, roadmap Phase 9): the import front
door (CSV/XLSX, every kind; the TallyPrime XML migration path), and the
export catalogue the window's export pane reads.

Roles follow the codebase's rule: reads need the reader role, imports are
bookkeeper writes (they post vouchers — the heaviest write there is).
Every route is book-scoped through the standard header.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .. import dataexchange, exporting, tally_import
from ..settings import get_settings
from . import BookDep, BookkeeperDep, ReaderDep, SessionDep

router = APIRouter(prefix="/dataexchange", tags=["dataexchange"])


def _manager(request: Request):
    return request.app.state.plugin_manager


class ImportCreate(BaseModel):
    kind: Literal["masters", "openings", "vouchers", "stock", "bank"]
    format: Literal["csv", "xlsx", "mt940"] = Field(
        description="csv: the content IS csv text; xlsx: base64 of the .xlsx; "
        "mt940: bank statements (kind=bank only)"
    )
    content: str = Field(min_length=1, description="the file's text or base64")
    source: str | None = Field(
        default=None, max_length=200, description="a label for the receipt (file name…)"
    )
    dry_run: bool = Field(default=False, description="preview — the full pipeline, rolled back")
    balance_to: str | None = Field(
        default=None,
        description="openings only: a ledger name (classically 'Suspense A/c') "
        "that absorbs an unbalanced file explicitly",
    )
    ledger_id: int | None = Field(default=None, description="bank only: the bank ledger")


@router.post("/imports", status_code=201)
def post_import(
    payload: ImportCreate, request: Request, session: SessionDep, book: BookDep, _: BookkeeperDep
):
    """Import a CSV/Excel file — masters, opening balances, vouchers,
    stock masters or a bank statement. Previewable (``dry_run``), validated,
    idempotent, all-or-nothing: the engine never grows a bulk lane
    (ADR-030). The receipt is the response."""
    return dataexchange.run_import(
        session,
        book=book,
        manager=_manager(request),
        settings=get_settings(),
        kind=payload.kind,
        fmt=payload.format,
        content=payload.content,
        source=payload.source,
        dry_run=payload.dry_run,
        balance_to=payload.balance_to,
        ledger_id=payload.ledger_id,
    )


class TallyImportCreate(BaseModel):
    xml: str = Field(min_length=1, description="the TallyPrime export's XML text")
    source: str | None = Field(default=None, max_length=200)
    dry_run: bool = Field(default=False, description="preview — nothing is written")
    type_map: dict[str, str] | None = Field(
        default=None,
        description="extra Tally type → LedgerForge code mappings "
        "(e.g. {'Stock Journal': 'JV'}); unmapped types are skipped and reported",
    )
    balance_to: str | None = Field(
        default=None,
        description="absorb an unbalanced opening export explicitly (classically 'Suspense A/c')",
    )


@router.post("/tally", status_code=201)
def post_tally(
    payload: TallyImportCreate,
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: BookkeeperDep,
):
    """The TallyPrime migration path (ADR-030): the export's XML → masters,
    opening balances, vouchers (FX included) and stock item masters, one
    idempotent all-or-nothing transaction — and the verification report:
    the source trial balance (folded from the XML) against the imported
    one (folded from what landed), per ledger, difference column, every
    skip named."""
    return tally_import.import_tally(
        session,
        book=book,
        manager=_manager(request),
        settings=get_settings(),
        xml=payload.xml,
        source=payload.source,
        dry_run=payload.dry_run,
        type_map=payload.type_map,
        balance_to=payload.balance_to,
    )


# --------------------------------------------------------------- the catalogue
#: Every exportable surface (ADR-030 §3) — the window's export pane and the
#: docs read this catalogue; ``path`` is the API route whose
#: ``?format=csv|xlsx`` the seam serves, ``params`` the query every one of
#: them needs. The reports are engine folds; the lists are the models.
EXPORT_SURFACES: list[dict] = [
    {
        "key": "trial-balance",
        "label": "Trial Balance",
        "path": "/api/v1/reports/trial-balance",
        "params": ["as_of"],
        "group": "Reports",
    },
    {
        "key": "profit-loss",
        "label": "Profit & Loss",
        "path": "/api/v1/reports/profit-loss",
        "params": ["from", "to"],
        "group": "Reports",
    },
    {
        "key": "balance-sheet",
        "label": "Balance Sheet",
        "path": "/api/v1/reports/balance-sheet",
        "params": ["as_of"],
        "group": "Reports",
    },
    {
        "key": "fy-close",
        "label": "FY Close Preview",
        "path": "/api/v1/reports/fy-close",
        "params": ["as_of"],
        "group": "Reports",
    },
    {
        "key": "cost-centres",
        "label": "Cost Centre Summary",
        "path": "/api/v1/reports/cost-centres",
        "params": ["from", "to"],
        "group": "Reports",
    },
    {
        "key": "cost-categories",
        "label": "Cost Category Summary",
        "path": "/api/v1/reports/cost-categories",
        "params": ["from", "to"],
        "group": "Reports",
    },
    {
        "key": "day-book",
        "label": "Day Book (vouchers)",
        "path": "/api/v1/vouchers",
        "params": ["type", "from", "to", "limit"],
        "group": "Lists",
    },
    {
        "key": "groups",
        "label": "Groups",
        "path": "/api/v1/groups",
        "params": [],
        "group": "Masters",
    },
    {
        "key": "ledgers",
        "label": "Ledgers",
        "path": "/api/v1/ledgers",
        "params": ["search"],
        "group": "Masters",
    },
    {
        "key": "cost-centre-list",
        "label": "Cost Centres",
        "path": "/api/v1/cost-centres",
        "params": [],
        "group": "Masters",
    },
    {
        "key": "cost-category-list",
        "label": "Cost Categories",
        "path": "/api/v1/cost-categories",
        "params": [],
        "group": "Masters",
    },
    {
        "key": "currencies",
        "label": "Currencies",
        "path": "/api/v1/currencies",
        "params": [],
        "group": "Masters",
    },
    {
        "key": "fx-rates",
        "label": "FX Rates",
        "path": "/api/v1/fx-rates",
        "params": ["currency"],
        "group": "Masters",
    },
    {"key": "books", "label": "Books", "path": "/api/v1/books", "params": [], "group": "Masters"},
    {
        "key": "periods",
        "label": "Periods",
        "path": "/api/v1/periods",
        "params": [],
        "group": "Masters",
    },
    {
        "key": "bank-ledgers",
        "label": "Bank Ledgers",
        "path": "/api/v1/banking/ledgers",
        "params": [],
        "group": "Banking",
    },
    {
        "key": "gst-downloads",
        "label": "GST Portal Downloads",
        "path": "/api/v1/gst/portal/downloads",
        "params": [],
        "group": "GST",
    },
    {
        "key": "gst-lines",
        "label": "GST Portal Lines",
        "path": "/api/v1/gst/portal/lines",
        "params": ["kind", "period"],
        "group": "GST",
    },
    {
        "key": "attendance",
        "label": "Attendance Register",
        "path": "/api/v1/payroll/attendance",
        "params": ["period"],
        "group": "Payroll",
    },
    {
        "key": "inventory-items",
        "label": "Stock Items",
        "path": "/api/v1/inventory/items",
        "params": [],
        "group": "Inventory",
    },
    {
        "key": "inventory-prices",
        "label": "Stock Price List",
        "path": "/api/v1/inventory/prices",
        "params": ["item_id"],
        "group": "Inventory",
    },
    {
        "key": "inventory-boms",
        "label": "Bills of Material",
        "path": "/api/v1/inventory/boms",
        "params": [],
        "group": "Inventory",
    },
]


@router.get("/exports")
def get_exports(session: SessionDep, _: ReaderDep):
    """The export catalogue: every report and list surface that answers
    ``?format=csv|xlsx``, with the parameters each takes — the window's
    export pane renders this, and the docs quote it."""
    return {
        "formats": list(exporting.EXPORT_FORMATS),
        "surfaces": EXPORT_SURFACES,
    }
