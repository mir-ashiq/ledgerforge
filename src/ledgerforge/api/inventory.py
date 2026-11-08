"""The stock-masters API (ADR-027, roadmap Phase 6): stock items with
their tracking modes and re-order levels, the per-party date-effective
price lists, the bills of materials, and the effective-price lookup.

Roles follow the codebase's rule: reads need the reader role, writes the
bookkeeper. Every route is book-scoped through the standard header.
Creating a stock item or a BOM fires the documented ``master.created``
after-commit event (kind ``stock_item`` / ``stock_bom``) — a pack can
react to a new master exactly as it reacts to a new ledger. Price rows
are reference data (the fx-rate precedent) and fire nothing.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from .. import exporting, inventory
from . import BookDep, BookkeeperDep, ReaderDep, SessionDep

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _emit_master_created(request: Request, book, kind: str, payload: dict) -> None:
    """The documented after-commit seam — handler failures are isolated by
    the registry; a listener can never break a master's creation."""
    request.app.state.plugin_manager.emit(
        "master.created", {"kind": kind, "book_id": book.id, **payload}
    )


# ------------------------------------------------------------------- schemas
class StockItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    unit: str | None = None
    track_batches: bool = False
    track_serials: bool = False
    reorder_level: str | None = None
    reorder_quantity: str | None = None
    notes: str | None = None


class StockItemUpdate(BaseModel):
    name: str | None = None
    unit: str | None = None
    track_batches: bool | None = None
    track_serials: bool | None = None
    reorder_level: str | None = None
    reorder_quantity: str | None = None
    notes: str | None = None


class PriceCreate(BaseModel):
    item_id: int
    party_ledger_id: int | None = None
    effective_from: date
    rate: str = Field(min_length=1)


class BomLineIn(BaseModel):
    component_item_id: int
    qty_per_output: str = Field(min_length=1)


class BomCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    output_item_id: int
    output_qty: str = Field(min_length=1)
    notes: str | None = None
    components: list[BomLineIn]


class BomUpdate(BaseModel):
    name: str | None = None
    output_item_id: int | None = None
    output_qty: str | None = None
    notes: str | None = None
    components: list[BomLineIn] | None = None


def _payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(exclude_unset=True)


# -------------------------------------------------------------------- items
ITEM_COLUMNS = [
    "id",
    "name",
    "unit",
    "track_batches",
    "track_serials",
    "reorder_level",
    "reorder_quantity",
    "notes",
    "price_count",
    "bom_count",
]


@router.get("/items")
def list_items(request: Request, session: SessionDep, book: BookDep, _: ReaderDep):
    """Every stock-item master of the book, name order."""
    rows = inventory.list_items(session, book)
    exported = exporting.rows_export(request, rows, ITEM_COLUMNS, "stock-items")
    return exported if exported is not None else rows


@router.get("/items/{item_id}")
def get_item(session: SessionDep, book: BookDep, _: ReaderDep, item_id: int):
    return inventory.get_item(session, book, item_id)


@router.post("/items", status_code=201)
def create_item(
    request: Request, session: SessionDep, book: BookDep, _: BookkeeperDep, body: StockItemCreate
):
    row = inventory.create_item(session, book, body.model_dump())
    session.commit()
    _emit_master_created(request, book, "stock_item", row)
    return row


@router.patch("/items/{item_id}")
def update_item(
    session: SessionDep, book: BookDep, _: BookkeeperDep, item_id: int, body: StockItemUpdate
):
    row = inventory.update_item(session, book, item_id, _payload(body))
    session.commit()
    return row


@router.delete("/items/{item_id}")
def delete_item(session: SessionDep, book: BookDep, _: BookkeeperDep, item_id: int):
    row = inventory.delete_item(session, book, item_id)
    session.commit()
    return row


# ------------------------------------------------------------------- prices
@router.get("/prices")
def list_prices(
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    item_id: int | None = Query(default=None),
):
    """Price rows of the book — optionally one item's — the honest history."""
    rows = inventory.list_prices(session, book, item_id)
    exported = exporting.rows_export(
        request,
        rows,
        ["id", "item", "party", "from_date", "to_date", "rate"],
        "stock-prices",
    )
    return exported if exported is not None else rows


@router.post("/prices", status_code=201)
def create_price(session: SessionDep, book: BookDep, _: BookkeeperDep, body: PriceCreate):
    row = inventory.create_price(session, book, body.model_dump())
    session.commit()
    return row


@router.delete("/prices/{price_id}")
def delete_price(session: SessionDep, book: BookDep, _: BookkeeperDep, price_id: int):
    row = inventory.delete_price(session, book, price_id)
    session.commit()
    return row


# Query-default singletons (ruff B008): FastAPI's own idiom, hoisted
ITEM_Q = Query()
PARTY_Q = Query(default=None)
ON_Q = Query(default=None)


@router.get("/prices/effective")
def effective_price(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    item_id: int = ITEM_Q,
    party_ledger_id: int | None = PARTY_Q,
    on: date | None = ON_Q,
):
    """The effective rate for (item, party, date): the latest
    ``effective_from <= date`` among the applicable rows, a party-specific
    row beating the generic list price at the same date. ``rate: null``
    when no row applies — the honest "no suggestion" (the selling rate
    stays the operator's explicit entry, the matchmaker's law)."""
    item = inventory.resolve_item(session, book, item_id)
    rate = inventory.effective_price(session, book, item, party_ledger_id, on)
    return {
        "item_id": item.id,
        "item": item.name,
        "party_ledger_id": party_ledger_id,
        "date": (on or date.today()).isoformat(),
        "rate": str(rate) if rate is not None else None,
    }


# --------------------------------------------------------------------- BOMs
@router.get("/boms")
def list_boms(request: Request, session: SessionDep, book: BookDep, _: ReaderDep):
    rows = inventory.list_boms(session, book)
    exported = exporting.rows_export(
        request, rows, ["id", "item", "component_count", "notes"], "stock-boms"
    )
    return exported if exported is not None else rows


@router.get("/boms/{bom_id}")
def get_bom(session: SessionDep, book: BookDep, _: ReaderDep, bom_id: int):
    return inventory.get_bom(session, book, bom_id)


@router.post("/boms", status_code=201)
def create_bom(
    request: Request, session: SessionDep, book: BookDep, _: BookkeeperDep, body: BomCreate
):
    row = inventory.create_bom(session, book, body.model_dump())
    session.commit()
    _emit_master_created(request, book, "stock_bom", row)
    return row


@router.patch("/boms/{bom_id}")
def update_bom(session: SessionDep, book: BookDep, _: BookkeeperDep, bom_id: int, body: BomUpdate):
    payload = _payload(body)
    if body.components is not None:
        payload["components"] = [c.model_dump() for c in body.components]
    row = inventory.update_bom(session, book, bom_id, payload)
    session.commit()
    return row


@router.delete("/boms/{bom_id}")
def delete_bom(session: SessionDep, book: BookDep, _: BookkeeperDep, bom_id: int):
    row = inventory.delete_bom(session, book, bom_id)
    session.commit()
    return row
