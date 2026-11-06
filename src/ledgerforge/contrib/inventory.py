"""Inventory plugin pack: stock as documents on the voucher store (ADR-011).

The roadmap's promise (analysis §9) is that inventory lives ON the kernel,
not IN it — the second plugin pack after invoicing, and the first roadmap
feature to land with **zero schema change**: quantities and valuation are
*derived* from posted documents (``vouchers.meta``, ADR-010) exactly the
way reports derive from entries. Nothing is stored that can be recomputed.

Four document modes, all on ``meta.stock``:

- ``movement: "in"`` — goods receipt. One credit to the supplier (under
  *Sundry Creditors*) for the total, one debit per item on its inventory
  ledger (default: the item's name, under *Stock-in-Hand*) — the perpetual
  TallyPrime purchase shape.
- ``movement: "out"`` — goods issue. Always Dr COGS / Cr inventory at the
  **moving weighted-average cost**. With a ``party`` the commercial leg
  posts too: Dr the customer for the sale total, Cr the item's revenue
  ledger — a full perpetual sale whose gross margin is annotated.
- ``movement: "transfer"`` — the stock journal: moves an item's value
  between two inventory ledgers (godowns) at average cost. Quantities and
  the item's position are unchanged; only the value's ledger distribution
  moves. Value-preserving by construction.
- ``movement: "recount"`` — the physical-stock count: counts an item to an
  absolute quantity, valuing the difference at average cost (an explicit
  ``rate`` when counting up from zero) against a *Stock Adjustment*
  ledger.

The arithmetic contract (the part tests pin down):

- State per item is ``(qty, value)``; ``value`` only ever moves by posted
  2-dp line amounts, so issuing the last unit costs exactly the remaining
  value — a position empties to ``0.00`` with no rounding residue.
- The unit cost is ``value / qty`` at 50-digit precision; the money line
  is that times quantity, rounded half-up to 2 dp.
- The transformer and any later re-derivation run the **same fold**
  (``_apply_line``) over the same document sequence, so positions are
  deterministic replays, never stored state.
- Every item named in a draft is advisory-locked (``resolver.lock``, in
  sorted order) before the fold, so concurrent movements of one item
  serialize exactly like the engine's numbering counters.

Reversal semantics: a reversal voucher carries empty meta (ADR-010), so
``prior_documents`` returns it alongside the original; the fold skips the
undone original — an original + its reversal net to zero on stock, matching
what the mirrored entries did to the ledgers.

All money is exact ``Decimal`` (Law 3). Tax is deliberately out of scope:
the invoicing pack owns sale-side GST; purchase-side input tax posts as
its own voucher. The two packs are mutually exclusive per draft (both
veto the combination), composable across vouchers.

The ``MOVEMENTS`` list (after-commit seam) records every posted stock
document — the spot a warehouse bridge or BI sync subscribes to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, getcontext

from ..errors import ValidationError
from ..plugins import DraftLine, PluginManifest, Resolver, VoucherDraft

logger = logging.getLogger("ledgerforge.plugins.inventory")

# Exact-arithmetic headroom — same discipline as the posting engine (Law 3);
# the fold must divide at full precision wherever it runs.
getcontext().prec = 50

PLUGIN = PluginManifest(name="inventory", version="0.1.0", api_compat="0.3")

META_KEY = "stock"

# Seed groups (ledgerforge/seeding.py) — masters resolve/create through the
# book-scoped Resolver only, so everything stays per-book (ADR-009).
STOCK_GROUP = "Stock-in-Hand"  # inventory ledgers (per item / godown)
SUPPLIER_GROUP = "Sundry Creditors"  # goods-in parties
CUSTOMER_GROUP = "Sundry Debtors"  # goods-out parties
REVENUE_GROUP = "Sales Accounts"  # sale leg when party present
EXPENSE_GROUP = "Direct Expenses"  # COGS + stock adjustment
COGS_LEDGER = "Cost of Goods Sold"
ADJUSTMENT_LEDGER = "Stock Adjustment"
DEFAULT_REVENUE_LEDGER = "Sales"

#: Every stock document posted while this pack is loaded (observability for
#: tests/demos; the real-world consumer is a warehouse/BI bridge).
MOVEMENTS: list[dict] = []

CENT = Decimal("0.01")
AVG = Decimal("0.0001")  # display-only precision for unit costs

MOVEMENTS_VALID = ("in", "out", "transfer", "recount")


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.register_transformer(build_stock_document)
    registry.register_validator(no_future_document_dates)
    registry.on("voucher.posted", record_movement)


# --------------------------------------------------------------- helpers
def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _avg_str(value: Decimal) -> str:
    return str(value.quantize(AVG, rounding=ROUND_HALF_UP))


def _decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except ArithmeticError:
        raise ValidationError(f"inventory: {field} must be a number, got {value!r}") from None


def _override(item: dict, key: str) -> str | None:
    """A stripped explicit override, or None (the default path)."""
    value = item.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


@dataclass
class _ItemState:
    """Derived stock state of one item: quantity and the money posted to its
    inventory lines. ``value`` only ever moves by 2-dp line amounts, so
    emptying the position is exact by construction."""

    qty: Decimal = Decimal(0)
    value: Decimal = Decimal("0.00")


def _item_name(item: dict, position: int) -> str:
    name = str(item.get("item") or "").strip()
    if not name:
        raise ValidationError(f"inventory: items[{position}].item is required")
    return name


def _apply_line(state: _ItemState, movement: str, item: dict) -> _ItemState:
    """The single source of truth for stock arithmetic — used by the
    transformer AND by every later fold over posted documents, which is why
    positions are deterministic replays. Structural errors (bad numbers,
    missing cost basis) fire identically in both; posting-time *policy*
    (insufficient stock) is checked by the transformer before calling this.
    """
    q = _decimal(item.get("qty", 0), "items.qty")

    if movement == "in":
        if q <= 0:
            raise ValidationError("inventory: goods-in qty must be > 0")
        rate = _decimal(item.get("rate", 0), "items.rate")
        if rate < 0:
            raise ValidationError("inventory: goods-in rate must be >= 0")
        state.qty += q
        state.value += _money(q * rate)

    elif movement == "out":
        if q <= 0:
            raise ValidationError("inventory: goods-out qty must be > 0")
        if state.qty <= 0:
            raise ValidationError("inventory: no cost basis — stock position is empty")
        unit = state.value / state.qty
        cost = _money(q * unit)
        state.qty -= q
        state.value -= cost

    elif movement == "transfer":
        if q <= 0:
            raise ValidationError("inventory: transfer qty must be > 0")
        if state.qty <= 0:
            raise ValidationError("inventory: no cost basis — stock position is empty")
        # Value and quantity are unchanged; only the ledger distribution
        # moves (the caller builds the Dr/Cr pair at average cost).

    elif movement == "recount":
        if q < 0:
            raise ValidationError("inventory: counted qty must be >= 0")
        delta = q - state.qty
        if delta > 0:
            rate = item.get("rate")
            if rate is not None:
                unit = _decimal(rate, "items.rate")
            elif state.qty > 0:
                unit = state.value / state.qty
            else:
                raise ValidationError(
                    "inventory: counting up from an empty position requires an explicit rate "
                    "(no average cost exists)"
                )
            state.value += _money(delta * unit)
        elif delta < 0:
            if state.qty <= 0:
                raise ValidationError("inventory: nothing to count down — position is empty")
            unit = state.value / state.qty
            state.value -= _money((-delta) * unit)
        state.qty = q

    else:
        raise ValidationError(
            f"inventory: movement must be one of {', '.join(MOVEMENTS_VALID)}, got {movement!r}"
        )

    return state


def _fold(documents: list, item: str) -> _ItemState:
    """Replay every posted stock document of the book for ``item``, oldest
    first. Documents undone by a reversal net to zero: the reversal marks
    its original, and both are skipped."""
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    state = _ItemState()
    for d in documents:
        if d.reversal_of is not None or d.voucher_id in reversed_ids:
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict):
            continue
        movement = doc.get("movement")
        for it in doc.get("items") or []:
            if not isinstance(it, dict) or str(it.get("item") or "").strip() != item:
                continue
            _apply_line(state, str(movement), it)
    return state


@dataclass(frozen=True)
class ItemPosition:
    """Current derived position of one item — what a warehouse bridge
    recomputes, what the reports would show if stock were a ledger."""

    qty: Decimal
    value: Decimal
    avg_cost: Decimal | None  # value/qty (4 dp); None when the position is empty


def position(resolver: Resolver, item: str) -> ItemPosition:
    """The stock position of ``item`` in the resolver's book — the same fold
    the transformer runs, so it always agrees with posted documents."""
    state = _fold(resolver.prior_documents(META_KEY), item)
    avg = state.value / state.qty if state.qty > 0 else None
    return ItemPosition(
        qty=state.qty,
        value=state.value,
        avg_cost=avg.quantize(AVG, rounding=ROUND_HALF_UP) if avg is not None else None,
    )


# ------------------------------------------------------------ master helpers
def _ensure_inventory_ledger(resolver: Resolver, item_name: str, override: str | None):
    """The item's inventory ledger: default is the item's own name under
    *Stock-in-Hand*; an explicit ``inventory_ledger`` override wins (godowns).
    The auto path refuses to silently reuse an unrelated existing ledger."""
    name = override or item_name
    if override is None:
        existing = resolver.ledger(name=name)
        if existing is not None:
            stock_group = resolver.group(STOCK_GROUP)
            if stock_group is None or existing.group_id != stock_group.id:
                raise ValidationError(
                    f"inventory: stock item {item_name!r} collides with the existing ledger "
                    f"{name!r} (not under {STOCK_GROUP!r}) — set inventory_ledger explicitly"
                )
    return resolver.ensure_ledger(name, STOCK_GROUP)


def _reject_conflicts(draft: VoucherDraft) -> None:
    for other in ("gst", "invoice", "receipt"):
        if draft.meta.get(other) is not None:
            raise ValidationError(f"inventory: meta.stock and meta.{other} are mutually exclusive")
    if any(ln.currency is not None for ln in draft.lines):
        raise ValidationError(
            "inventory: meta.stock is not supported on foreign-currency lines "
            "(post FX vouchers separately)"
        )


# --------------------------------------------------------- the transformer
def build_stock_document(draft: VoucherDraft, resolver: Resolver) -> VoucherDraft:
    doc = draft.meta.get(META_KEY)
    if doc is None:
        return draft
    if not isinstance(doc, dict):
        raise ValidationError("inventory: meta.stock must be an object")

    _reject_conflicts(draft)
    movement = doc.get("movement")
    if movement not in MOVEMENTS_VALID:
        raise ValidationError(
            f"inventory: movement must be one of {', '.join(MOVEMENTS_VALID)}, got {movement!r}"
        )
    items = doc.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError("inventory: stock.items must be a non-empty list")

    names: list[str] = []
    for i, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            raise ValidationError(f"inventory: items[{i}] must be an object")
        names.append(_item_name(it, i))
    allow_negative = bool(draft.meta.get("policy:allow_negative_stock"))

    # Serialize on every item this document touches — one sorted pass, so
    # concurrent drafts never deadlock (ADR-011 lock discipline).
    for name in sorted(set(names)):
        resolver.lock(f"stock:{name}")

    documents = resolver.prior_documents(META_KEY)
    states = {name: _fold(documents, name) for name in set(names)}

    if movement == "in":
        lines, computed = _goods_in(doc, items, names, states, resolver)
    elif movement == "out":
        lines, computed = _goods_out(doc, items, names, states, resolver, allow_negative)
    elif movement == "transfer":
        lines, computed = _transfer(items, names, states, resolver)
    else:
        lines, computed = _recount(items, names, states, resolver)

    if len(lines) < 2:
        raise ValidationError("inventory: the document produced fewer than 2 entries")
    if any(ln.amount <= 0 for ln in lines):
        raise ValidationError(
            "inventory: a derived line amount is 0.00 or less — every entry must carry a "
            "positive amount (check rates and counts)"
        )
    doc["computed"] = computed
    return draft.with_lines(lines)


def _reference_note(doc: dict) -> str:
    reference = str(doc.get("reference") or "").strip()
    return f"; {reference}" if reference else ""


def _avg_after(state: _ItemState) -> str | None:
    if state.qty <= 0:
        return None
    return _avg_str(state.value / state.qty)


def _goods_in(doc, items, names, states, resolver):
    party_name = str(doc.get("party") or "").strip()
    if not party_name:
        raise ValidationError("inventory: goods-in requires party (the supplier)")

    lines: list[DraftLine] = []
    computed_items: list[dict] = []
    total = Decimal("0.00")

    for i, (item, name) in enumerate(zip(items, names, strict=True), start=1):
        state = states[name]
        rate = _decimal(item.get("rate", 0), f"items[{i}].rate")
        value_before = state.value
        _apply_line(state, "in", item)  # structural validation + state update
        amount = state.value - value_before  # exactly what the fold will record

        ledger = _ensure_inventory_ledger(resolver, name, _override(item, "inventory_ledger"))
        lines.append(
            DraftLine(
                dc="D",
                amount=amount,
                ledger_id=ledger.id,
                ledger_name=ledger.name,
                narration=f"{name} × {item.get('qty')} @ {rate}",
            )
        )
        total += amount
        computed_items.append(
            {
                "item": name,
                "line_amount": str(amount),
                "qty_after": str(state.qty),
                "avg_cost_after": _avg_after(state),
            }
        )

    party = resolver.ensure_ledger(party_name, SUPPLIER_GROUP)
    lines.insert(
        0,
        DraftLine(
            dc="C",
            amount=total,
            ledger_id=party.id,
            ledger_name=party.name,
            narration=f"goods from {party_name}{_reference_note(doc)}",
        ),
    )
    return lines, {"items": computed_items, "total": str(total)}


def _goods_out(doc, items, names, states, resolver, allow_negative):
    party_name = str(doc.get("party") or "").strip()
    lines: list[DraftLine] = []
    computed_items: list[dict] = []
    total_cost = Decimal("0.00")
    total_revenue = Decimal("0.00")

    for i, (item, name) in enumerate(zip(items, names, strict=True), start=1):
        state = states[name]
        q = _decimal(item.get("qty"), f"items[{i}].qty")
        if state.qty <= 0:
            raise ValidationError(f"inventory: no stock of {name!r} to issue (position is empty)")
        if q > state.qty and not allow_negative:
            raise ValidationError(
                f"inventory: insufficient stock for {name!r}: have {state.qty}, need {q} "
                "(override with policy:allow_negative_stock=true)"
            )
        unit = state.value / state.qty
        if unit <= 0:
            raise ValidationError(
                f"inventory: no meaningful cost basis for {name!r} "
                "(position value/quantity is not positive)"
            )
        value_before = state.value
        _apply_line(state, "out", item)
        cost = value_before - state.value  # exactly what the fold will record

        inventory = _ensure_inventory_ledger(resolver, name, _override(item, "inventory_ledger"))
        cogs = resolver.ensure_ledger(COGS_LEDGER, EXPENSE_GROUP)
        lines.append(
            DraftLine(
                dc="D",
                amount=cost,
                ledger_id=cogs.id,
                ledger_name=cogs.name,
                narration=f"COGS: {name} × {q} @ {_avg_str(unit)}",
            )
        )
        lines.append(
            DraftLine(
                dc="C",
                amount=cost,
                ledger_id=inventory.id,
                ledger_name=inventory.name,
                narration=f"issue {name} × {q} @ {_avg_str(unit)}",
            )
        )
        total_cost += cost

        if party_name:
            rate = item.get("rate")
            if rate is None:
                raise ValidationError(
                    f"inventory: items[{i}].rate (the selling rate) is required when party is set"
                )
            revenue = _money(q * _decimal(rate, f"items[{i}].rate"))
            revenue_ledger = resolver.ensure_ledger(
                str(item.get("revenue_ledger") or DEFAULT_REVENUE_LEDGER), REVENUE_GROUP
            )
            lines.append(
                DraftLine(
                    dc="C",
                    amount=revenue,
                    ledger_id=revenue_ledger.id,
                    ledger_name=revenue_ledger.name,
                    narration=f"{name} × {q} @ {rate}",
                )
            )
            total_revenue += revenue

        computed_items.append(
            {
                "item": name,
                "cost": str(cost),
                "qty_after": str(state.qty),
                "avg_cost_after": _avg_after(state),
            }
        )

    computed: dict = {"items": computed_items, "total_cost": str(total_cost)}
    if party_name:
        party = resolver.ensure_ledger(party_name, CUSTOMER_GROUP)
        lines.insert(
            0,
            DraftLine(
                dc="D",
                amount=total_revenue,
                ledger_id=party.id,
                ledger_name=party.name,
                narration=f"sale to {party_name}{_reference_note(doc)}",
            ),
        )
        computed["revenue_total"] = str(total_revenue)
        computed["gross_margin"] = str(total_revenue - total_cost)
    return lines, computed


def _transfer(items, names, states, resolver):
    lines: list[DraftLine] = []
    computed_items: list[dict] = []
    total = Decimal("0.00")

    for i, (item, name) in enumerate(zip(items, names, strict=True), start=1):
        state = states[name]
        q = _decimal(item.get("qty"), f"items[{i}].qty")
        if state.qty <= 0:
            raise ValidationError(
                f"inventory: no stock of {name!r} to transfer (position is empty)"
            )
        if q > state.qty:
            raise ValidationError(
                f"inventory: insufficient stock for {name!r}: have {state.qty}, need {q} "
                "(transfers cannot go negative)"
            )
        unit = state.value / state.qty
        if unit <= 0:
            raise ValidationError(
                f"inventory: no meaningful cost basis for {name!r} "
                "(position value/quantity is not positive)"
            )
        amount = _money(q * unit)
        _apply_line(state, "transfer", item)  # structural checks; state unchanged

        source = _ensure_inventory_ledger(resolver, name, _override(item, "inventory_ledger"))
        to_name = str(item.get("to_ledger") or "").strip()
        if not to_name:
            raise ValidationError(f"inventory: items[{i}].to_ledger is required for a transfer")
        destination = resolver.ensure_ledger(to_name, STOCK_GROUP)
        if destination.id == source.id:
            raise ValidationError(
                f"inventory: items[{i}].to_ledger must differ from the source ledger"
            )
        note = f"transfer {name} × {q} @ {_avg_str(unit)}"
        lines.append(
            DraftLine(
                dc="D",
                amount=amount,
                ledger_id=destination.id,
                ledger_name=destination.name,
                narration=note,
            )
        )
        lines.append(
            DraftLine(
                dc="C",
                amount=amount,
                ledger_id=source.id,
                ledger_name=source.name,
                narration=note,
            )
        )
        total += amount
        computed_items.append({"item": name, "amount": str(amount)})

    return lines, {"items": computed_items, "total": str(total)}


def _recount(items, names, states, resolver):
    lines: list[DraftLine] = []
    computed_items: list[dict] = []
    total_adjustment = Decimal("0.00")
    adjustment = None

    for _i, (item, name) in enumerate(zip(items, names, strict=True), start=1):
        state = states[name]
        qty_before, value_before = state.qty, state.value
        _apply_line(state, "recount", item)
        delta = state.qty - qty_before
        if delta == 0:
            continue  # counted equals book — no line, no annotation
        value_delta = state.value - value_before  # exact: state moves only by money

        if value_delta == 0:
            raise ValidationError(
                f"inventory: recount of {name!r} adjusts value by 0.00 — every entry must "
                "carry a positive amount (qty changes alone cannot post)"
            )
        adjustment = adjustment or resolver.ensure_ledger(ADJUSTMENT_LEDGER, EXPENSE_GROUP)
        inventory = _ensure_inventory_ledger(resolver, name, _override(item, "inventory_ledger"))
        amount = abs(value_delta)
        note = f"recount {name}: {qty_before} → {state.qty}"
        if delta > 0:  # found stock: Dr inventory, Cr adjustment
            lines.append(
                DraftLine(
                    dc="D",
                    amount=amount,
                    ledger_id=inventory.id,
                    ledger_name=inventory.name,
                    narration=note,
                )
            )
            lines.append(
                DraftLine(
                    dc="C",
                    amount=amount,
                    ledger_id=adjustment.id,
                    ledger_name=adjustment.name,
                    narration=note,
                )
            )
        else:  # shrinkage: Cr inventory, Dr adjustment
            lines.append(
                DraftLine(
                    dc="C",
                    amount=amount,
                    ledger_id=inventory.id,
                    ledger_name=inventory.name,
                    narration=note,
                )
            )
            lines.append(
                DraftLine(
                    dc="D",
                    amount=amount,
                    ledger_id=adjustment.id,
                    ledger_name=adjustment.name,
                    narration=note,
                )
            )
        total_adjustment += value_delta
        computed_items.append(
            {
                "item": name,
                "delta_qty": str(delta),
                "value_delta": str(value_delta),
                "qty_after": str(state.qty),
            }
        )

    if not lines:
        raise ValidationError("inventory: recount changes nothing (counted qty equals book qty)")
    return lines, {"items": computed_items, "total_adjustment": str(total_adjustment)}


# ------------------------------------------------------------ validators
def no_future_document_dates(draft: VoucherDraft, resolver: Resolver) -> None:
    if draft.meta.get(META_KEY) is None:
        return
    if draft.meta.get("policy:allow_future_document_dates"):
        return
    if draft.voucher_date > date.today():
        raise ValidationError(
            f"Policy: {draft.voucher_date.isoformat()} is in the future — stock documents "
            "post on or before today (override with policy:allow_future_document_dates=true)"
        )


# --------------------------------------------------------- event handler
def record_movement(event) -> None:  # noqa: ANN001 — DomainEvent from the stable API
    doc = (event.data.get("meta") or {}).get(META_KEY)
    if not isinstance(doc, dict):
        return
    MOVEMENTS.append(
        {
            "kind": doc.get("movement"),
            "voucher_no": event.data.get("voucher_no"),
            "book_id": event.data.get("book_id"),
            "party": doc.get("party"),
            "items": [
                {"item": str(it.get("item")), "qty": str(it.get("qty"))}
                for it in doc.get("items") or []
                if isinstance(it, dict)
            ],
        }
    )
    logger.info("stock %s posted: %s", doc.get("movement"), event.data.get("voucher_no"))
