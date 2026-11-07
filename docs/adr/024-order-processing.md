# ADR-024: Order processing — five order documents, a zero-net memorandum trace, and the position fold

- **Status:** Accepted (2026-11-08)
- **Deciders:** mir-ashiq
- **Supersedes:** —
- **Extends:** ADR-010 (voucher meta), ADR-011 (prior_documents fold), ADR-013 (print bridge), ADR-014 (pack reports), ADR-023 (the annotation-layer pattern)

## Context

The quote-to-cash and procure-to-pay chains are where TallyPrime users live: a quotation
becomes a sales order, goods go out on a delivery note, the invoice bills the order, and
the purchase side mirrors it (purchase order → GRN → purchase bill). ROADMAP.md §3 Phase 3
(v0.21.0) commits to exactly that, as the fifth plugin pack:

- Quotation, Sales Order, Delivery Note, Purchase Order, GRN — five document types as one
  pack on the `meta` store, each a print-claiming document view (ADR-013);
- order position tracking — pending quantity, fulfilled quantity, order status — from a
  deterministic fold, plus **pending orders** reports on both the sales and purchase sides;
- order → invoice linkage (an invoice settles against an order; the order position updates)
  and PO → GRN → bill linkage, the same mechanism;
- the native window gains the document pickers alongside vouchers.

Two structural facts shaped the design, and one honest constraint.

First, **orders are not accounting**. TallyPrime's order vouchers touch neither ledgers nor
the trial balance — they move order books only. LedgerForge's kernel, correctly, only knows
vouchers: balanced Dr/Cr, positive amounts, append-only. So an order document must post as a
voucher *without pretending to be one*. Second, **fulfilment is quantity, not money**: the
position fold tracks quantities per order line (delivered, received, invoiced, billed), and
a money-conservation law like ADR-023's would be dishonest here — an invoice against an
order bills quantities, and its amount is the invoicing pack's business, not the orders
pack's. Third, the constraint: order vouchers deserve their own numbering series (QT-…,
SO-…, DN-…, PO-…, GRN-… — TallyPrime's muscle memory), but voucher types are *book data*
seeded per book, and no plugin has ever been able to create one.

## Decision

### 1. `meta.orders` — two roles, the ADR-023 shape

One document key, two roles:

- **Standalone order documents** (the line-builders). `meta.orders = {"kind": …, "party": …,
  "ref": …, "items": […], …}` with `kind` one of `quotation`, `sales_order`, `delivery_note`,
  `purchase_order`, `grn`. Items are `{item, description?, qty, rate, tax_rate?}` — qty > 0,
  rate ≥ 0, tax_rate a GST slab (the same whitelist the invoicing pack enforces; tax on an
  order is display-only, the invoice computes the real split). The transformer derives the
  computed block (`subtotal`/`tax_total`/`grand_total`, exact Decimal, the invoicing pack's
  arithmetic) and posts **one Dr + one Cr line on the memorandum ledger** for the grand
  total (§2). `against` links the chain: a sales order may convert a quotation, a delivery
  note fulfils a sales order, a GRN fulfils a purchase order — always same-party, always by
  ref. `valid_until` (quotations) and `deliver_by` (sales orders) are facts, printed, never
  policy.
- **Settlement allocations** (the annotation layer — never a line-builder). A billing
  voucher — a `meta.invoice` sales invoice, a plain purchase voucher, a note — carries
  `meta.orders = {"settle": [{"order": "SO-42", "lines": [{"item": "Widgets", "qty": "8"}]}]}`.
  Like bills allocations, it rides whatever builds the lines; unlike bills it conserves
  nothing (quantities, not money) — its guard is positional: the order must exist, belong
  to the same party, and **the carrying voucher must move that party's ledger**, the same
  discipline ADR-023 uses to keep allocations from riding vouchers they don't describe.

### 2. Orders post as a zero-net memorandum trace

The five kinds post Dr "Order Book (memo)" / Cr "Order Book (memo)" for the document's grand
total — one seeded group away from every financial statement (under *Suspense A/c*), a
ledger whose balance is **zero by construction** (every voucher that touches it debits and
credits it equally). The trial-balance figure never moves; the day book shows the order at
its value (the debit-side total the daybook column already computes); the voucher is
append-only, reversible, and idempotent like any other. This is the honest simulation of
"non-accounting voucher" inside Law 1 — the alternative (a real Dr/Cr on invented control
accounts) would fabricate a financial position the business does not have. The tests assert
the neutrality: after a full order chain, the memo ledger's trial-balance balance is 0.00
and the party ledgers are untouched by order vouchers.

### 3. Plugin API 0.9 — `Resolver.ensure_voucher_type(code, name, prefix, restart_yearly)`

Book-scoped and idempotent, the `ensure_ledger` pattern applied to `VoucherType`. The
engine resolves a draft's voucher type **after** transformers run, so the orders pack's
transformer rewrites `draft.voucher_type` to the document's kind code and ensures the type
exists — inside the posting transaction (it rolls back with a failed post). Orders get their
own gapless yearly-restarting series: `QT-…`, `SO-…`, `DN-…`, `PO-…`, `GRN-…`. Voucher
types are data, not schema: **zero migration**. Additive; the floor semantics of ADR-011
keep every 0.2–0.8 pack loading. The reversal path needs no change: `reverse_voucher`
already reuses the original's type, so a cancelled order reverses within its own series.

### 4. The position fold

`prior_documents("orders")`, oldest first by `(voucher_date, voucher_id)`, reversals
netted (ADR-010/011 — reversing an order cancels it; reversing a delivery note, GRN or
settling invoice restores the position; an undone pair skips entirely). Three positions:

- **Quotation lines**: quoted qty/rate; `converted` accumulates when sales orders name the
  quotation via `against`. Informational — a quotation never blocks anything and never
  appears in pending reports.
- **Sales order lines**: `qty` (ordered), `delivered` (from delivery notes against it),
  `invoiced` (from settle allocations). Status: `open` (nothing invoiced) → `partial` →
  `closed` (every line fully invoiced). Pending-to-deliver is qty − delivered;
  pending-to-invoice is qty − invoiced.
- **Purchase order lines**: `qty`, `received` (from GRNs), `billed` (from settle
  allocations), same status ladder on billed.

The guards are the caps, applied at posting time against the live fold and replayed
identically later: cumulative delivered ≤ ordered per line, cumulative received ≤ ordered,
cumulative invoiced ≤ ordered, cumulative billed ≤ ordered — per item, per order, same
party, order must exist. Over-fulfilment is refused with the pending figure in the message.
A duplicate `ref` collides only with the party's *open* order of the same kind (closed
orders free their refs, the bills pack's rule). A settle allocation on a standalone order
document is refused (the DN fulfils delivery, it does not bill), and a standalone order
document is mutually exclusive with every other line-building document key (invoice,
receipt, stock, payroll, gst, bills `interest_charge`) — the cross-pack rule since
ADR-011. Bills allocations and an orders settle may share one voucher: two annotation
layers, both validated against final lines.

### 5. Reports and surfaces

Registered reports (ADR-014; PDF through the print bridge, CSV for machines, JSON for UIs —
plugin API 0.8):

- `pending-orders` — `?side=sales|purchase` (default sales), optional `party`, `as_of`:
  one row per open order line with ordered/fulfilled/invoiced or received/billed, pending
  quantity and pending value (qty × rate, exact), status; per-party subtotals and the
  grand total. The Phase 3 acceptance report, both sides.
- `orders-open` — `?kind=…` (one of the five), optional `party`: the open documents of
  that kind with per-line positions — the picker feed, the `bills-open` pattern. JSON for
  the native pickers, CSV for machines.

Print (ADR-013): `register_document_view("orders", …)` claims the five kinds — a
quotation, order, delivery note or GRN prints as the commercial document it is (party,
ref, items, tax, totals, terms), static from the immutable stored document (reprints are
identical — Law 2); settle-carrying vouchers decline (the invoice's own view wins) and
print as the tax invoice.

Native window (ADR-022): an **Orders page** joins the sidebar (Day Book, Masters, Orders,
Reports, Periods, Sandbox — the page keys renumber, Alt+3 is now Orders) listing the five
types' vouchers with kind/party/ref/value/status, a **New Order dialog** (kind, party of
the right side, ref, items grid, against-picker fed by `orders-open`), and the voucher
form's **order allocation picker** — *Order…* on a party-moving voucher, offering that
party's open orders with pending-to-invoice per line, arming `meta.orders.settle` on the
post. The document pickers alongside vouchers, as the roadmap says.

## Consequences

- **(+)** Phase 3's whole scope lands with **zero schema change and zero kernel law
  change** — the fourth read-heavy pack on the ADR-011 pattern, and the second (after
  bills) whose correctness is a fold invariant the tests assert: Σ pending quantity is
  always ordered − fulfilled, every report a deterministic replay.
- **(+)** The settle-annotation is pack-agnostic by construction: it validates against
  final lines, so it rides `meta.invoice`, plain vouchers, notes — and Phase 5's banking
  or Phase 7's GST flows can settle orders without this pack changing again.
- **(+)** Order numbering is Tally-faithful per kind, per book, per year — and
  `ensure_voucher_type` is the sanctioned seam for any future pack that needs its own
  document series (banking's PDC, payroll's loans).
- **(−)** The memorandum trace doubles each order in the memo ledger's entries (Dr and Cr
  of the same amount). Visible, boring, and the price of Law 1; the ledger's balance is
  always exactly zero and its statement says so.
- **(−)** The fold is O(all orders documents of the book) per validation and report — the
  same ADR-011 trade-off as inventory and bills, fine at SMB scale, same escape hatch
  (promote to schema when a book outgrows it).
- **(−)** `against`-ref validation serializes on the document fold, not a lock — two
  concurrent delivery notes over-shipping the same order line can both read the fold; the
  cap refusal at posting time is the guard, not a proof of global serializability
  (identical to bills' over-settlement window, and parties ship at human speed).
- **(−)** v0.21 scope, documented: no order amendment (Law 2 — reverse and re-post, like
  every correction); no partial-line cancellation without reversal; no price-list lookup
  (Phase 6); no web orders screen (the native window is Phase 3's surface, the REST API
  carries the web's future one); delivery notes and GRNs move *order* positions, not
  *stock* — the inventory pack's four movements own stock, deliberately unlinked here so
  the packs stay composable across vouchers (Phase 6 may join them).

Shipped with no migration: `alembic` head unchanged. The consumer is
`ledgerforge/contrib/orders.py`, registered as an entry point and loaded at boot;
exercised by `tests/test_orders_plugin.py` (document shapes, memo neutrality against the
trial balance, per-kind numbering, the position fold and its caps, quotation conversion,
reversal netting, the pending-orders and orders-open reports in all three formats, print
views, book isolation) and the offscreen native suite for the Orders page, the New Order
dialog and the allocation picker.
