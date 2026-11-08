"""The stock-masters kernel (ADR-027, roadmap Phase 6): items with their
tracking modes and re-order levels, the per-party date-effective price
lists, and the bills of materials.

Three disciplines, one module:

- **Masters are identity, not annotation.** A re-order level, a price
  row, a BOM line is referenced by name from documents and by report
  after report — the ADR-025 argument restated for master data. UNIQUE
  names per book, FK-validated components, one rate per
  (item, party-slot, date): the database's own verdicts, never
  check-then-insert races.
- **The kernel stays meta-blind (ADR-010).** Nothing here reads
  ``vouchers.meta`` — positions, batches, serials and godowns are the
  pack's fold over documents (ADR-011, extended by ADR-027 §4). The pack
  reads these tables only through the versioned plugin API's resolver
  handles (0.12), never raw.
- **Suggestions never commit (the matchmaker's law).** The effective
  price is a lookup the caller may read; the goods-out document that
  annotates it still demands the operator's explicit selling rate.

The effective-price rule, stated once: for ``(item, party, date)`` the
row with the latest ``effective_from <= date`` wins, and at the same date
a party-specific row beats the generic list price. A newer generic price
therefore overtakes an older party contract — deliberate, documented,
testable; a book that wants lock-in prices enters a new party row on the
same date.

All money is exact ``Decimal`` (Law 3); amounts cross the wire as
strings.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .errors import ConflictError, NotFoundError, ValidationError
from .models import Book, Ledger, StockBom, StockBomLine, StockItem, StockPrice

logger = logging.getLogger("ledgerforge.inventory")

_CENT = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except ArithmeticError:
        raise ValidationError(f"inventory: {field} must be a number, got {value!r}") from None
    if not parsed.is_finite():
        raise ValidationError(f"inventory: {field} must be a finite number")
    return parsed


def _required_name(value: object, field: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValidationError(f"inventory: {field} is required")
    return name


# ------------------------------------------------------------------ the items
def _item_row(item: StockItem, price_count: int, bom_count: int) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "unit": item.unit,
        "track_batches": item.track_batches,
        "track_serials": item.track_serials,
        "reorder_level": str(item.reorder_level) if item.reorder_level is not None else None,
        "reorder_quantity": (
            str(item.reorder_quantity) if item.reorder_quantity is not None else None
        ),
        "notes": item.notes,
        "price_count": price_count,
        "bom_count": bom_count,
    }


def list_items(session: Session, book: Book) -> list[dict]:
    """Every stock-item master of the book, name order — the pickers' feed."""
    from sqlalchemy import func

    prices = dict(
        session.execute(
            select(StockPrice.item_id, func.count())
            .where(StockPrice.book_id == book.id)
            .group_by(StockPrice.item_id)
        ).all()
    )
    outputs = dict(
        session.execute(
            select(StockBom.output_item_id, func.count())
            .where(StockBom.book_id == book.id)
            .group_by(StockBom.output_item_id)
        ).all()
    )
    items = session.scalars(
        select(StockItem).where(StockItem.book_id == book.id).order_by(StockItem.name)
    ).all()
    return [_item_row(i, prices.get(i.id, 0), outputs.get(i.id, 0)) for i in items]


def get_item(session: Session, book: Book, item_id: int) -> dict:
    item = resolve_item(session, book, item_id)
    return _item_row(item, *_reference_counts(session, book, item))


def _reference_counts(session: Session, book: Book, item: StockItem) -> tuple[int, int]:
    from sqlalchemy import func

    price_count = session.scalar(select(func.count()).where(StockPrice.item_id == item.id)) or 0
    bom_count = session.scalar(select(func.count()).where(StockBom.output_item_id == item.id)) or 0
    return int(price_count), int(bom_count)


def resolve_item(session: Session, book: Book, item_id: int) -> StockItem:
    item = session.get(StockItem, item_id)
    if item is None or item.book_id != book.id:
        raise NotFoundError(f"Stock item {item_id} does not exist in book {book.name!r}")
    return item


def _validate_item_fields(
    session: Session, book: Book, payload: dict, *, current: StockItem | None
) -> dict:
    """Cross-field validation shared by create and update; returns the
    cleaned column values present in the payload."""
    values: dict = {}

    if "name" in payload:
        name = _required_name(payload.get("name"), "name")
        existing = session.scalar(
            select(StockItem).where(StockItem.book_id == book.id, StockItem.name == name)
        )
        if existing is not None and (current is None or existing.id != current.id):
            raise ConflictError(
                f"inventory: stock item {name!r} already exists in book {book.name!r}"
            )
        values["name"] = name

    if "unit" in payload:
        unit = payload.get("unit")
        values["unit"] = str(unit).strip() or None if unit is not None else None

    for flag in ("track_batches", "track_serials"):
        if flag in payload:
            value = payload.get(flag)
            if not isinstance(value, bool):
                raise ValidationError(f"inventory: {flag} must be true or false")
            values[flag] = value

    for field, rule in (
        ("reorder_level", ("zero_or_positive",)),
        ("reorder_quantity", ("strictly_positive",)),
    ):
        if field in payload:
            raw = payload.get(field)
            if raw is None or str(raw).strip() == "":
                values[field] = None
                continue
            parsed = _decimal(raw, field)
            if rule[0] == "strictly_positive" and parsed <= 0:
                raise ValidationError(f"inventory: {field} must be > 0")
            if rule[0] == "zero_or_positive" and parsed < 0:
                raise ValidationError(f"inventory: {field} must be >= 0")
            values[field] = parsed

    if "notes" in payload:
        notes = payload.get("notes")
        values["notes"] = str(notes).strip() or None if notes is not None else None

    return values


def create_item(session: Session, book: Book, payload: dict) -> dict:
    values = _validate_item_fields(session, book, payload, current=None)
    if "name" not in values:
        raise ValidationError("inventory: name is required")
    item = StockItem(book_id=book.id, **values)
    session.add(item)
    session.flush()
    logger.info("stock item master created: %s (book %s)", item.name, book.name)
    return _item_row(item, 0, 0)


def update_item(session: Session, book: Book, item_id: int, payload: dict) -> dict:
    item = resolve_item(session, book, item_id)
    values = _validate_item_fields(session, book, payload, current=item)
    for key, value in values.items():
        setattr(item, key, value)
    session.flush()
    return _item_row(item, *_reference_counts(session, book, item))


def delete_item(session: Session, book: Book, item_id: int) -> dict:
    """Delete an item master. Prices cascade with it; BOM references are
    the database's own refusal (RESTRICT) — translated to the honest 409."""
    item = resolve_item(session, book, item_id)
    name = item.name
    try:
        session.delete(item)
        session.flush()
    except IntegrityError:
        session.rollback()
        raise ConflictError(
            f"inventory: stock item {name!r} is a BOM's output or component — "
            "delete or re-point the BOM first"
        ) from None
    logger.info("stock item master deleted: %s (book %s)", name, book.name)
    return {"deleted": True, "id": item_id, "name": name}


# ---------------------------------------------------------------- the prices
def _resolve_party(session: Session, book: Book, party_ledger_id: int) -> Ledger:
    ledger = session.get(Ledger, party_ledger_id)
    if ledger is None or ledger.book_id != book.id:
        raise NotFoundError(f"Party ledger {party_ledger_id} does not exist in book {book.name!r}")
    return ledger


def _price_row(session: Session, price: StockPrice) -> dict:
    item = session.get(StockItem, price.item_id)
    party = session.get(Ledger, price.party_ledger_id) if price.party_ledger_id else None
    return {
        "id": price.id,
        "item_id": price.item_id,
        "item": item.name if item else None,
        "party_ledger_id": price.party_ledger_id,
        "party": party.name if party else None,
        "effective_from": price.effective_from.isoformat(),
        "rate": str(price.rate),
    }


def list_prices(session: Session, book: Book, item_id: int | None = None) -> list[dict]:
    """Price rows of the book — optionally one item's — oldest effective
    first, the honest history of the list."""
    stmt = select(StockPrice).where(StockPrice.book_id == book.id)
    if item_id is not None:
        resolve_item(session, book, item_id)
        stmt = stmt.where(StockPrice.item_id == item_id)
    rows = session.scalars(
        stmt.order_by(StockPrice.item_id, StockPrice.effective_from, StockPrice.id)
    ).all()
    return [_price_row(session, p) for p in rows]


def create_price(session: Session, book: Book, payload: dict) -> dict:
    item_id = payload.get("item_id")
    if not isinstance(item_id, int):
        raise ValidationError("inventory: item_id is required (the stock item)")
    item = resolve_item(session, book, item_id)

    party_ledger_id = payload.get("party_ledger_id")
    party = None
    if party_ledger_id is not None:
        if not isinstance(party_ledger_id, int):
            raise ValidationError("inventory: party_ledger_id must be an integer or null")
        party = _resolve_party(session, book, party_ledger_id)

    raw_from = payload.get("effective_from")
    try:
        effective_from = (
            raw_from if isinstance(raw_from, date) else date.fromisoformat(str(raw_from))
        )
    except (TypeError, ValueError):
        raise ValidationError(
            f"inventory: effective_from must be an ISO date, got {raw_from!r}"
        ) from None

    rate = _decimal(payload.get("rate"), "rate")
    if rate <= 0:
        raise ValidationError("inventory: rate must be > 0")

    price = StockPrice(
        book_id=book.id,
        item_id=item.id,
        party_ledger_id=party.id if party else None,
        effective_from=effective_from,
        rate=rate,
    )
    session.add(price)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        whose = f"party {party.name!r}" if party else "the list price"
        raise ConflictError(
            f"inventory: {whose} already has a rate for {item.name!r} effective "
            f"{effective_from.isoformat()} — one rate per slot; revise it instead"
        ) from None
    logger.info("stock price added: %s %s @ %s", item.name, party.name if party else "(list)", rate)
    return _price_row(session, price)


def delete_price(session: Session, book: Book, price_id: int) -> dict:
    price = session.get(StockPrice, price_id)
    if price is None or price.book_id != book.id:
        raise NotFoundError(f"Price {price_id} does not exist in book {book.name!r}")
    session.delete(price)
    session.flush()
    return {"deleted": True, "id": price_id}


def effective_price(
    session: Session,
    book: Book,
    item: StockItem,
    party_ledger_id: int | None,
    on: date | None = None,
) -> Decimal | None:
    """The effective rate for ``(item, party, date)``: the latest
    ``effective_from <= on`` among the item's applicable rows, a
    party-specific row beating the generic list price at the same date.
    ``None`` when no row applies — the caller's honest "no suggestion"."""
    from sqlalchemy import case, or_

    on = on or date.today()
    # The rule (ADR-027 §1): the latest effective_from <= on wins; at the
    # same date a party-specific row beats the generic list price; the id
    # breaks the last tie deterministically. The case() is deliberate:
    # bare ``== NULL`` yields NULL, and PostgreSQL's DESC sorts NULLs
    # FIRST — the generic row would silently beat the party's contract.
    if party_ledger_id is None:
        party_first = case((StockPrice.party_ledger_id.is_(None), 1), else_=0)
    else:
        party_first = case((StockPrice.party_ledger_id == party_ledger_id, 1), else_=0)
    stmt = (
        select(StockPrice)
        .where(
            StockPrice.book_id == book.id,
            StockPrice.item_id == item.id,
            StockPrice.effective_from <= on,
            or_(
                StockPrice.party_ledger_id.is_(None),
                StockPrice.party_ledger_id == party_ledger_id,
            ),
        )
        .order_by(
            StockPrice.effective_from.desc(),
            party_first.desc(),
            StockPrice.id.desc(),
        )
        .limit(1)
    )
    price = session.scalar(stmt)
    return _money(price.rate) if price is not None else None


# ------------------------------------------------------------------ the BOMs
def _bom_component_rows(session: Session, bom: StockBom) -> list[dict]:
    lines = session.scalars(
        select(StockBomLine).where(StockBomLine.bom_id == bom.id).order_by(StockBomLine.id)
    ).all()
    rows = []
    for line in lines:
        component = session.get(StockItem, line.component_item_id)
        rows.append(
            {
                "id": line.id,
                "component_item_id": line.component_item_id,
                "component": component.name if component else None,
                "qty_per_output": str(line.qty_per_output),
            }
        )
    return rows


def _bom_row(session: Session, bom: StockBom) -> dict:
    output = session.get(StockItem, bom.output_item_id)
    return {
        "id": bom.id,
        "name": bom.name,
        "output_item_id": bom.output_item_id,
        "output": output.name if output else None,
        "output_qty": str(bom.output_qty),
        "notes": bom.notes,
        "components": _bom_component_rows(session, bom),
    }


def list_boms(session: Session, book: Book) -> list[dict]:
    boms = session.scalars(
        select(StockBom).where(StockBom.book_id == book.id).order_by(StockBom.name)
    ).all()
    return [_bom_row(session, b) for b in boms]


def get_bom(session: Session, book: Book, bom_id: int) -> dict:
    bom = _resolve_bom(session, book, bom_id)
    return _bom_row(session, bom)


def _resolve_bom(session: Session, book: Book, bom_id: int) -> StockBom:
    bom = session.get(StockBom, bom_id)
    if bom is None or bom.book_id != book.id:
        raise NotFoundError(f"BOM {bom_id} does not exist in book {book.name!r}")
    return bom


def _validate_bom_components(
    session: Session, book: Book, components: object, output_id: int
) -> list[tuple[int, Decimal]]:
    if not isinstance(components, list) or not components:
        raise ValidationError("inventory: a BOM needs at least one component")
    seen: dict[int, Decimal] = {}
    for i, entry in enumerate(components, start=1):
        if not isinstance(entry, dict):
            raise ValidationError(f"inventory: components[{i}] must be an object")
        component_id = entry.get("component_item_id")
        if not isinstance(component_id, int):
            raise ValidationError(f"inventory: components[{i}].component_item_id is required")
        component = resolve_item(session, book, component_id)
        if component.id == output_id:
            raise ValidationError(
                "inventory: a BOM's component cannot be its own output "
                "(production cannot consume itself)"
            )
        qty = _decimal(entry.get("qty_per_output"), f"components[{i}].qty_per_output")
        if qty <= 0:
            raise ValidationError(f"inventory: components[{i}].qty_per_output must be > 0")
        if component.id in seen:
            raise ValidationError(
                f"inventory: component {component.name!r} appears twice — one line per component"
            )
        seen[component.id] = qty
    return list(seen.items())


def create_bom(session: Session, book: Book, payload: dict) -> dict:
    name = _required_name(payload.get("name"), "name")
    existing = session.scalar(
        select(StockBom).where(StockBom.book_id == book.id, StockBom.name == name)
    )
    if existing is not None:
        raise ConflictError(f"inventory: BOM {name!r} already exists in book {book.name!r}")

    output_id = payload.get("output_item_id")
    if not isinstance(output_id, int):
        raise ValidationError("inventory: output_item_id is required (the produced item)")
    output = resolve_item(session, book, output_id)
    output_qty = _decimal(payload.get("output_qty"), "output_qty")
    if output_qty <= 0:
        raise ValidationError("inventory: output_qty must be > 0")

    components = _validate_bom_components(session, book, payload.get("components"), output.id)

    bom = StockBom(
        book_id=book.id,
        name=name,
        output_item_id=output.id,
        output_qty=output_qty,
        notes=str(payload.get("notes") or "").strip() or None,
    )
    session.add(bom)
    session.flush()
    for component_id, qty in components:
        session.add(
            StockBomLine(
                book_id=book.id,
                bom_id=bom.id,
                component_item_id=component_id,
                qty_per_output=qty,
            )
        )
    session.flush()
    logger.info("BOM created: %s → %s × %s", name, output.name, output_qty)
    return _bom_row(session, bom)


def update_bom(session: Session, book: Book, bom_id: int, payload: dict) -> dict:
    """Revise a BOM: name, output, quantity and/or the whole component
    set (components replace atomically — a recipe is one decision)."""
    bom = _resolve_bom(session, book, bom_id)

    if "name" in payload:
        name = _required_name(payload.get("name"), "name")
        existing = session.scalar(
            select(StockBom).where(StockBom.book_id == book.id, StockBom.name == name)
        )
        if existing is not None and existing.id != bom.id:
            raise ConflictError(f"inventory: BOM {name!r} already exists in book {book.name!r}")
        bom.name = name

    output = session.get(StockItem, bom.output_item_id)
    if "output_item_id" in payload:
        output_id = payload.get("output_item_id")
        if not isinstance(output_id, int):
            raise ValidationError("inventory: output_item_id must be an integer")
        output = resolve_item(session, book, output_id)
        bom.output_item_id = output.id
    if "output_qty" in payload:
        output_qty = _decimal(payload.get("output_qty"), "output_qty")
        if output_qty <= 0:
            raise ValidationError("inventory: output_qty must be > 0")
        bom.output_qty = output_qty
    if "notes" in payload:
        bom.notes = str(payload.get("notes") or "").strip() or None

    if "components" in payload:
        # re-validate against the (possibly new) output before replacing
        components = _validate_bom_components(
            session, book, payload.get("components"), bom.output_item_id
        )
        for line in session.scalars(
            select(StockBomLine).where(StockBomLine.bom_id == bom.id)
        ).all():
            session.delete(line)
        session.flush()
        for component_id, qty in components:
            session.add(
                StockBomLine(
                    book_id=book.id,
                    bom_id=bom.id,
                    component_item_id=component_id,
                    qty_per_output=qty,
                )
            )
    session.flush()
    return _bom_row(session, bom)


def delete_bom(session: Session, book: Book, bom_id: int) -> dict:
    bom = _resolve_bom(session, book, bom_id)
    name = bom.name
    session.delete(bom)  # lines cascade
    session.flush()
    logger.info("BOM deleted: %s (book %s)", name, book.name)
    return {"deleted": True, "id": bom_id, "name": name}


# ------------------------------------------------- the pack's read surfaces
def item_master_by_name(session: Session, book: Book, name: str) -> StockItem | None:
    """The item master a document names, or None — the resolver handle's
    engine side (plugin API 0.12). Book-scoped, exactly like ledger()."""
    return session.scalar(
        select(StockItem).where(StockItem.book_id == book.id, StockItem.name == name)
    )


def bom_by_name(session: Session, book: Book, name: str) -> StockBom | None:
    """The BOM a manufacturing journal names, or None (plugin API 0.12)."""
    return session.scalar(
        select(StockBom).where(StockBom.book_id == book.id, StockBom.name == name)
    )


def bom_components(session: Session, bom: StockBom) -> list[tuple[int, str, Decimal]]:
    """The BOM's components as (item_id, item_name, qty_per_output),
    insertion order — the recipe the journal validates against."""
    rows = session.scalars(
        select(StockBomLine).where(StockBomLine.bom_id == bom.id).order_by(StockBomLine.id)
    ).all()
    resolved = []
    for line in rows:
        item = session.get(StockItem, line.component_item_id)
        resolved.append((line.component_item_id, item.name if item else "", line.qty_per_output))
    return resolved
