"""Order processing (ADR-024, roadmap Phase 3): the quote-to-cash and
procure-to-pay chains — quotation, sales order, delivery note, purchase
order and GRN — as the fifth plugin pack, with **zero schema change**.

One document key, ``meta.orders``, two roles (the ADR-023 shape):

- **Standalone order documents** (the line-builders): ``{"kind": …,
  "party": …, "ref": …, "items": […]}`` where kind is one of ``quotation``,
  ``sales_order``, ``delivery_note``, ``purchase_order``, ``grn``. Orders
  are not accounting (TallyPrime's order vouchers touch neither ledgers nor
  the trial balance), so each posts as a **zero-net memorandum trace**: one
  Dr + one Cr on "Order Book (memo)" for the grand total — a ledger whose
  balance is zero by construction, parked under Suspense A/c, one seeded
  group away from every financial statement. The transformer also rewrites
  the draft's voucher type to the kind's own series (plugin API 0.9:
  ``ensure_voucher_type``) so orders number QT-…, SO-…, DN-…, PO-…, GRN-…

  ``against`` links the chain: a sales order may convert a quotation, a
  delivery note fulfils a sales order, a GRN fulfils a purchase order —
  always the same party, always by ref. A delivery note / GRN line without
  its own rate is valued at the parent order line's rate (it is moving
  that order's quantity); a document's grand total must be positive.

- **Settlement allocations** (the annotation layer — never a line-builder):
  a billing voucher carries ``{"settle": [{"order": "SO-42", "lines":
  [{"item": "Widgets", "qty": "8"}]}]}``. Like bills allocations it rides
  whatever builds the lines (a ``meta.invoice`` sales invoice, a plain
  purchase voucher, a note); unlike bills it conserves no money — its guard
  is positional (the order exists, same party, cumulative invoiced/billed
  ≤ ordered per line) plus the carrier-moves-the-party discipline: the
  validator resolves the order among the parties the voucher actually
  moves and writes the resolved ``party``/``kind`` back into the
  allocation, so the fold replays it without guessing.

The position fold (deterministic replay, oldest first, reversals netted —
ADR-010/011): quotation lines track ``converted`` (informational); sales
order lines track ``fulfilled`` (delivered, from DNs) and ``invoiced``
(from settles); purchase order lines track ``fulfilled`` (received, from
GRNs) and ``invoiced`` (billed, from settles). Status: open → partial →
closed (fully invoiced/billed). Reversing an order cancels it (the undone
pair skips); reversing a DN, GRN or settling invoice restores the
position. The guards are the caps — cumulative fulfilled ≤ ordered and
cumulative invoiced ≤ ordered, per item, per order — enforced against the
live fold at posting time and replayed identically by every later fold.

Registered reports (ADR-014; PDF through the print bridge, CSV for
machines, JSON for UIs — plugin API 0.8):

- ``pending-orders`` — ``?side=sales|purchase``, optional ``party`` and
  ``as_of``: open order lines with ordered / delivered or received /
  invoiced or billed, pending quantity and value, status, per-party
  subtotals (Phase 3's acceptance report, both sides);
- ``orders-open`` — ``?kind=…``, optional ``party``: the open documents of
  one kind with per-line positions — the picker feed (the ``bills-open``
  pattern).

The print bridge (ADR-013): each of the five kinds claims its voucher's
print as the commercial document it is — static from the immutable stored
document, so reprints are identical (Law 2). Settle-carrying vouchers
decline, and the invoice's own view wins.

All money is exact ``Decimal`` (Law 3); tax rounds half-up to 2 dp at GST
slabs — the invoicing pack's arithmetic, display-only here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
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
    PriorDocument,
    Resolver,
    VoucherDraft,
)
from ..reporting.printing import format_amount

logger = logging.getLogger("ledgerforge.plugins.orders")

# Exact-arithmetic headroom — same discipline as the posting engine (Law 3).
getcontext().prec = 50

PLUGIN = PluginManifest(name="orders", version="0.1.0", api_compat="0.9")

META_KEY = "orders"

# The five document kinds: code (voucher type series), human name, side.
# Sales side sells to Sundry Debtors; purchase side buys from Sundry
# Creditors — the invoicing/inventory packs' party groups, so parties
# resolve through the same book-scoped masters.
SALES = "sales"
PURCHASE = "purchase"
KINDS: dict[str, tuple[str, str, str]] = {
    "quotation": ("QT", "Quotation", SALES),
    "sales_order": ("SO", "Sales Order", SALES),
    "delivery_note": ("DN", "Delivery Note", SALES),
    "purchase_order": ("PO", "Purchase Order", PURCHASE),
    "grn": ("GRN", "Goods Receipt Note", PURCHASE),
}

#: kind → the kind its ``against`` references (and what the reference
#: fulfils): a sales order converts a quotation, a DN fulfils an SO's
#: delivery, a GRN fulfils a PO's receipt — both tick ``fulfilled`` (the
#: reports name it delivered/received by side).
PARENT_OF = {
    "sales_order": ("quotation", None),
    "delivery_note": ("sales_order", "fulfilled"),
    "grn": ("purchase_order", "fulfilled"),
}

PARTY_GROUPS = {SALES: "Sundry Debtors", PURCHASE: "Sundry Creditors"}

# Seed groups (ledgerforge/seeding.py). The memorandum ledger lives under
# Suspense A/c: one seeded group away from every financial statement, its
# balance zero by construction (every order debits and credits it equally).
MEMO_GROUP = "Suspense A/c"
MEMO_LEDGER = "Order Book (memo)"

#: GST slabs — the same whitelist the invoicing pack enforces; tax on an
#: order is display-only (the invoice computes the real split), but a
#: fat-fingered slab must not print either.
TAX_SLABS = ("0", "5", "12", "18", "28")

#: Every order document posted while this pack is loaded (observability for
#: tests/demos; the real-world consumer is a fulfilment or BI bridge).
POSTED: list[dict] = []

CENT = Decimal("0.01")
_HUNDRED = Decimal(100)

ORDER_KINDS = ("sales_order", "purchase_order")


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.register_transformer(build_order_document)
    registry.register_validator(validate_orders)
    registry.on("voucher.posted", record_posted)
    registry.register_document_view(META_KEY, orders_view)
    registry.register_report("pending-orders", pending_orders_report)
    registry.register_report("orders-open", orders_open_report)


# --------------------------------------------------------------- helpers
def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _plain(value: Decimal) -> str:
    return f"{value.normalize():f}"


def _decimal(value: object, where: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except ArithmeticError:
        raise ValidationError(f"orders: {where} must be a number, got {value!r}") from None
    if not parsed.is_finite():
        raise ValidationError(f"orders: {where} must be a finite number")
    return parsed


def _qty(value: object, where: str) -> Decimal:
    parsed = _decimal(value, where)
    if parsed <= 0:
        raise ValidationError(f"orders: {where} must be > 0")
    return parsed


def _rate(value: object, where: str) -> Decimal:
    parsed = _decimal(value, where)
    if parsed < 0:
        raise ValidationError(f"orders: {where} must be >= 0")
    return parsed


def _slab(value: object, where: str) -> Decimal:
    raw = str(value if value is not None else "0")
    if raw not in TAX_SLABS:
        raise ValidationError(
            f"orders: {where} {raw!r} is not a GST slab (one of {', '.join(TAX_SLABS)})"
        )
    return Decimal(raw)


def _clean_name(value: object, where: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValidationError(f"orders: {where} is required")
    return name


def _doc_items(doc: dict) -> list[dict]:
    items = doc.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError("orders: items must be a non-empty list")
    return items


def _line_amount(item: dict, where: str) -> Decimal:
    """qty × rate, rounded once to cents (Law 3, the invoicing discipline)."""
    return _money(
        _qty(item.get("qty", 1), f"{where}.qty") * _rate(item.get("rate", 0), f"{where}.rate")
    )


def _line_tax(item: dict, where: str) -> Decimal:
    return _money(
        _line_amount(item, where) * _slab(item.get("tax_rate", "0"), f"{where}.tax_rate") / _HUNDRED
    )


def _document_total(doc: dict) -> tuple[Decimal, Decimal, Decimal]:
    """(subtotal, tax_total, grand_total) of an order document's items.
    Rates must already be resolved (a DN/GRN item without its own rate
    carries the parent order line's rate — see build_order_document)."""
    items = _doc_items(doc)
    subtotal = Decimal("0.00")
    tax_total = Decimal("0.00")
    for i, item in enumerate(items, start=1):
        where = f"items[{i}]"
        _clean_name(item.get("item"), f"{where}.item")
        subtotal += _line_amount(item, where)
        tax_total += _line_tax(item, where)
    return subtotal, tax_total, subtotal + tax_total


def _reject_conflicts(draft: VoucherDraft) -> None:
    """A standalone order document builds its own lines — it must not ride
    another line-building document (the cross-pack rule since ADR-011)."""
    for other in ("invoice", "receipt", "stock", "payroll", "gst"):
        if draft.meta.get(other) is not None:
            raise ValidationError(
                f"orders: meta.orders (a document) and meta.{other} are mutually "
                "exclusive — post the commercial and the order documents separately"
            )
    bills_doc = draft.meta.get("bills")
    if isinstance(bills_doc, dict) and bills_doc.get("interest_charge") is not None:
        raise ValidationError(
            "orders: meta.orders and an interest_charge both derive lines — post them separately"
        )


# ------------------------------------------------------- the position fold
@dataclass
class OrderLine:
    """One line of one order document — derived state, never stored."""

    item: str
    qty: Decimal
    rate: Decimal = Decimal("0.00")
    tax_rate: Decimal = Decimal(0)
    description: str = ""
    #: the fulfilment counters, accumulated by the fold: ``fulfilled`` is
    #: delivered (sales orders, from DNs) or received (purchase orders,
    #: from GRNs); ``invoiced`` is invoiced (sales) or billed (purchase).
    fulfilled: Decimal = Decimal(0)
    invoiced: Decimal = Decimal(0)


@dataclass
class OrderState:
    """One open order (or quotation) of one party — the fold's unit."""

    party: str
    ref: str
    kind: str
    voucher_no: str
    voucher_id: int
    date: date
    lines: dict[str, OrderLine] = field(default_factory=dict)
    against: str | None = None

    def status(self) -> str:
        if self.kind == "quotation":
            converted = any(ln.fulfilled > 0 for ln in self.lines.values())
            return "converted" if converted else "open"
        if not self.lines:
            return "open"
        if all(ln.invoiced >= ln.qty for ln in self.lines.values()):
            return "closed"
        if any(ln.invoiced > 0 or ln.fulfilled > 0 for ln in self.lines.values()):
            return "partial"
        return "open"


@dataclass
class QuotationState:
    """One quotation's conversion position — informational, never pending."""

    party: str
    ref: str
    voucher_no: str
    date: date
    converted: dict[str, Decimal] = field(default_factory=dict)  # per item


def _parse_items(doc: dict, kind: str, ref: str) -> dict[str, OrderLine]:
    """The document's own lines, keyed by item (one line per item)."""
    lines: dict[str, OrderLine] = {}
    title = KINDS[kind][1].lower()
    for item in _doc_items(doc):
        name = _clean_name(item.get("item"), "item")
        if name in lines:
            raise ValidationError(
                f"orders: item {name!r} appears twice in {title} {ref!r} — "
                "one line per item, or split the qty"
            )
        lines[name] = OrderLine(
            item=name,
            qty=_qty(item.get("qty", 1), "items.qty"),
            rate=_rate(item.get("rate", 0), "items.rate"),
            tax_rate=_slab(item.get("tax_rate", "0"), "items.tax_rate"),
            description=str(item.get("description") or ""),
        )
    return lines


def _apply_document(
    states: dict[tuple[str, str, str], OrderState],  # (kind, party, ref)
    quotes: dict[tuple[str, str], QuotationState],  # (party, ref)
    doc: dict,
    voucher_id: int,
    voucher_no: str,
    voucher_date: date,
) -> None:
    """The single source of truth for position arithmetic — used by the
    validator (guards live) and every later fold (what posted is valid, so
    the same guards pass on replay). Structural errors fire identically."""
    kind = doc.get("kind")
    if kind is None:
        return  # a settle allocation — handled by _apply_settle
    if kind not in KINDS:
        raise ValidationError(f"orders: kind must be one of {', '.join(KINDS)}, got {kind!r}")
    party = _clean_name(doc.get("party"), "party")
    ref = _clean_name(doc.get("ref"), "ref")

    if kind == "quotation":
        state = OrderState(
            party=party,
            ref=ref,
            kind=kind,
            voucher_no=voucher_no,
            voucher_id=voucher_id,
            date=voucher_date,
        )
        state.lines = _parse_items(doc, kind, ref)
        quotes[(party, ref)] = QuotationState(
            party=party, ref=ref, voucher_no=voucher_no, date=voucher_date
        )
        states[(kind, party, ref)] = state
        return

    # orders may reference a parent document (against)
    parent_kind, counter = PARENT_OF.get(kind, (None, None))
    against = str(doc.get("against") or "").strip() or None
    parent: OrderState | None = None
    if kind == "sales_order" and against is not None:
        if (party, against) not in quotes:
            raise ValidationError(
                f"orders: sales order {ref!r} converts quotation {against!r} — no "
                f"prior quotation of {party!r} carries that ref"
            )
    elif counter is not None and against is not None:
        parent = states.get((parent_kind, party, against))
        if parent is None:
            raise ValidationError(
                f"orders: {KINDS[kind][1].lower()} {ref!r} references {against!r} — "
                f"no prior open {KINDS[parent_kind][1].lower()} of {party!r} "
                "carries that ref"
            )

    if kind in ORDER_KINDS:
        state = OrderState(
            party=party,
            ref=ref,
            kind=kind,
            voucher_no=voucher_no,
            voucher_id=voucher_id,
            date=voucher_date,
            against=against,
        )
        state.lines = _parse_items(doc, kind, ref)
        states[(kind, party, ref)] = state
        # the conversion counts toward the quotation's informational position
        if kind == "sales_order" and against is not None:
            quote = quotes[(party, against)]
            for name, line in state.lines.items():
                quote.converted[name] = quote.converted.get(name, Decimal(0)) + line.qty
            # and marks the quotation state converted (for its own status)
            quote_state = states.get(("quotation", party, against))
            if quote_state is not None:
                for name, line in state.lines.items():
                    quote_state.lines.setdefault(name, line)
                    quote_state.lines[name].fulfilled += line.qty
        return

    # delivery note / GRN: fulfil the parent order's lines
    assert parent is not None and counter is not None
    for item in _doc_items(doc):
        name = _clean_name(item.get("item"), "item")
        qty = _qty(item.get("qty", 1), "items.qty")
        line = parent.lines.get(name)
        if line is None:
            raise ValidationError(
                f"orders: {KINDS[kind][1].lower()} {ref!r} moves item {name!r} which "
                f"is not on order {against!r} of {party!r}"
            )
        new_qty = line.fulfilled + qty
        if new_qty > line.qty:
            raise ValidationError(
                f"orders: {KINDS[kind][1].lower()} {ref!r} would fulfil {name!r} of "
                f"order {against!r} beyond its ordered {_plain(line.qty)} "
                f"(pending {_plain(line.qty - line.fulfilled)})"
            )
        line.fulfilled = new_qty


def _resolve_order_for_settle(
    states: dict[tuple[str, str, str], OrderState],
    order_ref: str,
    parties: set[str] | None,
) -> OrderState:
    """The order a settle allocation names: among ``parties`` when given
    (the validator's parties-moved), else a unique match by ref."""
    candidates = [
        s
        for (k, p, r), s in states.items()
        if k in ORDER_KINDS and r == order_ref and (parties is None or p in parties)
    ]
    if not candidates:
        scope = " of the parties this voucher moves" if parties is not None else ""
        raise ValidationError(
            f"orders: settle names order {order_ref!r} — no prior open sales or "
            f"purchase order carries that ref{scope}"
        )
    if len(candidates) > 1:
        raise ValidationError(
            f"orders: settle names order {order_ref!r} which is ambiguous — "
            f"{len(candidates)} parties carry that ref; the order's party must be "
            "billed by this voucher"
        )
    return candidates[0]


def _apply_settle(states: dict[tuple[str, str, str], OrderState], allocations: list[dict]) -> None:
    """A billing voucher's settle allocations: qty toward the named order's
    invoiced (SO) or billed (PO) counters. The allocation's resolved
    ``party`` (written by the validator at posting time) scopes the lookup;
    a missing party falls back to a unique-ref match."""
    for alloc in allocations:
        if not isinstance(alloc, dict):
            raise ValidationError("orders: settle entries must be objects")
        order_ref = _clean_name(alloc.get("order"), "settle.order")
        lines = alloc.get("lines")
        if not isinstance(lines, list) or not lines:
            raise ValidationError(f"orders: settle {order_ref!r} needs a non-empty lines list")
        party = str(alloc.get("party") or "").strip() or None
        order: OrderState | None = None
        if party is not None:
            for kind in ORDER_KINDS:
                order = states.get((kind, party, order_ref))
                if order is not None:
                    break
        if order is None:
            order = _resolve_order_for_settle(states, order_ref, {party} if party else None)
        if order.status() == "closed":
            raise ValidationError(
                f"orders: settle names order {order_ref!r} which is already fully invoiced/billed"
            )
        for line_alloc in lines:
            if not isinstance(line_alloc, dict):
                raise ValidationError(f"orders: settle {order_ref!r} lines must be objects")
            name = _clean_name(line_alloc.get("item"), "settle.lines.item")
            qty = _qty(line_alloc.get("qty", 1), "settle.lines.qty")
            line = order.lines.get(name)
            if line is None:
                raise ValidationError(
                    f"orders: settle {order_ref!r} bills item {name!r} which is not "
                    f"on that order of {order.party!r}"
                )
            if line.invoiced + qty > line.qty:
                raise ValidationError(
                    f"orders: settle would bill {name!r} of order {order_ref!r} beyond "
                    f"its ordered {_plain(line.qty)} (pending to bill "
                    f"{_plain(line.qty - line.invoiced)})"
                )
            line.invoiced += qty


def _sorted_documents(documents: list[PriorDocument]) -> list[PriorDocument]:
    """Oldest first by business date, voucher id breaking same-day ties —
    the order a deterministic replay needs (the bills fold's rule)."""
    return sorted(documents, key=lambda d: (d.voucher_date, d.voucher_id))


def _netted(documents: list[PriorDocument]) -> list[PriorDocument]:
    """Documents undone by a reversal net to zero: the reversal marks its
    original, and both are skipped (ADR-010/011) — a reversed order cancels,
    a reversed fulfilment restores."""
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    return [d for d in documents if d.reversal_of is None and d.voucher_id not in reversed_ids]


def fold(
    documents: list[PriorDocument], *, as_of: date | None = None
) -> tuple[dict[tuple[str, str, str], OrderState], dict[tuple[str, str], QuotationState]]:
    """Replay every posted orders document of the book, oldest first, into
    (order states keyed by (kind, party, ref), quotation positions keyed by
    (party, ref)). ``as_of`` windows the replay to documents dated on or
    before it — the position as of that date."""
    states: dict[tuple[str, str, str], OrderState] = {}
    quotes: dict[tuple[str, str], QuotationState] = {}
    for d in _netted(_sorted_documents(documents)):
        if as_of is not None and d.voucher_date > as_of:
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict):
            continue
        settle = doc.get("settle")
        if isinstance(settle, list):
            _apply_settle(states, settle)
        elif doc.get("kind") is not None:
            _apply_document(states, quotes, doc, d.voucher_id, d.voucher_no, d.voucher_date)
    return states, quotes


# --------------------------------------------------------- the transformer
def build_order_document(draft: VoucherDraft, resolver: Resolver) -> VoucherDraft:
    """The standalone order document mode: derive the memorandum pair for
    the document's grand total and move the draft onto the kind's own
    voucher-type series (plugin API 0.9 — the engine resolves the type
    after transformers, so the rewrite lands in the numbering)."""
    doc = draft.meta.get(META_KEY)
    if doc is None:
        return draft
    if not isinstance(doc, dict):
        raise ValidationError("orders: meta.orders must be an object")

    if doc.get("kind") is None:
        # a settle allocation: annotation only, no lines, no type rewrite
        return draft

    kind = doc.get("kind")
    if kind not in KINDS:
        raise ValidationError(f"orders: kind must be one of {', '.join(KINDS)}, got {kind!r}")
    _reject_conflicts(draft)

    code, name, side = KINDS[kind]
    party = _clean_name(doc.get("party"), "party")
    ref = _clean_name(doc.get("ref"), "ref")
    against = str(doc.get("against") or "").strip() or None

    # A delivery note / GRN line without its own rate is valued at the
    # parent order line's rate — it moves that order's quantity. Resolved
    # here (the fold at posting time) so the memo trace carries the value
    # of what moved; the validator's replay re-checks the structural
    # guards, and a missing parent is refused with its own clear error.
    if kind in ("delivery_note", "grn") and against is not None:
        parent_kind = PARENT_OF[kind][0]
        states, _quotes = fold(resolver.prior_documents(META_KEY))
        parent = states.get((parent_kind, party, against))
        if parent is None:
            raise ValidationError(
                f"orders: {KINDS[kind][1].lower()} {ref!r} references {against!r} — "
                f"no prior open {KINDS[parent_kind][1].lower()} of {party!r} "
                "carries that ref"
            )
        for item in doc["items"]:
            name = str(item.get("item") or "").strip()
            if item.get("rate") is None:
                parent_line = parent.lines.get(name)
                if parent_line is None:
                    # never borrowable and never valid: the validator would
                    # refuse it — refuse here with the structural truth
                    raise ValidationError(
                        f"orders: {KINDS[kind][1].lower()} {ref!r} moves item "
                        f"{name!r} which is not on order {against!r} of {party!r}"
                    )
                item["rate"] = str(parent_line.rate.normalize())

    subtotal, tax_total, grand_total = _document_total(doc)
    if grand_total <= 0:
        raise ValidationError(
            f"orders: {KINDS[kind][1].lower()} {ref!r} needs a positive total — "
            "give its items rates (a DN/GRN line without a rate takes the order's)"
        )

    # masters: the party ledger resolves/creates under the side's party
    # group (the invoicing pack's rule); the memo ledger under Suspense A/c
    party_ledger = resolver.ensure_ledger(party, PARTY_GROUPS[side])
    memo = resolver.ensure_ledger(MEMO_LEDGER, MEMO_GROUP)

    # the kind's own numbering series (created lazily, inside the posting
    # transaction — it rolls back with a failed post)
    vtype = resolver.ensure_voucher_type(code, name)
    draft.voucher_type = vtype.code

    doc["computed"] = {
        "party_ledger_id": party_ledger.id,
        "subtotal": str(subtotal),
        "tax_total": str(tax_total),
        "grand_total": str(grand_total),
    }

    note = f"{name.lower()} {ref} for {party}"
    lines = [
        DraftLine(
            dc="D",
            amount=grand_total,
            ledger_id=memo.id,
            ledger_name=memo.name,
            narration=note,
        ),
        DraftLine(
            dc="C",
            amount=grand_total,
            ledger_id=memo.id,
            ledger_name=memo.name,
            narration=note,
        ),
    ]
    return draft.with_lines(lines)


# ------------------------------------------------------------ the validator
def _parties_moved(draft: VoucherDraft, resolver: Resolver) -> set[str]:
    """The ledger names this voucher moves (by id or name, the bills pack's
    matching rule) that also have an open order — the scope a settle
    allocation may draw from."""
    names: set[str] = set()
    for line in draft.lines:
        if line.ledger_name:
            names.add(line.ledger_name)
        elif line.ledger_id is not None:
            ledger = resolver.ledger(id=line.ledger_id)
            if ledger is not None:
                names.add(ledger.name)
    return names


def validate_orders(draft: VoucherDraft, resolver: Resolver) -> None:
    """Runs after every transformer (the engine validates post-transform),
    so a settle allocation's carrier lines are final — document-mode or
    plain. Standalone documents are replayed with the guards live against
    what is already posted."""
    doc = draft.meta.get(META_KEY)
    if doc is None:
        return
    if not isinstance(doc, dict):
        raise ValidationError("orders: meta.orders must be an object")

    states, quotes = fold(resolver.prior_documents(META_KEY))

    if doc.get("kind") is not None:
        kind = doc.get("kind")
        if kind not in KINDS:
            raise ValidationError(f"orders: kind must be one of {', '.join(KINDS)}, got {kind!r}")
        code, _name, _side = KINDS[kind]
        party = _clean_name(doc.get("party"), "party")
        ref = _clean_name(doc.get("ref"), "ref")

        # an open order of the same kind and ref for the same party collides
        # (closed orders free their refs — the bills pack's rule)
        existing = states.get((kind, party, ref))
        if existing is not None and existing.status() != "closed":
            raise ValidationError(
                f"orders: {ref!r} collides with {party}'s open "
                f"{KINDS[kind][1].lower()} — settle it first or use a distinct ref"
            )

        # (the transformer has already moved the draft onto the kind's own
        # series — meta.orders.kind is the source of truth, any carrier type
        # normalizes; replay this document's effects with the guards live)
        _apply_document(states, quotes, doc, -1, "(this voucher)", draft.voucher_date)
        return

    # ---- a settle allocation rides a billing voucher
    settle = doc.get("settle")
    if settle is None:
        raise ValidationError(
            "orders: a meta.orders document needs a kind or a settle allocation — "
            "this one does nothing"
        )
    if not isinstance(settle, list) or not settle:
        raise ValidationError("orders: settle must be a non-empty list of objects")
    if any(not isinstance(a, dict) for a in settle):
        raise ValidationError("orders: settle must be a list of objects")

    # the parties this voucher actually moves scope the allocation (the
    # ADR-023 discipline — an allocation rides vouchers that describe it);
    # the resolved party is written back so the fold never guesses
    moved = _parties_moved(draft, resolver)
    for alloc in settle:
        order_ref = _clean_name(alloc.get("order"), "settle.order")
        # every open order of that ref, then the ones this voucher bills:
        # the carrier must move the order's party (the ADR-023 discipline)
        matches = [s for (k, _p, r), s in states.items() if k in ORDER_KINDS and r == order_ref]
        if not matches:
            raise ValidationError(
                f"orders: settle names order {order_ref!r} — no prior open sales or "
                "purchase order carries that ref"
            )
        moved_matches = [s for s in matches if s.party in moved]
        if not moved_matches:
            owners = ", ".join(sorted({s.party for s in matches}))
            raise ValidationError(
                f"orders: this voucher does not move party ledger {owners!r} — "
                "settlements ride vouchers that bill the party"
            )
        if len(moved_matches) > 1:
            raise ValidationError(
                f"orders: settle names order {order_ref!r} which is ambiguous — "
                f"{len(moved_matches)} parties this voucher moves carry that ref"
            )
        order = moved_matches[0]
        alloc["party"] = order.party
        alloc["kind"] = order.kind
    _apply_settle(states, settle)


# --------------------------------------------------------- event handler
def record_posted(event) -> None:  # noqa: ANN001 — DomainEvent from the stable API object
    meta = event.data.get("meta") or {}
    doc = meta.get(META_KEY)
    if not isinstance(doc, dict):
        return
    POSTED.append(
        {
            "voucher_no": event.data.get("voucher_no"),
            "book_id": event.data.get("book_id"),
            "kind": doc.get("kind"),
            "party": doc.get("party"),
            "ref": doc.get("ref"),
            "settle": len(doc.get("settle") or []),
        }
    )
    logger.info("orders document posted: %s", event.data.get("voucher_no"))


# ------------------------------------------------------------ the print view
def orders_view(ctx: DocumentContext) -> DocumentView | None:
    """The print layout for ``meta.orders`` documents (plugin API 0.5,
    ADR-013): each of the five kinds prints as the commercial document it
    is — items, tax and totals re-derived through the same exact-Decimal
    arithmetic the transformer posted under, static from the immutable
    stored document (reprints are identical, Law 2). Settle-carrying
    vouchers decline so the invoice's own view wins."""
    doc = ctx.meta.get(META_KEY)
    if not isinstance(doc, dict) or doc.get("kind") not in KINDS:
        return None
    kind = doc["kind"]
    _code, title, _side = KINDS[kind]
    items = [i for i in (doc.get("items") or []) if isinstance(i, dict)]

    rows: list[tuple[str, ...]] = []
    subtotal = Decimal("0.00")
    tax_total = Decimal("0.00")
    for index, item in enumerate(items, start=1):
        name = str(item.get("item") or f"item {index}")
        description = str(item.get("description") or "")
        qty = _decimal(item.get("qty", 1), "qty")
        rate = _decimal(item.get("rate", 0), "rate")
        amount = _money(qty * rate)
        tax_rate = str(item.get("tax_rate", "0"))
        tax = (
            _money(amount * _slab(tax_rate, "tax_rate") / _HUNDRED)
            if tax_rate in TAX_SLABS
            else Decimal("0.00")
        )
        subtotal += amount
        tax_total += tax
        rows.append(
            (
                name,
                description,
                f"{qty.normalize():f}",
                format_amount(rate),
                f"{tax_rate}%" if tax_rate != "0" else "-",
                format_amount(amount),
            )
        )

    fields: list[tuple[str, str]] = [
        ("Party", str(doc.get("party") or "")),
        ("Document ref", str(doc.get("ref") or "")),
        ("Date", ctx.voucher_date.isoformat()),
        ("Voucher", f"{ctx.voucher_type} {ctx.voucher_no}"),
    ]
    against = str(doc.get("against") or "").strip()
    if against:
        parent_kind = PARENT_OF.get(kind, (None, None))[0] or "quotation"
        parent_title = KINDS.get(parent_kind, ("", "", ""))[1]
        fields.append(("Against", f"{parent_title} {against}"))
    if doc.get("valid_until"):
        fields.append(("Valid until", str(doc["valid_until"])))
    if doc.get("deliver_by"):
        fields.append(("Deliver by", str(doc["deliver_by"])))

    footnotes = [
        "Quantities and values are the order document's own — the position "
        "this order reaches lives in the pending-orders report, not here "
        "(a reprint is always the document as posted, Law 2).",
        "Amounts are in the book's base currency, exact decimals (Law 3); "
        "tax shown at the declared slab is indicative — the tax invoice "
        "computes the actual split.",
        "Computer-generated from immutable posted entries (Law 2) — reprints are identical.",
    ]
    if doc.get("terms"):
        footnotes.insert(0, f"Terms: {doc['terms']}")

    return DocumentView(
        title=title,
        subtitle=ctx.book_name,
        reference=ctx.voucher_no,
        fields=tuple(fields),
        sections=(
            DocumentSection(
                title="Items",
                columns=(
                    DocumentColumn("Item", width=1.6),
                    DocumentColumn("Description", width=1.8),
                    DocumentColumn("Qty", width=0.6, align="right"),
                    DocumentColumn("Rate", width=0.9, align="right"),
                    DocumentColumn("Tax", width=0.6, align="right"),
                    DocumentColumn("Amount", width=1.0, align="right"),
                ),
                rows=tuple(rows),
            ),
        ),
        totals=(
            ("Subtotal", format_amount(subtotal)),
            ("Tax", format_amount(tax_total)),
            ("Total", format_amount(subtotal + tax_total)),
        ),
        footnotes=tuple(footnotes),
        filename=f"{title} {doc.get('ref') or ctx.voucher_no}",
    )


# ---------------------------------------------------------------- reports
def _parse_as_of(params: dict[str, str]) -> date:
    raw = (params.get("as_of") or "").strip()
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationError("orders: as_of must be YYYY-MM-DD") from None


_PENDING_COLUMNS = {
    SALES: ("Ordered", "Delivered", "Invoiced", "Pending delivery", "Pending invoice"),
    PURCHASE: ("Ordered", "Received", "Billed", "Pending receipt", "Pending bill"),
}


def _pending_rows(
    states: dict[tuple[str, str, str], OrderState],
    side: str,
    party: str | None,
    as_of: date,
) -> list[dict]:
    """Open/partial order lines on one side, oldest first: quantities,
    pending quantities, pending value, status."""
    order_kind = "sales_order" if side == SALES else "purchase_order"
    rows: list[dict] = []
    for key in sorted(states, key=lambda k: (states[k].date, states[k].voucher_id)):
        kind, p, ref = key
        if kind != order_kind:
            continue
        if party and p != party:
            continue
        state = states[key]
        if state.status() == "closed":
            continue
        for name in sorted(state.lines):
            line = state.lines[name]
            pending = line.qty - line.invoiced
            rows.append(
                {
                    "party": p,
                    "ref": ref,
                    "voucher_no": state.voucher_no,
                    "date": state.date.isoformat(),
                    "item": name,
                    "qty": line.qty,
                    "rate": line.rate,
                    "value": _money(line.qty * line.rate),
                    "fulfilled": line.fulfilled,
                    "invoiced": line.invoiced,
                    "pending": pending,
                    "pending_value": _money(pending * line.rate),
                    "status": state.status(),
                }
            )
    return rows


def pending_orders_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """Open order lines with pending quantity and value — the Phase 3
    acceptance report, sales (default) or purchase side. PDF (default),
    CSV or JSON."""
    side = (params.get("side") or SALES).strip().lower()
    if side not in (SALES, PURCHASE):
        raise ValidationError("orders: side must be sales or purchase")
    party = (params.get("party") or "").strip() or None
    as_of = _parse_as_of(params)

    states, _quotes = fold(resolver.prior_documents(META_KEY), as_of=as_of)
    rows = _pending_rows(states, side, party, as_of)
    label = "Pending sales orders" if side == SALES else "Pending purchase orders"
    columns = _PENDING_COLUMNS[side]

    by_party: dict[str, Decimal] = {}
    total_pending = Decimal("0.00")
    total_value = Decimal("0.00")
    for row in rows:
        by_party[row["party"]] = by_party.get(row["party"], Decimal("0.00")) + row["pending_value"]
        total_pending += row["pending"]
        total_value += row["pending_value"]

    data = {
        "side": side,
        "as_of": as_of.isoformat(),
        "party": party,
        "rows": [
            {
                "party": r["party"],
                "ref": r["ref"],
                "voucher_no": r["voucher_no"],
                "date": r["date"],
                "item": r["item"],
                "qty": str(r["qty"]),
                "rate": str(r["rate"]),
                "fulfilled": str(r["fulfilled"]),
                "invoiced": str(r["invoiced"]),
                "pending": str(r["pending"]),
                "pending_value": str(r["pending_value"]),
                "status": r["status"],
            }
            for r in rows
        ],
        "party_totals": {p: str(v) for p, v in sorted(by_party.items())},
        "total_pending": str(total_pending),
        "total_pending_value": str(total_value),
    }
    title = f"{label} {as_of.isoformat()}"
    if party:
        title += f" — {party}"

    if (params.get("format") or "").lower() == "csv":
        lines = [
            f"party,ref,voucher,date,item,{columns[0].lower()},{columns[1].lower()},"
            f"{columns[2].lower()},{columns[3].lower()},{columns[4].lower()},"
            "pending_value,status"
        ]
        lines += [
            f"{r['party']},{r['ref']},{r['voucher_no']},{r['date']},{r['item']},"
            f"{_plain(r['qty'])},{_plain(r['fulfilled'])},{_plain(r['invoiced'])},"
            f"{_plain(r['qty'] - r['fulfilled'])},{_plain(r['pending'])},"
            f"{r['pending_value']},{r['status']}"
            for r in rows
        ]
        total_qty = sum((r["qty"] for r in rows), Decimal(0))
        lines.append(f"TOTAL,-,-,-,-,{_plain(total_qty)},-,-,-,-,{total_value},-")
        return PackReport(title=title, filename=title, csv="\n".join(lines) + "\n", data=data)

    view = DocumentView(
        title=label,
        subtitle=f"positions as of {as_of.isoformat()}",
        fields=(
            ("Side", SALES if side == SALES else PURCHASE),
            ("As of", as_of.isoformat()),
            ("Open lines", str(len(rows))),
            ("Parties", str(len(by_party))),
        ),
        sections=(
            DocumentSection(
                title="Open order lines",
                columns=(
                    DocumentColumn("Party", width=1.4),
                    DocumentColumn("Order", width=1.0),
                    DocumentColumn("Date", width=0.85),
                    DocumentColumn("Item", width=1.4),
                    DocumentColumn(columns[0], width=0.7, align="right"),
                    DocumentColumn(columns[1], width=0.7, align="right"),
                    DocumentColumn(columns[2], width=0.7, align="right"),
                    DocumentColumn(columns[4], width=0.7, align="right"),
                    DocumentColumn("Pending value", width=1.0, align="right"),
                ),
                rows=tuple(
                    (
                        r["party"],
                        r["ref"],
                        r["date"],
                        r["item"],
                        _plain(r["qty"]),
                        _plain(r["fulfilled"]),
                        _plain(r["invoiced"]),
                        _plain(r["pending"]),
                        format_amount(r["pending_value"]),
                    )
                    for r in rows
                ),
            ),
        ),
        totals=(
            *((f"Pending — {p}", format_amount(v)) for p, v in sorted(by_party.items())),
            ("Total pending value", format_amount(total_value)),
        ),
        footnotes=(
            "Positions replayed from posted meta.orders documents with "
            "reversals netted (Law 2) — derived, never stored.",
            f"Status: open (nothing billed) / partial / closed (fully billed, "
            f"excluded). {columns[3]} = {columns[0].lower()} − {columns[1].lower()}; "
            f"{columns[4]} = {columns[0].lower()} − {columns[2].lower()}.",
            "Pending value is pending quantity × the order line's rate, exact "
            "decimals rounded once (Law 3) — the invoice, not this report, "
            "decides what is actually billed.",
        ),
        filename=f"{label} {as_of.isoformat()}",
    )
    return PackReport(title=title, filename=title, view=view, data=data)


def orders_open_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """One kind's open documents with per-line positions — the picker feed
    (the ``bills-open`` pattern). JSON for UIs, CSV for machines, PDF for
    the wall."""
    kind = (params.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise ValidationError(f"orders: kind must be one of {', '.join(KINDS)} (got {kind!r})")
    party = (params.get("party") or "").strip() or None
    as_of = _parse_as_of(params)
    _code, title, _side = KINDS[kind]

    states, quotes = fold(resolver.prior_documents(META_KEY), as_of=as_of)
    documents: list[dict] = []
    if kind == "quotation":
        for (p, ref), quote in sorted(
            quotes.items(), key=lambda kv: (kv[1].date, kv[1].voucher_no)
        ):
            if party and p != party:
                continue
            documents.append(
                {
                    "party": p,
                    "ref": ref,
                    "voucher_no": quote.voucher_no,
                    "date": quote.date.isoformat(),
                    "status": "converted" if quote.converted else "open",
                    "items": [
                        {"item": name, "converted": str(qty)}
                        for name, qty in sorted(quote.converted.items())
                    ],
                }
            )
    else:
        for key in sorted(states, key=lambda k: (states[k].date, states[k].voucher_id)):
            k, p, ref = key
            if k != kind or (party and p != party):
                continue
            state = states[key]
            if state.status() == "closed":
                continue
            documents.append(
                {
                    "party": p,
                    "ref": ref,
                    "voucher_no": state.voucher_no,
                    "date": state.date.isoformat(),
                    "status": state.status(),
                    "items": [
                        {
                            "item": name,
                            "qty": str(line.qty),
                            "rate": str(line.rate),
                            "fulfilled": str(line.fulfilled),
                            "invoiced": str(line.invoiced),
                            "pending_deliver": str(line.qty - line.fulfilled),
                            "pending_bill": str(line.qty - line.invoiced),
                        }
                        for name, line in sorted(state.lines.items())
                    ],
                }
            )

    data = {"kind": kind, "party": party, "as_of": as_of.isoformat(), "documents": documents}
    report_title = f"Open {title.lower()}s {as_of.isoformat()}"
    if party:
        report_title += f" — {party}"

    if (params.get("format") or "").lower() == "csv":
        lines: list[str] = []
        if kind == "quotation":
            lines = ["party,ref,voucher,date,status,item,converted"]
            for d in documents:
                items = d["items"] or [{"item": "-", "converted": "-"}]
                for item in items:
                    lines.append(
                        f"{d['party']},{d['ref']},{d['voucher_no']},{d['date']},"
                        f"{d['status']},{item['item']},{item['converted']}"
                    )
        else:
            lines = ["party,ref,voucher,date,status,item,qty,rate,fulfilled,invoiced,pending"]
            for d in documents:
                for item in d["items"]:
                    lines.append(
                        f"{d['party']},{d['ref']},{d['voucher_no']},{d['date']},{d['status']},"
                        f"{item['item']},{item['qty']},{item['rate']},{item['fulfilled']},"
                        f"{item['invoiced']},{item['pending_bill']}"
                    )
        return PackReport(
            title=report_title, filename=report_title, csv="\n".join(lines) + "\n", data=data
        )

    # PDF view: the compact position list
    section_rows: list[tuple[str, ...]] = []
    for d in documents:
        if kind == "quotation":
            items = d["items"] or [{"item": "-", "converted": "-"}]
            for item in items:
                section_rows.append(
                    (
                        d["party"],
                        d["ref"],
                        d["date"],
                        d["status"],
                        item["item"],
                        item["converted"],
                        "-",
                    )
                )
        else:
            for item in d["items"]:
                section_rows.append(
                    (
                        d["party"],
                        d["ref"],
                        d["date"],
                        d["status"],
                        item["item"],
                        item["qty"],
                        item["pending_bill"],
                    )
                )
    if kind == "quotation":
        columns = (
            DocumentColumn("Party", width=1.4),
            DocumentColumn("Ref", width=1.0),
            DocumentColumn("Date", width=0.85),
            DocumentColumn("Status", width=0.9),
            DocumentColumn("Item", width=1.3),
            DocumentColumn("Converted", width=0.9, align="right"),
            DocumentColumn("Pending", width=0.8, align="right"),
        )
    else:
        columns = (
            DocumentColumn("Party", width=1.4),
            DocumentColumn("Ref", width=1.0),
            DocumentColumn("Date", width=0.85),
            DocumentColumn("Status", width=0.7),
            DocumentColumn("Item", width=1.3),
            DocumentColumn("Qty", width=0.7, align="right"),
            DocumentColumn("Pending", width=0.8, align="right"),
        )
    view = DocumentView(
        title=f"Open {title.lower()}s",
        subtitle=f"as of {as_of.isoformat()}",
        fields=(
            ("Kind", title),
            ("As of", as_of.isoformat()),
            ("Documents", str(len(documents))),
        ),
        sections=(
            DocumentSection(
                title="Open documents",
                columns=columns,
                rows=tuple(section_rows),
            ),
        ),
        totals=(("Documents", str(len(documents))),),
        footnotes=(
            "The picker feed: every open document of this kind with its "
            "per-line position, replayed from posted documents (Law 2).",
            "Pending is the quantity still to bill (orders) — delivery and "
            "receipt counters are exposed in the JSON feed for the pickers.",
        ),
        filename=f"Open {title.lower()}s {as_of.isoformat()}",
    )
    return PackReport(title=report_title, filename=view.filename, view=view, data=data)
