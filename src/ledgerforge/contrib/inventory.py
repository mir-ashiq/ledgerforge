"""Inventory plugin pack: stock as documents on the voucher store (ADR-011),
with the Phase 6 depth — batches, serials, expiry, godowns, re-order levels
and manufacturing (ADR-027).

The roadmap's promise (analysis §9) is that inventory lives ON the kernel,
not IN it — the second plugin pack after invoicing, and the first roadmap
feature to land with **zero schema change**: quantities and valuation are
*derived* from posted documents (``vouchers.meta``, ADR-010) exactly the
way reports derive from entries. Nothing is stored that can be recomputed.

Five document modes, all on ``meta.stock``:

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
- ``movement: "manufacture"`` — the manufacturing journal (ADR-027 §5):
  consumes components at their moving averages, produces the finished
  good at the rolled-up cost (material + the operator's explicit charge
  legs), optionally validated against a named BOM.

The arithmetic contract (the part tests pin down):

- State per item is ``(qty, value)``; ``value`` only ever moves by posted
  2-dp line amounts, so issuing the last unit costs exactly the remaining
  value — a position empties to ``0.00`` with no residue (Law 3). Every
  later re-derivation runs the **same fold** (``_apply_line`` /
  ``_apply_manufacture``) over the same document sequence, so a position
  is a deterministic replay, never stored state.
- **Valuation stays the moving weighted average at the item level**
  (ADR-027): batches, serials, expiry and godowns are *quantity
  sub-ledgers* of the same position — they never carry value of their
  own, so batch-wise issue can never fork the cost arithmetic.

The depth grammar (ADR-027 §3) — optional keys on every item line:

- ``godown`` — the location of the movement's item side (default
  ``"Main location"``); the fold tracks ``(item, godown) → qty`` and the
  guards refuse what the position refuses.
- ``batch`` — the batch identity. Receipts create or add to a batch (the
  first receipt fixes its ``expiry``); issues without a batch are
  **FEFO-allocated** across the earliest-expiry batches (deterministic,
  recorded in ``computed``).
- ``expiry`` — the batch's expiry date, goods-in only, batch only.
- ``serials`` — the serial numbers moving; ``len(serials) == qty``; a
  serial is one physical thing (duplicates are a 422, issuing a serial
  that is not in stock is a 422).
- ``to_godown`` — the transfer's destination location.

Tracking modes are enforced when a stock-item **master** exists (kernel
tables, read through plugin API 0.12): ``track_batches`` makes batch
mandatory on goods-in (identity is never auto-invented) and FEFO-automatic
on issues; ``track_serials`` makes serials mandatory on both sides.
Masters also carry the re-order levels (the shortfall report) and anchor
the price lists (the goods-out ``computed`` annotates the effective rate —
informational, never applied; the matchmaker's law).

Reversal semantics: a reversal voucher carries empty meta (ADR-010), so
``prior_documents`` returns it alongside the original; the fold skips the
undone original — an original + its reversal net to zero on stock AND on
every sub-ledger (batches, godowns, serials), matching what the mirrored
entries did to the ledgers.

All money is exact ``Decimal`` (Law 3). Tax is deliberately out of scope:
the invoicing pack owns sale-side GST; purchase-side input tax posts as
its own voucher. The two packs are mutually exclusive per draft (both
veto the combination), composable across vouchers.

The ``MOVEMENTS`` list (after-commit seam) records every posted stock
document — the spot a warehouse bridge or BI sync subscribes to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import MAXYEAR, date
from decimal import ROUND_HALF_UP, Decimal, getcontext

from ..errors import ValidationError
from ..plugins import (
    DocumentColumn,
    DocumentContext,
    DocumentSection,
    DocumentView,
    DraftLine,
    PackReport,
    PluginManifest,
    Resolver,
    VoucherDraft,
)
from ..reporting.printing import format_amount

logger = logging.getLogger("ledgerforge.plugins.inventory")

# Exact-arithmetic headroom — same discipline as the posting engine (Law 3);
# the fold must divide at full precision wherever it runs.
getcontext().prec = 50

PLUGIN = PluginManifest(name="inventory", version="0.2.0", api_compat="0.12")

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

#: TallyPrime's own default location — the godown every godown-naive
#: document (the entire pre-depth history) implicitly posts into.
DEFAULT_GODOWN = "Main location"

#: Every stock document posted while this pack is loaded (observability for
#: tests/demos; the real-world consumer is a warehouse/BI bridge).
MOVEMENTS: list[dict] = []

CENT = Decimal("0.01")
AVG = Decimal("0.0001")  # display-only precision for unit costs

MOVEMENTS_VALID = ("in", "out", "transfer", "recount", "manufacture")

#: The FEFO sort key for a batch that never expires (sorted after every
#: dated batch — undated stock is consumed last).
_FAR_FUTURE = date(MAXYEAR, 12, 31)


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.register_transformer(build_stock_document)
    registry.register_validator(no_future_document_dates)
    registry.on("voucher.posted", record_movement)
    registry.register_document_view(META_KEY, stock_view)
    registry.register_report("stock-position", stock_position_report)
    registry.register_report("stock-godown", stock_godown_report)
    registry.register_report("stock-batch", stock_batch_report)
    registry.register_report("stock-expiry", stock_expiry_report)
    registry.register_report("stock-shortfall", stock_shortfall_report)


# --------------------------------------------------------------- helpers
def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _avg_str(value: Decimal) -> str:
    return str(value.quantize(AVG, rounding=ROUND_HALF_UP))


def _plain(value: Decimal) -> str:
    return f"{value.normalize():f}"


def _decimal(value: object, field_: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except ArithmeticError:
        raise ValidationError(f"inventory: {field_} must be a number, got {value!r}") from None
    if not parsed.is_finite():
        raise ValidationError(f"inventory: {field_} must be a finite number")
    return parsed


def _override(item: dict, key: str) -> str | None:
    """A stripped explicit override, or None (the default path)."""
    value = item.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _parse_expiry(raw: object, where: str) -> date:
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        raise ValidationError(
            f"inventory: {where} must be an ISO date (YYYY-MM-DD), got {raw!r}"
        ) from None


# ------------------------------------------------------- the depth state
@dataclass
class _BatchState:
    """One batch of one item: quantity, its fixed expiry (the first
    receipt's), and the godown split. A quantity sub-ledger only — value
    lives with the item (ADR-027)."""

    qty: Decimal = Decimal(0)
    expiry: date | None = None
    godowns: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class _SerialRef:
    """Where one serial number sits: its batch (when the item tracks
    batches) and its godown."""

    batch: str | None
    godown: str


@dataclass
class _ItemState:
    """Derived stock state of one item: quantity and the money posted to its
    inventory lines — plus the Phase 6 quantity sub-ledgers (godowns,
    batches, serials). ``value`` only ever moves by 2-dp line amounts, so
    emptying the position is exact by construction."""

    qty: Decimal = Decimal(0)
    value: Decimal = Decimal("0.00")
    godowns: dict[str, Decimal] = field(default_factory=dict)
    batches: dict[str, _BatchState] = field(default_factory=dict)
    serials: dict[str, _SerialRef] = field(default_factory=dict)

    def godown_qty(self, godown: str) -> Decimal:
        return self.godowns.get(godown, Decimal(0))

    def batch_state(self, batch: str) -> _BatchState:
        state = self.batches.get(batch)
        if state is None:
            raise ValidationError(
                f"inventory: unknown batch {batch!r} — the position holds "
                f"{', '.join(sorted(self.batches)) or 'no batches'}"
            )
        return state


def _item_name(item: dict, position: int) -> str:
    name = str(item.get("item") or "").strip()
    if not name:
        raise ValidationError(f"inventory: items[{position}].item is required")
    return name


def _line_godown(item: dict, position: int, key: str = "godown") -> str:
    """The line's location key — stripped, defaulting to the main location."""
    value = _override(item, key)
    return value if value is not None else DEFAULT_GODOWN


def _line_serials(item: dict, position: int) -> list[str]:
    """The serial numbers a line moves: non-empty strings, no duplicates,
    exactly one per unit of quantity."""
    raw = item.get("serials")
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ValidationError(f"inventory: items[{position}].serials must be a non-empty list")
    serials: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        serial = str(entry or "").strip()
        if not serial:
            raise ValidationError(
                f"inventory: items[{position}].serials contains an empty serial number"
            )
        if serial in seen:
            raise ValidationError(
                f"inventory: items[{position}].serials lists {serial!r} twice — "
                "a serial is one physical thing"
            )
        seen.add(serial)
        serials.append(serial)
    return serials


def _fefo_allocations(state: _ItemState, qty: Decimal) -> list[tuple[str, Decimal]]:
    """Split ``qty`` across the item's batches, earliest expiry first (FEFO),
    ties by batch name; undated batches last. Pure and deterministic — the
    transformer records exactly what the fold will replay."""
    remaining = qty
    allocations: list[tuple[str, Decimal]] = []
    ordered = sorted(state.batches.items(), key=lambda kv: (kv[1].expiry or _FAR_FUTURE, kv[0]))
    for batch, bstate in ordered:
        if remaining <= 0:
            break
        if bstate.qty <= 0:
            continue
        take = min(remaining, bstate.qty)
        allocations.append((batch, take))
        remaining -= take
    if remaining > 0 and not allocations:
        return []
    return allocations


def _apply_batch_in(
    state: _ItemState,
    batch_name: str,
    qty: Decimal,
    expiry: date | None,
    godown: str,
    tolerant: bool = False,
) -> None:
    """Goods-in of one batch: create or add, the first receipt fixing the
    expiry (later receipts must agree or omit)."""
    bstate = state.batches.get(batch_name)
    if bstate is None:
        bstate = _BatchState(expiry=expiry)
        state.batches[batch_name] = bstate
    elif expiry is not None and bstate.expiry is not None and expiry != bstate.expiry:
        if not tolerant:
            raise ValidationError(
                f"inventory: batch {batch_name!r} expires {bstate.expiry.isoformat()} — "
                f"a receipt dated {expiry.isoformat()} cannot redefine it "
                "(receive under a new batch name)"
            )
        bstate.expiry = expiry
    bstate.qty += qty
    bstate.godowns[godown] = bstate.godowns.get(godown, Decimal(0)) + qty


def _apply_batch_out(
    state: _ItemState, batch_name: str, qty: Decimal, godown: str, tolerant: bool = False
) -> None:
    """Issue from one named batch at one godown — structural at posting: the
    batch must exist and hold the quantity. ``tolerant`` (the replay mode)
    absorbs reversal-orphaned history: an unknown batch is skipped, an
    overdrawn one clamps — the item-level arithmetic stays primary and the
    divergence is exactly the orphan the position reports."""
    bstate = state.batches.get(batch_name)
    if bstate is None:
        if not tolerant:
            raise ValidationError(
                f"inventory: unknown batch {batch_name!r} — the position holds "
                f"{', '.join(sorted(state.batches)) or 'no batches'}"
            )
        return
    at_godown = bstate.godowns.get(godown, Decimal(0))
    if at_godown < qty:
        if not tolerant:
            raise ValidationError(
                f"inventory: batch {batch_name!r} holds {at_godown} at {godown!r}, "
                f"the line takes {qty}"
            )
        qty = max(at_godown, Decimal(0))
    bstate.godowns[godown] = at_godown - qty
    if bstate.godowns[godown] == 0:
        del bstate.godowns[godown]
    bstate.qty -= qty
    if bstate.qty == 0:
        # fully consumed: the batch leaves the position (qty is the truth)
        state.batches.pop(batch_name, None)


def _apply_serials_in(
    state: _ItemState, serials: list[str], batch: str | None, godown: str, tolerant: bool = False
) -> None:
    for serial in serials:
        if serial in state.serials and not tolerant:
            raise ValidationError(
                f"inventory: serial {serial!r} is already in stock — "
                "duplicates are a data error, not an override policy"
            )
        state.serials[serial] = _SerialRef(batch=batch, godown=godown)


def _apply_serials_out(
    state: _ItemState, serials: list[str], batch: str | None, godown: str, tolerant: bool = False
) -> None:
    for serial in serials:
        ref = state.serials.get(serial)
        if ref is None:
            if not tolerant:
                raise ValidationError(
                    f"inventory: serial {serial!r} is not in stock "
                    f"(the item holds {len(state.serials)})"
                )
            continue  # the replay's orphan: the supply was reversed away
        if not tolerant:
            if batch is not None and ref.batch is not None and ref.batch != batch:
                raise ValidationError(
                    f"inventory: serial {serial!r} sits in batch {ref.batch!r}, not {batch!r}"
                )
            if ref.godown != godown:
                raise ValidationError(
                    f"inventory: serial {serial!r} sits at godown {ref.godown!r}, not {godown!r}"
                )
        del state.serials[serial]


def _apply_line(state: _ItemState, movement: str, item: dict, tolerant: bool = False) -> _ItemState:
    """The single source of truth for stock arithmetic — used by the
    transformer AND by every later fold over posted documents, which is why
    positions are deterministic replays. Structural errors (bad numbers,
    missing cost basis, impossible sub-ledger moves) fire at posting;
    ``tolerant`` (the replay mode) absorbs reversal-orphaned history — a
    later reversal of a goods-in whose stock was already issued leaves an
    orphaned issue, and the replay of that history applies the overdraw
    (quantity negative, no cost basis) instead of refusing the whole book:
    the position reports the orphan, the posting guards stop new ones.

    The ``(qty, value)`` arithmetic of the four ADR-011 movements is
    UNCHANGED — depth keys only steer the quantity sub-ledgers."""
    q = _decimal(item.get("qty", 0), "items.qty")

    if movement == "in":
        if q <= 0:
            raise ValidationError("inventory: goods-in qty must be > 0")
        rate = _decimal(item.get("rate", 0), "items.rate")
        if rate < 0:
            raise ValidationError("inventory: goods-in rate must be >= 0")
        state.qty += q
        state.value += _money(q * rate)
        godown = _line_godown(item, 0)
        state.godowns[godown] = state.godown_qty(godown) + q
        batch = _override(item, "batch")
        expiry_raw = item.get("expiry")
        if expiry_raw is not None and batch is None:
            raise ValidationError("inventory: expiry rides a batch — set items[].batch too")
        if batch is not None:
            expiry = _parse_expiry(expiry_raw, "items[].expiry") if expiry_raw else None
            _apply_batch_in(state, batch, q, expiry, godown)
        serials = _line_serials(item, 0)
        if serials:
            if Decimal(len(serials)) != q:
                raise ValidationError(
                    f"inventory: {len(serials)} serials cannot move a qty of {q} — "
                    "one serial per unit"
                )
            _apply_serials_in(state, serials, batch, godown)

    elif movement == "out":
        if q <= 0:
            raise ValidationError("inventory: goods-out qty must be > 0")
        godown = _line_godown(item, 0)
        batch = _override(item, "batch")
        serials = _line_serials(item, 0)
        if serials and Decimal(len(serials)) != q:
            raise ValidationError(
                f"inventory: {len(serials)} serials cannot move a qty of {q} — one serial per unit"
            )
        if state.qty <= 0:
            # posting refuses an empty position (no cost basis); a tolerant
            # replay of orphaned history applies the overdraw at zero cost
            if not tolerant:
                raise ValidationError("inventory: no cost basis — stock position is empty")
            state.qty -= q
            state.godowns[godown] = state.godown_qty(godown) - q
            if serials:
                _apply_serials_out(state, serials, batch, godown, tolerant=True)
            elif batch is not None:
                _apply_batch_out(state, batch, q, godown, tolerant=True)
            return state
        unit = state.value / state.qty
        cost = _money(q * unit)
        state.qty -= q
        state.value -= cost
        state.godowns[godown] = state.godown_qty(godown) - q
        if serials:
            _apply_serials_out(state, serials, batch, godown, tolerant=tolerant)
        elif batch is not None:
            _apply_batch_out(state, batch, q, godown, tolerant=tolerant)
        elif state.batches:
            # FEFO: the earliest-expiry batches go first (ADR-027 §3) —
            # the same deterministic split the fold replays.
            for batch_name, take in _fefo_allocations(state, q):
                _apply_batch_out(state, batch_name, take, godown, tolerant=tolerant)

    elif movement == "transfer":
        if q <= 0:
            raise ValidationError("inventory: transfer qty must be > 0")
        if state.qty <= 0 and not tolerant:
            raise ValidationError("inventory: no cost basis — stock position is empty")
        godown = _line_godown(item, 0)
        to_godown = _line_godown(item, 0, "to_godown")
        batch = _override(item, "batch")
        serials = _line_serials(item, 0)
        if godown != DEFAULT_GODOWN or to_godown != DEFAULT_GODOWN or batch or serials:
            # godown-aware relocation: quantity follows the value pair
            if state.godown_qty(godown) < q and not tolerant:
                raise ValidationError(
                    f"inventory: {godown!r} holds {state.godown_qty(godown)} of "
                    f"this item, the transfer moves {q} — locations never go negative"
                )
            state.godowns[godown] = state.godown_qty(godown) - q
            state.godowns[to_godown] = state.godown_qty(to_godown) + q
            if serials:
                if Decimal(len(serials)) != q:
                    raise ValidationError(
                        f"inventory: {len(serials)} serials cannot move a qty of "
                        f"{q} — one serial per unit"
                    )
                _apply_serials_out(state, serials, batch, godown, tolerant=tolerant)
                _apply_serials_in(state, serials, batch, to_godown, tolerant=tolerant)
            elif batch is not None:
                _apply_batch_out(state, batch, q, godown, tolerant=tolerant)
                _apply_batch_in(state, batch, q, None, to_godown, tolerant=tolerant)
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
            elif tolerant:
                unit = Decimal(0)  # the replay's orphan: counted stock with no basis
            else:
                raise ValidationError(
                    "inventory: counting up from an empty position requires an explicit rate "
                    "(no average cost exists)"
                )
            state.value += _money(delta * unit)
        elif delta < 0:
            if state.qty <= 0 and not tolerant:
                raise ValidationError("inventory: nothing to count down — position is empty")
            unit = state.value / state.qty if state.qty > 0 else Decimal(0)
            state.value -= _money((-delta) * unit)
        state.qty = q

    else:
        raise ValidationError(
            f"inventory: movement must be one of {', '.join(MOVEMENTS_VALID)}, got {movement!r}"
        )

    return state


def _apply_recount(state: _ItemState, item: dict, tolerant: bool = False) -> None:
    """The physical count, depth-aware (ADR-027 §4): the line's qty is the
    ABSOLUTE count of its target — ``(item, godown)`` by default, or
    ``(item, batch, godown)`` when the line names a batch (a plain count of
    a batch-carrying godown would leave the split unknowable — refused at
    posting). The item-level arithmetic runs on the equivalent absolute:
    ``state.qty + (counted − sub_before)``, so the value adjusts at the
    item's average exactly as ADR-011's recount did, and the sub-ledgers
    move by the same delta. Without depth keys the target is the whole
    position at Main location — the original semantics, byte for byte."""
    counted = _decimal(item.get("qty", 0), "items.qty")
    godown = _line_godown(item, 0)
    batch = _override(item, "batch")
    if _line_serials(item, 0):
        raise ValidationError(
            "inventory: a recount counts units, not serials — recount the quantity (serials follow)"
        )

    if batch is not None:
        bstate = state.batches.get(batch)
        if bstate is None:
            if not tolerant:
                state.batch_state(batch)  # raises the honest unknown-batch error
            return
        sub_before = bstate.godowns.get(godown, Decimal(0))
    else:
        batches_here = [b for b, bs in state.batches.items() if bs.godowns.get(godown, 0) > 0]
        if batches_here and not tolerant:
            raise ValidationError(
                f"inventory: the item tracks batches at {godown!r} "
                f"({', '.join(sorted(batches_here))}) — a physical count names its batch"
            )
        sub_before = state.godown_qty(godown)

    delta = counted - sub_before
    item_abs = {**item, "qty": str(state.qty + delta)}
    _apply_line(state, "recount", item_abs, tolerant=tolerant)
    if batch is not None:
        if delta < 0:
            _apply_batch_out(state, batch, -delta, godown, tolerant=tolerant)
        elif delta > 0:
            _apply_batch_in(state, batch, delta, None, godown, tolerant=tolerant)
    state.godowns[godown] = state.godown_qty(godown) + delta


def _apply_manufacture(states: dict[str, _ItemState], doc: dict, tolerant: bool = False) -> dict:
    """The manufacturing journal's fold (ADR-027 §5): consume every
    component at its moving average, produce the single output at the
    rolled-up cost — ``output value = Σ component costs + Σ charges``.
    Used by the transformer (which posts exactly these numbers) and by
    every replay, so a manufacture is as deterministic as any other
    movement. Returns the cost rollup for the lines/annotations."""
    components = doc.get("components")
    if not isinstance(components, list) or not components:
        raise ValidationError("inventory: manufacture.components must be a non-empty list")
    output = doc.get("output")
    if not isinstance(output, dict):
        raise ValidationError("inventory: manufacture.output must be an object")
    charges = doc.get("charges") or []

    costs: list[dict] = []
    total = Decimal("0.00")

    for i, comp in enumerate(components, start=1):
        if not isinstance(comp, dict):
            raise ValidationError(f"inventory: components[{i}] must be an object")
        name = str(comp.get("item") or "").strip()
        if not name:
            raise ValidationError(f"inventory: components[{i}].item is required")
        state = states.get(name)
        if state is None or state.qty <= 0:
            raise ValidationError(f"inventory: no stock of {name!r} to consume (position is empty)")
        value_before = state.value
        _apply_line(state, "out", comp)
        cost = value_before - state.value  # exactly what the fold will record
        costs.append({"item": name, "cost": cost})
        total += cost

    charge_total = Decimal("0.00")
    charge_rows: list[dict] = []
    for i, charge in enumerate(charges, start=1):
        if not isinstance(charge, dict):
            raise ValidationError(f"inventory: charges[{i}] must be an object")
        amount = _money(_decimal(charge.get("amount"), f"charges[{i}].amount"))
        if amount <= 0:
            raise ValidationError(f"inventory: charges[{i}].amount must be > 0")
        charge_rows.append({"ledger": str(charge.get("ledger") or "").strip(), "amount": amount})
        charge_total += amount
    total += charge_total

    out_name = str(output.get("item") or "").strip()
    if not out_name:
        raise ValidationError("inventory: manufacture.output.item is required")
    out_qty = _decimal(output.get("qty"), "output.qty")
    if out_qty <= 0:
        raise ValidationError("inventory: manufacture.output.qty must be > 0")
    out_state = states.setdefault(out_name, _ItemState())
    godown = _line_godown(output, 0)
    batch = _override(output, "batch")
    expiry_raw = output.get("expiry")
    if expiry_raw is not None and batch is None:
        raise ValidationError("inventory: output expiry rides a batch — set output.batch too")
    expiry = _parse_expiry(expiry_raw, "output.expiry") if expiry_raw else None
    serials = _line_serials(output, 0)
    if serials and Decimal(len(serials)) != out_qty:
        raise ValidationError(
            f"inventory: {len(serials)} serials cannot produce a qty of {out_qty} — "
            "one serial per unit"
        )

    # production: the output's value IS the rollup — no rate rounding, ever
    out_state.qty += out_qty
    out_state.value += total
    out_state.godowns[godown] = out_state.godown_qty(godown) + out_qty
    if batch is not None:
        _apply_batch_in(out_state, batch, out_qty, expiry, godown, tolerant=tolerant)
    if serials:
        _apply_serials_in(out_state, serials, batch, godown, tolerant=tolerant)

    return {
        "components": costs,
        "charges": charge_rows,
        "output": out_name,
        "output_qty": out_qty,
        "output_value": total,
        "material_total": total - charge_total,
        "charge_total": charge_total,
    }


# ----------------------------------------------------------- the fold
def _document_items(doc: dict) -> list[tuple[str, dict]]:
    """Every (item name, line) a stock document touches — the five movement
    shapes flattened. Manufacture contributes its components and output."""
    movement = doc.get("movement")
    if movement == "manufacture":
        pairs = [
            (str(c.get("item") or "").strip(), c)
            for c in (doc.get("components") or [])
            if isinstance(c, dict)
        ]
        output = doc.get("output")
        if isinstance(output, dict):
            pairs.append((str(output.get("item") or "").strip(), output))
        return pairs
    return [
        (str(it.get("item") or "").strip(), it)
        for it in (doc.get("items") or [])
        if isinstance(it, dict)
    ]


def _fold_states(documents: list, as_of: date | None = None) -> dict[str, _ItemState]:
    """Replay every posted stock document of the book, oldest first,
    folding ALL items together (a manufacture needs its components' states
    to cost its output). Documents undone by a reversal net to zero: the
    reversal marks its original, and both are skipped. ``as_of`` windows
    the replay by voucher date — the position as the books stood that day."""
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    states: dict[str, _ItemState] = {}
    for d in documents:
        if d.reversal_of is not None or d.voucher_id in reversed_ids:
            continue
        if as_of is not None and d.voucher_date > as_of:
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict):
            continue
        movement = doc.get("movement")
        if movement == "manufacture":
            for name in {n for n, _ in _document_items(doc) if n}:
                states.setdefault(name, _ItemState())
            _apply_manufacture(states, doc, tolerant=True)
            continue
        for name, line in _document_items(doc):
            if not name:
                continue
            state = states.setdefault(name, _ItemState())
            if movement == "recount":
                _apply_recount(state, line, tolerant=True)
            else:
                _apply_line(state, str(movement), line, tolerant=True)
    return states


def _fold(documents: list, item: str) -> _ItemState:
    """The one-item view of the fold (the ADR-011 signature, kept): replay
    the book's documents, return the named item's state."""
    return _fold_states(documents).get(item, _ItemState())


@dataclass(frozen=True)
class BatchPosition:
    """One batch of one item as the reports show it."""

    batch: str
    qty: Decimal
    expiry: date | None
    godowns: dict[str, Decimal]


@dataclass(frozen=True)
class ItemPosition:
    """Current derived position of one item — what a warehouse bridge
    recomputes, what the reports would show if stock were a ledger. The
    Phase 6 sub-ledgers (godowns, batches, serial count) ride along; an
    item without depth history reports the plain defaults."""

    qty: Decimal
    value: Decimal
    avg_cost: Decimal | None  # value/qty (4 dp); None when the position is empty
    godowns: dict[str, Decimal] = field(default_factory=dict)
    batches: dict[str, BatchPosition] = field(default_factory=dict)
    serial_count: int = 0


def position(resolver: Resolver, item: str, as_of: date | None = None) -> ItemPosition:
    """The stock position of ``item`` in the resolver's book — the same fold
    the transformer runs, so it always agrees with posted documents."""
    state = _fold_states(resolver.prior_documents(META_KEY), as_of=as_of).get(item, _ItemState())
    avg = state.value / state.qty if state.qty > 0 else None
    return ItemPosition(
        qty=state.qty,
        value=state.value,
        avg_cost=avg.quantize(AVG, rounding=ROUND_HALF_UP) if avg is not None else None,
        godowns={g: q for g, q in sorted(state.godowns.items()) if q != 0},
        batches={
            name: BatchPosition(
                batch=name,
                qty=bstate.qty,
                expiry=bstate.expiry,
                godowns={g: q for g, q in sorted(bstate.godowns.items()) if q != 0},
            )
            for name, bstate in sorted(state.batches.items())
            if bstate.qty != 0
        },
        serial_count=len(state.serials),
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
    for other in ("gst", "invoice", "receipt", "payroll"):
        if draft.meta.get(other) is not None:
            raise ValidationError(f"inventory: meta.stock and meta.{other} are mutually exclusive")
    if any(ln.currency is not None for ln in draft.lines):
        raise ValidationError(
            "inventory: meta.stock is not supported on foreign-currency lines "
            "(post FX vouchers separately)"
        )


# --------------------------------------------------------- the transformer
def _masters_for(resolver: Resolver, names) -> dict:
    """The stock-item masters of the document's items (plugin API 0.12) —
    None for unmastered names (ADR-011's string-item freedom)."""
    masters = {}
    for name in names:
        try:
            masters[name] = resolver.stock_item(name=name)
        except Exception:  # noqa: BLE001 — a missing table degrades to freedom
            masters[name] = None
    return masters


def _demand_tracking(masters, movement: str, items, names) -> None:
    """Master-declared tracking modes are demands, not suggestions (ADR-027
    §3): a batch-tracked item receives only into named batches; a
    serial-tracked item moves only by named serials. Unmastered items and
    masters without the toggles demand nothing."""
    for i, (item, name) in enumerate(zip(items, names, strict=True), start=1):
        master = masters.get(name)
        if master is None:
            continue
        if movement == "in" and master.track_batches and _override(item, "batch") is None:
            raise ValidationError(
                f"inventory: items[{i}] ({name!r}) maintains batches — goods-in must name its batch"
            )
        if movement in ("in", "out") and master.track_serials and not _line_serials(item, i):
            raise ValidationError(
                f"inventory: items[{i}] ({name!r}) maintains serial numbers — "
                f"{'receipts' if movement == 'in' else 'issues'} must list them "
                "(serials are identity, never auto-assigned)"
            )


def _check_out_godown(state: _ItemState, item: dict, i: int, allow_negative: bool) -> str:
    """The out-side location guard: a godown lends only what it holds
    (ADR-027 §4) — the fix for the ADR-011 limitation. ``allow_negative``
    follows the item overdraft, godown sub-ledger with it (documented)."""
    godown = _line_godown(item, i)
    q = _decimal(item.get("qty"), f"items[{i}].qty")
    held = state.godown_qty(godown)
    if q > held and not allow_negative:
        raise ValidationError(
            f"inventory: godown {godown!r} holds {held} of the item, "
            f"items[{i}] takes {q} — stock lives in locations "
            "(policy:allow_negative_stock overrides)"
        )
    return godown


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

    allow_negative = bool(draft.meta.get("policy:allow_negative_stock"))

    if movement == "manufacture":
        names = [n for n, _ in _document_items(doc) if n]
        if not names:
            raise ValidationError("inventory: manufacture names no items")
        for name in sorted(set(names)):
            resolver.lock(f"stock:{name}")
        # the fold runs UNDER the lock — the states see every committed
        # predecessor (the ADR-011 race discipline, manufacture included)
        states = _fold_states(resolver.prior_documents(META_KEY))
        lines, computed = _manufacture(doc, states, resolver)
    else:
        items = doc.get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError("inventory: stock.items must be a non-empty list")

        names: list[str] = []
        for i, it in enumerate(items, start=1):
            if not isinstance(it, dict):
                raise ValidationError(f"inventory: items[{i}] must be an object")
            names.append(_item_name(it, i))
        masters = _masters_for(resolver, set(names))
        _demand_tracking(masters, str(movement), items, names)

        # Serialize on every item this document touches — one sorted pass, so
        # concurrent drafts never deadlock (ADR-011 lock discipline). The
        # fold runs UNDER the lock: the states see every committed
        # predecessor, so the guards are airtight under races.
        for name in sorted(set(names)):
            resolver.lock(f"stock:{name}")

        documents = resolver.prior_documents(META_KEY)
        states = _fold_states(documents)
        states = {**states, **{n: states.get(n, _ItemState()) for n in set(names)}}

        if movement == "in":
            lines, computed = _goods_in(doc, items, names, states, resolver, masters)
        elif movement == "out":
            lines, computed = _goods_out(
                doc,
                items,
                names,
                states,
                resolver,
                masters,
                allow_negative,
                draft.voucher_date,
            )
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


def _depth_note(item: dict) -> str:
    """The narration's depth suffix: batch, expiry, serials and locations,
    ONLY when the line carries them explicitly — a depth-naive line keeps
    the plain ADR-011 narration (the tests' contract)."""
    parts = []
    batch = _override(item, "batch")
    if batch:
        note = f"batch {batch}"
        if item.get("expiry"):
            note += f" (exp {item['expiry']})"
        parts.append(note)
    serials = _line_serials(item, 0)
    if serials:
        parts.append(f"serials {', '.join(serials[:3])}{'…' if len(serials) > 3 else ''}")
    where = _override(item, "godown")
    to_godown = _override(item, "to_godown")
    if where and to_godown:
        parts.append(f"{where} → {to_godown}")
    elif where:
        parts.append(where)
    elif to_godown:
        parts.append(f"→ {to_godown}")
    return f" [{'; '.join(parts)}]" if parts else ""


def _goods_in(doc, items, names, states, resolver, masters):
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
                narration=f"{name} × {item.get('qty')} @ {rate}{_depth_note(item)}",
            )
        )
        total += amount
        computed_items.append(
            {
                "item": name,
                "line_amount": str(amount),
                "qty_after": str(state.qty),
                "avg_cost_after": _avg_after(state),
                "godown": _line_godown(item, i),
                **({"batch": _override(item, "batch")} if _override(item, "batch") else {}),
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


def _goods_out(doc, items, names, states, resolver, masters, allow_negative, voucher_date):
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
        godown = _check_out_godown(state, item, i, allow_negative)
        unit = state.value / state.qty
        if unit <= 0:
            raise ValidationError(
                f"inventory: no meaningful cost basis for {name!r} "
                "(position value/quantity is not positive)"
            )
        # FEFO/explicit allocation, recorded before the state moves
        batch = _override(item, "batch")
        if batch is not None:
            allocation = [(batch, q)]
        else:
            allocation = [(b, take) for b, take in _fefo_allocations(state, q)] or (
                [(None, q)] if not state.batches else []
            )
            if state.batches and sum((t for _, t in allocation), Decimal(0)) < q:
                raise ValidationError(
                    f"inventory: batches of {name!r} hold less than {q} — "
                    "FEFO cannot cover the issue"
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
                narration=f"COGS: {name} × {q} @ {_avg_str(unit)}{_depth_note(item)}",
            )
        )
        lines.append(
            DraftLine(
                dc="C",
                amount=cost,
                ledger_id=inventory.id,
                ledger_name=inventory.name,
                narration=f"issue {name} × {q} @ {_avg_str(unit)}{_depth_note(item)}",
            )
        )
        total_cost += cost

        computed: dict = {
            "item": name,
            "cost": str(cost),
            "qty_after": str(state.qty),
            "avg_cost_after": _avg_after(state),
            "godown": godown,
        }
        if allocation:
            computed["batch_allocation"] = [
                {"batch": b, "qty": str(t)} for b, t in allocation if b is not None
            ]

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
                    narration=f"{name} × {q} @ {rate}{_depth_note(item)}",
                )
            )
            total_revenue += revenue
            # the price-list suggestion, annotated never applied (ADR-027 §6)
            suggested = resolver.stock_price(name, party_name, voucher_date)
            if suggested is not None:
                computed["price_list_rate"] = str(suggested)

        computed_items.append(computed)

    computed_doc: dict = {"items": computed_items, "total_cost": str(total_cost)}
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
        computed_doc["revenue_total"] = str(total_revenue)
        computed_doc["gross_margin"] = str(total_revenue - total_cost)
    return lines, computed_doc


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
        _apply_line(state, "transfer", item)  # structural checks + depth move

        source = _ensure_inventory_ledger(resolver, name, _override(item, "inventory_ledger"))
        to_name = str(item.get("to_ledger") or "").strip()
        if not to_name:
            raise ValidationError(f"inventory: items[{i}].to_ledger is required for a transfer")
        destination = resolver.ensure_ledger(to_name, STOCK_GROUP)
        if destination.id == source.id:
            raise ValidationError(
                f"inventory: items[{i}].to_ledger must differ from the source ledger"
            )
        note = f"transfer {name} × {q} @ {_avg_str(unit)}{_depth_note(item)}"
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
        computed_items.append(
            {
                "item": name,
                "amount": str(amount),
                "from_godown": _line_godown(item, i),
                "to_godown": _line_godown(item, i, "to_godown"),
            }
        )

    return lines, {"items": computed_items, "total": str(total)}


def _recount(items, names, states, resolver):
    lines: list[DraftLine] = []
    computed_items: list[dict] = []
    total_adjustment = Decimal("0.00")
    adjustment = None

    for _i, (item, name) in enumerate(zip(items, names, strict=True), start=1):
        state = states[name]
        qty_before, value_before = state.qty, state.value
        _apply_recount(state, item)
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
        note = f"recount {name}: {qty_before} → {state.qty}{_depth_note(item)}"
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
                "godown": _line_godown(item, _i),
            }
        )

    if not lines:
        raise ValidationError("inventory: recount changes nothing (counted qty equals book qty)")
    return lines, {"items": computed_items, "total_adjustment": str(total_adjustment)}


# ------------------------------------------------------------- manufacture
def _validate_against_bom(resolver: Resolver, doc: dict, masters: dict) -> None:
    """When the journal names a BOM, the recipe rules (ADR-027 §5): the
    output item must be the BOM's own, the component set must be exactly
    the recipe's, and every component quantity must cover its per-output
    share (excess consumption is wastage — real; under-consumption against
    the recipe is a data error)."""
    bom_name = _override(doc, "bom")
    if bom_name is None:
        return
    bom = resolver.stock_bom(name=bom_name)
    if bom is None:
        raise ValidationError(
            f"inventory: no BOM named {bom_name!r} in this book — create it "
            "under Inventory, or post the journal without 'bom'"
        )
    output = doc.get("output") or {}
    out_name = str(output.get("item") or "").strip()
    if out_name != bom.output_item:
        raise ValidationError(
            f"inventory: BOM {bom_name!r} produces {bom.output_item!r}, the "
            f"journal produces {out_name!r}"
        )
    out_qty = _decimal(output.get("qty"), "output.qty")
    components = {
        str(c.get("item") or "").strip(): _decimal(c.get("qty"), "components[].qty")
        for c in (doc.get("components") or [])
        if isinstance(c, dict)
    }
    recipe = {name: qty for name, qty in bom.components}
    missing = sorted(set(recipe) - set(components))
    extra = sorted(set(components) - set(recipe))
    if missing:
        raise ValidationError(
            f"inventory: BOM {bom_name!r} requires {', '.join(missing)} — the journal omits them"
        )
    if extra:
        raise ValidationError(
            f"inventory: {', '.join(extra)} are not in BOM {bom_name!r} — "
            "post extras as their own journal without the BOM"
        )
    for name, per_output in recipe.items():
        required = per_output * out_qty / bom.output_qty
        if components[name] < required:
            raise ValidationError(
                f"inventory: BOM {bom_name!r} needs {name} × {per_output} per "
                f"{bom.output_qty} output ({_plain(required)} for this run) — "
                f"the journal consumes {components[name]}"
            )


def _manufacture(doc, states, resolver):
    """The manufacturing journal's posting half: validate the shape, the
    recipe (when named) and the charge ledgers, then fold the document
    through ``_apply_manufacture`` — the same function the replay runs —
    and post exactly the numbers it returns."""
    output = doc.get("output")
    if not isinstance(output, dict):
        raise ValidationError("inventory: manufacture.output must be an object")
    components = doc.get("components")
    if not isinstance(components, list) or not components:
        raise ValidationError("inventory: manufacture.components must be a non-empty list")
    for i, comp in enumerate(components, start=1):
        if not isinstance(comp, dict):
            raise ValidationError(f"inventory: components[{i}] must be an object")

    names = [str(c.get("item") or "").strip() for c in components if isinstance(c, dict)]
    out_name = str(output.get("item") or "").strip()
    names.append(out_name)
    masters = _masters_for(resolver, set(n for n in names if n))

    # tracking demands: serialized outputs are born with their serials;
    # serialized/batched components move like any issue
    out_master = masters.get(out_name)
    if out_master is not None and out_master.track_serials and not _line_serials(output, 0):
        raise ValidationError(
            f"inventory: output item {out_name!r} maintains serial numbers — "
            "production must assign them (output.serials)"
        )
    _demand_tracking(masters, "out", components, names[:-1])

    _validate_against_bom(resolver, doc, masters)

    # charge ledgers exist and are not stock (valuing stock with stock is a
    # closed loop, not a cost)
    charges = doc.get("charges") or []
    stock_group = resolver.group(STOCK_GROUP)
    for i, charge in enumerate(charges, start=1):
        if not isinstance(charge, dict):
            raise ValidationError(f"inventory: charges[{i}] must be an object")
        ledger_name = str(charge.get("ledger") or "").strip()
        if not ledger_name:
            raise ValidationError(f"inventory: charges[{i}].ledger is required")
        ledger = resolver.ledger(name=ledger_name)
        if ledger is None:
            raise ValidationError(
                f"inventory: charge ledger {ledger_name!r} does not exist in this book"
            )
        if stock_group is not None and ledger.group_id == stock_group.id:
            raise ValidationError(
                f"inventory: charge ledger {ledger_name!r} is under {STOCK_GROUP!r} — "
                "charges come from outside the stock (wages, power, freight…)"
            )
        amount = _money(_decimal(charge.get("amount"), f"charges[{i}].amount"))
        if amount <= 0:
            raise ValidationError(f"inventory: charges[{i}].amount must be > 0")

    # the fold under the advisory locks: components consumed, output valued
    rollup = _apply_manufacture(states, doc)
    if rollup["output_value"] <= 0:
        raise ValidationError(
            "inventory: the manufacture rolls up to 0.00 — every entry must carry a positive amount"
        )

    lines: list[DraftLine] = []
    for comp, cost_row in zip(components, rollup["components"], strict=True):
        name = cost_row["item"]
        ledger = _ensure_inventory_ledger(resolver, name, _override(comp, "inventory_ledger"))
        lines.append(
            DraftLine(
                dc="C",
                amount=cost_row["cost"],
                ledger_id=ledger.id,
                ledger_name=ledger.name,
                narration=f"consume {name} × {comp.get('qty')}{_depth_note(comp)}",
            )
        )
    for charge_row in rollup["charges"]:
        ledger = resolver.ledger(name=charge_row["ledger"])
        lines.append(
            DraftLine(
                dc="C",
                amount=charge_row["amount"],
                ledger_id=ledger.id,
                ledger_name=ledger.name,
                narration=f"charge: {charge_row['ledger']}{_reference_note(doc)}",
            )
        )
    out_ledger = _ensure_inventory_ledger(resolver, out_name, _override(output, "inventory_ledger"))
    lines.insert(
        0,
        DraftLine(
            dc="D",
            amount=rollup["output_value"],
            ledger_id=out_ledger.id,
            ledger_name=out_ledger.name,
            narration=(
                f"produce {out_name} × {rollup['output_qty']} @ "
                f"{_avg_str(rollup['output_value'] / rollup['output_qty'])}"
                f"{_depth_note(output)}"
            ),
        ),
    )

    computed = {
        "bom": _override(doc, "bom"),
        "components": [
            {
                "item": row["item"],
                "cost": str(row["cost"]),
                "qty_after": str(states[row["item"]].qty),
            }
            for row in rollup["components"]
        ],
        "charges": [
            {"ledger": row["ledger"], "amount": str(row["amount"])} for row in rollup["charges"]
        ],
        "output": rollup["output"],
        "output_qty": str(rollup["output_qty"]),
        "material_total": str(rollup["material_total"]),
        "charge_total": str(rollup["charge_total"]),
        "output_value": str(rollup["output_value"]),
        "output_unit_cost": _avg_str(rollup["output_value"] / rollup["output_qty"]),
        "output_qty_after": str(states[rollup["output"]].qty),
    }
    return lines, computed


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
    items = doc.get("items") or []
    if doc.get("movement") == "manufacture":
        items = (doc.get("components") or []) + [doc.get("output")]
    MOVEMENTS.append(
        {
            "kind": doc.get("movement"),
            "voucher_no": event.data.get("voucher_no"),
            "book_id": event.data.get("book_id"),
            "party": doc.get("party"),
            "items": [
                {
                    "item": str(it.get("item")),
                    "qty": str(it.get("qty")),
                    **({"godown": it["godown"]} if it.get("godown") else {}),
                    **({"batch": it["batch"]} if it.get("batch") else {}),
                }
                for it in items
                if isinstance(it, dict)
            ],
        }
    )
    logger.info("stock %s posted: %s", doc.get("movement"), event.data.get("voucher_no"))


# ---------------------------------------------------------------- reports
def _parse_as_of(params: dict[str, str]) -> date:
    raw = (params.get("as_of") or "").strip()
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationError(
            f"inventory: as_of must be an ISO date (YYYY-MM-DD), got {raw!r}"
        ) from None


def _positions(resolver: Resolver, as_of: date) -> dict[str, _ItemState]:
    return _fold_states(resolver.prior_documents(META_KEY), as_of=as_of)


def _avg_or_none(state: _ItemState) -> Decimal | None:
    return state.value / state.qty if state.qty > 0 else None


def _expiry_bucket(expiry: date, as_of: date) -> str:
    days = (expiry - as_of).days
    if days < 0:
        return "expired"
    if days <= 30:
        return "<=30d"
    if days <= 60:
        return "<=60d"
    if days <= 90:
        return "<=90d"
    return ">90d"


_EXPIRY_ORDER = {"expired": 0, "<=30d": 1, "<=60d": 2, "<=90d": 3, ">90d": 4}


def stock_position_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """Every item's position with its depth: qty, value, average cost, the
    godown and batch breakdowns, serial counts — the positions browser
    (JSON is the UI's surface; CSV and PDF render the same fold)."""
    as_of = _parse_as_of(params)
    states = _positions(resolver, as_of)
    item_param = (params.get("item") or "").strip()

    rows = []
    for name in sorted(states):
        if item_param and name != item_param:
            continue
        state = states[name]
        avg = _avg_or_none(state)
        rows.append(
            {
                "item": name,
                "qty": str(state.qty),
                "value": str(state.value),
                "avg_cost": _avg_str(avg) if avg is not None else None,
                "serial_count": len(state.serials),
                "godowns": {g: str(q) for g, q in sorted(state.godowns.items()) if q != 0},
                "batches": [
                    {
                        "batch": b,
                        "qty": str(bs.qty),
                        "expiry": bs.expiry.isoformat() if bs.expiry else None,
                        "godowns": {g: str(q) for g, q in sorted(bs.godowns.items()) if q != 0},
                    }
                    for b, bs in sorted(state.batches.items())
                ],
            }
        )

    total_value = sum((states[n].value for n in states), Decimal("0.00"))
    data = {"as_of": as_of.isoformat(), "items": rows, "total_value": str(total_value)}
    title = "Stock position" + (f" — {item_param}" if item_param else "")

    csv_lines = ["item,qty,value,avg_cost,serials,godowns,batches"]
    csv_lines += [
        f"{r['item']},{r['qty']},{r['value']},{r['avg_cost'] or ''},"
        f"{r['serial_count']},"
        f"{' '.join(f'{g}:{q}' for g, q in r['godowns'].items())},"
        f"{' '.join(b['batch'] + ':' + b['qty'] for b in r['batches'])}"
        for r in rows
    ]
    csv_lines.append(f"TOTAL,,,{-total_value if False else total_value}")

    view = DocumentView(
        title=title,
        subtitle=f"positions as of {as_of.isoformat()} — weighted average valuation",
        fields=(
            ("Book", resolver.book.name),
            ("As of", as_of.isoformat()),
            ("Items", str(len(rows))),
            ("Stock value", format_amount(total_value)),
        ),
        sections=(
            DocumentSection(
                title="Positions",
                columns=(
                    DocumentColumn("Item", width=1.6),
                    DocumentColumn("Qty", width=0.8, align="right"),
                    DocumentColumn("Value", width=1.0, align="right"),
                    DocumentColumn("Avg cost", width=0.9, align="right"),
                    DocumentColumn("Godowns (qty)", width=1.6),
                    DocumentColumn("Batches (qty)", width=1.6),
                ),
                rows=tuple(
                    (
                        r["item"],
                        _plain(Decimal(r["qty"])),
                        format_amount(Decimal(r["value"])),
                        r["avg_cost"] or "—",
                        "; ".join(f"{g}: {_plain(Decimal(q))}" for g, q in r["godowns"].items())
                        or "—",
                        "; ".join(
                            f"{b['batch']}: {_plain(Decimal(b['qty']))}" for b in r["batches"]
                        )
                        or "—",
                    )
                    for r in rows
                ),
            ),
        ),
        totals=(("Total stock value", format_amount(total_value)),),
        filename=title,
    )
    return PackReport(
        title=title, filename=title, view=view, csv="\n".join(csv_lines) + "\n", data=data
    )


def stock_godown_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The godown × item matrix with per-godown totals (ADR-027 §6) —
    where the stock physically sits, at average-cost value."""
    as_of = _parse_as_of(params)
    states = _positions(resolver, as_of)

    rows = []
    totals: dict[str, Decimal] = {}
    for name in sorted(states):
        state = states[name]
        avg = _avg_or_none(state)
        for godown in sorted({g for g, q in state.godowns.items() if q != 0}):
            qty = state.godowns[godown]
            value = _money(qty * avg) if avg is not None else Decimal("0.00")
            totals[godown] = totals.get(godown, Decimal("0.00")) + value
            rows.append(
                {
                    "godown": godown,
                    "item": name,
                    "qty": str(qty),
                    "value": str(value),
                    "avg_cost": _avg_str(avg) if avg is not None else None,
                }
            )

    data = {
        "as_of": as_of.isoformat(),
        "rows": rows,
        "godown_totals": {g: str(v) for g, v in sorted(totals.items())},
    }
    csv_lines = ["godown,item,qty,value,avg_cost"]
    csv_lines += [
        f"{r['godown']},{r['item']},{r['qty']},{r['value']},{r['avg_cost'] or ''}" for r in rows
    ]
    for godown, value in sorted(totals.items()):
        csv_lines.append(f"TOTAL,{godown},,,{value}")

    view = DocumentView(
        title="Godown-wise stock",
        subtitle=f"where the stock sits, as of {as_of.isoformat()}",
        fields=(
            ("Book", resolver.book.name),
            ("As of", as_of.isoformat()),
            ("Locations", str(len(totals))),
            ("Lines", str(len(rows))),
        ),
        sections=(
            DocumentSection(
                title="By location",
                columns=(
                    DocumentColumn("Godown", width=1.4),
                    DocumentColumn("Item", width=1.6),
                    DocumentColumn("Qty", width=0.8, align="right"),
                    DocumentColumn("Avg cost", width=0.9, align="right"),
                    DocumentColumn("Value", width=1.0, align="right"),
                ),
                rows=tuple(
                    (
                        r["godown"],
                        r["item"],
                        _plain(Decimal(r["qty"])),
                        r["avg_cost"] or "—",
                        format_amount(Decimal(r["value"])),
                    )
                    for r in rows
                ),
            ),
        ),
        totals=tuple(
            (f"{godown} — total", format_amount(value)) for godown, value in sorted(totals.items())
        ),
        filename="Godown-wise stock",
    )
    return PackReport(
        title="Godown-wise stock",
        filename="Godown-wise stock",
        view=view,
        csv="\n".join(csv_lines) + "\n",
        data=data,
    )


def stock_batch_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """Batch-wise positions with expiry and the godown split (ADR-027 §6)."""
    as_of = _parse_as_of(params)
    states = _positions(resolver, as_of)

    rows = []
    for name in sorted(states):
        state = states[name]
        avg = _avg_or_none(state)
        for batch in sorted(state.batches):
            bstate = state.batches[batch]
            if bstate.qty == 0:
                continue
            value = _money(bstate.qty * avg) if avg is not None else Decimal("0.00")
            rows.append(
                {
                    "item": name,
                    "batch": batch,
                    "qty": str(bstate.qty),
                    "expiry": bstate.expiry.isoformat() if bstate.expiry else None,
                    "value": str(value),
                    "godowns": {g: str(q) for g, q in sorted(bstate.godowns.items()) if q != 0},
                }
            )

    data = {"as_of": as_of.isoformat(), "rows": rows}
    csv_lines = ["item,batch,expiry,qty,value,godowns"]
    csv_lines += [
        f"{r['item']},{r['batch']},{r['expiry'] or ''},{r['qty']},{r['value']},"
        f"{' '.join(f'{g}:{q}' for g, q in r['godowns'].items())}"
        for r in rows
    ]

    view = DocumentView(
        title="Batch-wise stock",
        subtitle=f"batches with expiry, as of {as_of.isoformat()}",
        fields=(
            ("Book", resolver.book.name),
            ("As of", as_of.isoformat()),
            ("Batches", str(len(rows))),
        ),
        sections=(
            DocumentSection(
                title="Batches",
                columns=(
                    DocumentColumn("Item", width=1.4),
                    DocumentColumn("Batch", width=1.2),
                    DocumentColumn("Expiry", width=0.9),
                    DocumentColumn("Qty", width=0.7, align="right"),
                    DocumentColumn("Value", width=0.9, align="right"),
                    DocumentColumn("Godowns (qty)", width=1.5),
                ),
                rows=tuple(
                    (
                        r["item"],
                        r["batch"],
                        r["expiry"] or "—",
                        _plain(Decimal(r["qty"])),
                        format_amount(Decimal(r["value"])),
                        "; ".join(f"{g}: {_plain(Decimal(q))}" for g, q in r["godowns"].items())
                        or "—",
                    )
                    for r in rows
                ),
            ),
        ),
        filename="Batch-wise stock",
    )
    return PackReport(
        title="Batch-wise stock",
        filename="Batch-wise stock",
        view=view,
        csv="\n".join(csv_lines) + "\n",
        data=data,
    )


def stock_expiry_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The expiry-ageing report (ADR-027 §6): every dated batch bucketed by
    days-to-expiry against the cut — expired, ≤30, ≤60, ≤90, >90 — quantity
    and value-at-average per batch, earliest first."""
    as_of = _parse_as_of(params)
    states = _positions(resolver, as_of)

    rows = []
    for name in sorted(states):
        state = states[name]
        avg = _avg_or_none(state)
        for batch in sorted(state.batches):
            bstate = state.batches[batch]
            if bstate.qty == 0 or bstate.expiry is None:
                continue
            value = _money(bstate.qty * avg) if avg is not None else Decimal("0.00")
            rows.append(
                {
                    "item": name,
                    "batch": batch,
                    "expiry": bstate.expiry.isoformat(),
                    "days_left": str((bstate.expiry - as_of).days),
                    "bucket": _expiry_bucket(bstate.expiry, as_of),
                    "qty": str(bstate.qty),
                    "value": str(value),
                }
            )
    rows.sort(key=lambda r: (r["expiry"], r["item"], r["batch"]))

    bucket_totals: dict[str, Decimal] = {}
    for row in rows:
        bucket_totals[row["bucket"]] = bucket_totals.get(row["bucket"], Decimal("0.00")) + Decimal(
            row["value"]
        )
    data = {
        "as_of": as_of.isoformat(),
        "rows": rows,
        "bucket_totals": {
            b: str(bucket_totals.get(b, Decimal("0.00")))
            for b in ("expired", "<=30d", "<=60d", "<=90d", ">90d")
        },
    }
    csv_lines = ["item,batch,expiry,days_left,bucket,qty,value"]
    csv_lines += [
        f"{r['item']},{r['batch']},{r['expiry']},{r['days_left']},{r['bucket']},"
        f"{r['qty']},{r['value']}"
        for r in rows
    ]

    view = DocumentView(
        title="Expiry ageing",
        subtitle=f"dated batches against {as_of.isoformat()} — FEFO's evidence",
        fields=(
            ("Book", resolver.book.name),
            ("As of", as_of.isoformat()),
            ("Dated batches", str(len(rows))),
            ("Expired value", format_amount(bucket_totals.get("expired", Decimal("0.00")))),
        ),
        sections=(
            DocumentSection(
                title="Batches by expiry",
                columns=(
                    DocumentColumn("Item", width=1.4),
                    DocumentColumn("Batch", width=1.1),
                    DocumentColumn("Expiry", width=0.85),
                    DocumentColumn("Days", width=0.6, align="right"),
                    DocumentColumn("Bucket", width=0.7),
                    DocumentColumn("Qty", width=0.7, align="right"),
                    DocumentColumn("Value", width=0.9, align="right"),
                ),
                rows=tuple(
                    (
                        r["item"],
                        r["batch"],
                        r["expiry"],
                        r["days_left"],
                        r["bucket"],
                        _plain(Decimal(r["qty"])),
                        format_amount(Decimal(r["value"])),
                    )
                    for r in rows
                ),
            ),
        ),
        totals=tuple(
            (f"{bucket} — value", format_amount(bucket_totals.get(bucket, Decimal("0.00"))))
            for bucket in ("expired", "<=30d", "<=60d", "<=90d", ">90d")
        ),
        filename="Expiry ageing",
    )
    return PackReport(
        title="Expiry ageing",
        filename="Expiry ageing",
        view=view,
        csv="\n".join(csv_lines) + "\n",
        data=data,
    )


def stock_shortfall_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The re-order shortfall (ADR-027 §6): items at or below their masters'
    re-order levels — on-hand, level, below-by, and the suggested order
    (the re-order quantity). Items without masters are absent by
    definition; positions are the same fold every report reads."""
    as_of = _parse_as_of(params)
    states = _positions(resolver, as_of)
    masters = resolver.stock_items()

    rows = []
    total_suggested = Decimal("0.00")
    for master in masters:
        if master.reorder_level is None:
            continue
        state = states.get(master.name, _ItemState())
        if state.qty > master.reorder_level:
            continue
        below_by = master.reorder_level - state.qty
        suggested = master.reorder_quantity or Decimal(0)
        total_suggested += suggested
        rows.append(
            {
                "item": master.name,
                "unit": master.unit,
                "on_hand": _plain(state.qty),
                "reorder_level": _plain(master.reorder_level),
                "below_by": _plain(below_by),
                "reorder_quantity": _plain(master.reorder_quantity or Decimal(0)),
                "suggested_order": _plain(suggested),
            }
        )

    data = {"as_of": as_of.isoformat(), "rows": rows}
    csv_lines = ["item,unit,on_hand,reorder_level,below_by,reorder_quantity,suggested_order"]
    csv_lines += [
        f"{r['item']},{r['unit'] or ''},{r['on_hand']},{r['reorder_level']},"
        f"{r['below_by']},{r['reorder_quantity']},{r['suggested_order']}"
        for r in rows
    ]
    csv_lines.append(f"TOTAL,,,,,,{total_suggested}")

    view = DocumentView(
        title="Re-order shortfall",
        subtitle=f"items at or below their levels, as of {as_of.isoformat()}",
        fields=(
            ("Book", resolver.book.name),
            ("As of", as_of.isoformat()),
            ("Below level", str(len(rows))),
        ),
        sections=(
            DocumentSection(
                title="Shortfalls",
                columns=(
                    DocumentColumn("Item", width=1.5),
                    DocumentColumn("Unit", width=0.6),
                    DocumentColumn("On hand", width=0.8, align="right"),
                    DocumentColumn("Level", width=0.7, align="right"),
                    DocumentColumn("Below by", width=0.7, align="right"),
                    DocumentColumn("Order qty", width=0.8, align="right"),
                ),
                rows=tuple(
                    (
                        r["item"],
                        r["unit"] or "—",
                        _plain(Decimal(r["on_hand"])),
                        _plain(Decimal(r["reorder_level"])),
                        _plain(Decimal(r["below_by"])),
                        _plain(Decimal(r["suggested_order"])),
                    )
                    for r in rows
                ),
            ),
        ),
        totals=(("Suggested order quantity (total)", _plain(total_suggested)),),
        filename="Re-order shortfall",
    )
    return PackReport(
        title="Re-order shortfall",
        filename="Re-order shortfall",
        view=view,
        csv="\n".join(csv_lines) + "\n",
        data=data,
    )


# ---------------------------------------------------------- document view
def stock_view(ctx: DocumentContext) -> DocumentView | None:
    """The print layout for ``meta.stock`` documents (plugin API 0.5,
    ADR-013): each movement prints as the physical document it is — goods
    receipt, issue note, stock transfer, physical count, manufacturing
    journal — items, batches, expiry, locations and the exact cost rollup
    re-read from the immutable stored document (reprints are identical,
    Law 2)."""
    doc = ctx.meta.get(META_KEY)
    if not isinstance(doc, dict) or doc.get("movement") not in MOVEMENTS_VALID:
        return None
    movement = doc.get("movement")
    computed = doc.get("computed") or {}

    fields: list[tuple[str, str]] = [
        ("Date", ctx.voucher_date.isoformat()),
        ("Voucher", f"{ctx.voucher_type} {ctx.voucher_no}"),
    ]
    sections: list[DocumentSection] = []
    totals: list[tuple[str, str]] = []

    if movement == "manufacture":
        components = [c for c in (doc.get("components") or []) if isinstance(c, dict)]
        output = doc.get("output") or {}
        charges = [c for c in (doc.get("charges") or []) if isinstance(c, dict)]
        fields.append(("BOM", str(doc.get("bom") or "explicit journal")))
        sections.append(
            DocumentSection(
                title="Components consumed",
                columns=(
                    DocumentColumn("Item", width=1.7),
                    DocumentColumn("Qty", width=0.7, align="right"),
                    DocumentColumn("Batch", width=1.1),
                    DocumentColumn("Godown", width=1.2),
                ),
                rows=tuple(
                    (
                        str(c.get("item") or ""),
                        _plain(_decimal(c.get("qty", 0), "qty")),
                        _override(c, "batch") or "—",
                        _line_godown(c, 0),
                    )
                    for c in components
                ),
            )
        )
        if charges:
            sections.append(
                DocumentSection(
                    title="Additional charges",
                    columns=(
                        DocumentColumn("Ledger", width=2.6),
                        DocumentColumn("Amount", width=1.0, align="right"),
                    ),
                    rows=tuple(
                        (
                            str(c.get("ledger") or ""),
                            format_amount(_money(_decimal(c.get("amount", 0), "amount"))),
                        )
                        for c in charges
                    ),
                )
            )
        unit_cost = computed.get("output_unit_cost")
        sections.append(
            DocumentSection(
                title="Production",
                columns=(
                    DocumentColumn("Item", width=1.7),
                    DocumentColumn("Qty", width=0.7, align="right"),
                    DocumentColumn("Batch", width=1.1),
                    DocumentColumn("Godown", width=1.2),
                    DocumentColumn("Unit cost", width=0.9, align="right"),
                ),
                rows=(
                    (
                        str(output.get("item") or ""),
                        _plain(_decimal(output.get("qty", 0), "qty")),
                        _override(output, "batch") or "—",
                        _line_godown(output, 0),
                        str(unit_cost or "—"),
                    ),
                ),
            )
        )
        totals = [
            ("Material cost", str(computed.get("material_total", "—"))),
            ("Charges", str(computed.get("charge_total", "—"))),
            ("Output value", str(computed.get("output_value", "—"))),
        ]
        title = "Manufacturing journal"
    else:
        items = [i for i in (doc.get("items") or []) if isinstance(i, dict)]
        if movement == "in":
            title = "Goods receipt"
            fields.append(("Supplier", str(doc.get("party") or "")))
            columns = (
                DocumentColumn("Item", width=1.5),
                DocumentColumn("Qty", width=0.6, align="right"),
                DocumentColumn("Rate", width=0.8, align="right"),
                DocumentColumn("Batch", width=1.0),
                DocumentColumn("Expiry", width=0.8),
                DocumentColumn("Godown", width=1.1),
                DocumentColumn("Amount", width=0.9, align="right"),
            )
            rows = tuple(
                (
                    str(i.get("item") or ""),
                    _plain(_decimal(i.get("qty", 0), "qty")),
                    format_amount(_decimal(i.get("rate", 0), "rate")),
                    _override(i, "batch") or "—",
                    str(i.get("expiry") or "—"),
                    _line_godown(i, 0),
                    format_amount(
                        _money(
                            _decimal(i.get("qty", 0), "qty") * _decimal(i.get("rate", 0), "rate")
                        )
                    ),
                )
                for i in items
            )
            totals = [("Total", str(computed.get("total", "—")))]
        elif movement == "out":
            title = "Goods issue"
            if doc.get("party"):
                fields.append(("Party", str(doc.get("party"))))
            columns = (
                DocumentColumn("Item", width=1.5),
                DocumentColumn("Qty", width=0.6, align="right"),
                DocumentColumn("Batch", width=1.1),
                DocumentColumn("Godown", width=1.1),
                DocumentColumn("Serials", width=1.2),
            )
            rows = tuple(
                (
                    str(i.get("item") or ""),
                    _plain(_decimal(i.get("qty", 0), "qty")),
                    _override(i, "batch") or "—",
                    _line_godown(i, 0),
                    ", ".join(_line_serials(i, 0)[:3]) or "—",
                )
                for i in items
            )
            totals = [("Total cost", str(computed.get("total_cost", "—")))]
            if "revenue_total" in computed:
                totals.insert(0, ("Revenue", str(computed.get("revenue_total"))))
                totals.append(("Gross margin", str(computed.get("gross_margin"))))
        elif movement == "transfer":
            title = "Stock transfer"
            columns = (
                DocumentColumn("Item", width=1.5),
                DocumentColumn("Qty", width=0.7, align="right"),
                DocumentColumn("Batch", width=1.1),
                DocumentColumn("From", width=1.2),
                DocumentColumn("To", width=1.2),
                DocumentColumn("Value", width=1.0, align="right"),
            )
            rows = tuple(
                (
                    str(i.get("item") or ""),
                    _plain(_decimal(i.get("qty", 0), "qty")),
                    _override(i, "batch") or "—",
                    _line_godown(i, 0),
                    _line_godown(i, 0, "to_godown"),
                    "—",
                )
                for i in items
            )
            totals = [("Total value moved", str(computed.get("total", "—")))]
        else:
            title = "Physical stock recount"
            columns = (
                DocumentColumn("Item", width=1.6),
                DocumentColumn("Counted", width=0.8, align="right"),
                DocumentColumn("Batch", width=1.1),
                DocumentColumn("Godown", width=1.2),
            )
            rows = tuple(
                (
                    str(i.get("item") or ""),
                    _plain(_decimal(i.get("qty", 0), "qty")),
                    _override(i, "batch") or "—",
                    _line_godown(i, 0),
                )
                for i in items
            )
            totals = [("Adjustment", str(computed.get("total_adjustment", "—")))]
        sections.append(DocumentSection(title="Lines", columns=columns, rows=rows))

    reference = str(doc.get("reference") or "").strip()
    if reference:
        fields.append(("Reference", reference))
    return DocumentView(
        title=title,
        subtitle=ctx.book_name,
        reference=ctx.voucher_no,
        fields=tuple(fields),
        sections=tuple(sections),
        totals=tuple(totals),
        filename=f"{title} {ctx.voucher_no}",
    )
