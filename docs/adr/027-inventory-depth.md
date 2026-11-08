# ADR-027: Inventory depth — batches, serials, expiry, godowns, re-order levels and manufacturing

- **Status:** Accepted (2026-11-08)
- **Deciders:** mir-ashiq
- **Supersedes:** —
- **Extends:** ADR-010 (the meta store — movements stay documents), ADR-011 (the inventory pack — this is its depth promotion, foreseen in its own consequences), ADR-025 (the schema-exception precedent — masters get tables), ADR-026 (the kernel/pack division of labour), ADR-023 (the meta-fold precedent)

## Context

ROADMAP.md §3 Phase 6 (v0.24.0, issue #12) asks for the depth TallyPrime
users call "real inventory": batch-wise and serial-number tracking with
expiry management, godowns as a first-class dimension, re-order levels with
a shortfall report, per-party date-effective price lists, and manufacturing
(BOM definitions plus a manufacturing journal that rolls component costs
into finished goods at their moving averages). ADR-011 shipped inventory
with weighted-average valuation over document folds and honestly listed
all of this as out of scope, with the escape hatch: "a production pack
that needs these promotes to schema per ADR-010 — or ships as a richer
plugin using the same primitives." Phase 6 is that promotion, taken
deliberately.

Three facts shaped the design.

**First — movements are already documents, and depth is more of the same.**
A batch allocation, a serial number, a godown key and an expiry date are
annotations on a stock movement, not financial facts of their own. The
line `Amoxycillin 500 × 100 @ 12.50` becomes
`Amoxycillin 500 × 100 @ 12.50, batch AMX-114, expiry 2027-03-31, godown Cold Room`
— the same voucher, the same Cr supplier / Dr inventory legs, the same
2-dp line amounts. The ADR-011 fold therefore extends, it is not replaced:
`_ItemState` keeps its `(qty, value)` — **valuation stays the moving
weighted average at the item level, exactly as the roadmap demands** — and
gains quantity sub-ledgers: per godown, per batch, per serial. The
sub-ledgers are derived state replayed by the same fold (nothing stored
that can be recomputed), and reversal netting works unchanged: a reversal
carries empty meta, so both the original and its mirror are skipped.

**Second — masters are not movements, and they need identity.** A stock
item's re-order level, a price-list row, a BOM: these are *master data* —
referenced by name from documents and by report after report, stable
across thousands of movements. Riding them on vouchers would be absurd
(Law 1: a BOM is not a double-entry event; the placeholder-entry trick of
the orders pack buys nothing here), and riding them on document folds
buys the wrong thing: no database identity (two "documents" can claim
different re-order levels for the same item name — the check-then-insert
race ADR-025 rejected for allocations), no date-effective queries (price
lookups scan everything), no referential integrity (a BOM naming a
component that does not exist). So Phase 6 is the **third core-schema
change since 0006**, landing as migration `0009_stock_depth`, by the same
ADR-025 argument as cost centres and banking: `stock_items`,
`stock_prices`, `stock_boms` + `stock_bom_lines` — masters with UNIQUE
names per book, FK-validated components, and date-effective price rows.
The kernel stays meta-blind (ADR-010): it never reads `vouchers.meta`;
the pack never writes the tables directly — it reads masters through the
versioned plugin API.

**Third — godowns must not become "another ledger" twice.** ADR-011's
documented limitation: godowns were value-level *ledgers*, so issuing
from a godown whose ledger happened to be short drove that ledger
negative while the item position stayed correct. Phase 6 makes godown a
**location key on movements and positions**: every stock line may carry
`godown` (default `"Main location"` — TallyPrime's own default), the fold
tracks `(item, godown) → qty`, and the guards refuse what the position
refuses: an issue cannot take quantity a godown does not hold, whatever
the ledgers say. Per-godown *ledgers* remain available (the
`inventory_ledger` override is untouched) for operators who want
value-level godown isolation — the two models compose. One boundary is
deliberate: a transfer still posts its value pair between two *ledgers*
(ADR-011's shape, unchanged), now with quantity semantics — the source
line's `godown` loses, `to_godown` gains. A pure same-ledger relocation
(zero accounting impact) has no voucher to ride: Law 1 forbids wash
entries, and we refuse to invent them. The escape hatch, documented for
the day it is needed, is the ADR-026 statement-table precedent — an
append-only relocations table as kernel schema; Phase 6 does not need it.

## Decision

### 1. Migration `0009_stock_depth` — the master tables (ADR-025 discipline)

- **`stock_items`** — one row per named stock item per book:
  `name` UNIQUE with the book, `unit`, `track_batches`, `track_serials`
  (the TallyPrime "maintain in batches / serial numbers" toggles),
  `reorder_level`, `reorder_quantity` (both NUMERIC, both optional),
  `notes`. Append-only in spirit: UPDATE allowed (masters are revised —
  the cost-category precedent), DELETE RESTRICTed by BOM references.
- **`stock_prices`** — the price lists: `item_id` FK, `party_ledger_id`
  FK nullable (NULL = the list price for everyone), `effective_from`
  DATE, `rate` NUMERIC exact. The effective price for `(item, party,
  date)` is the row with the latest `effective_from ≤ date`, a
  party-specific row beating the generic one at the same date — one SQL
  query, no fold.
- **`stock_boms` + `stock_bom_lines`** — the recipe: a BOM names its
  output item and a per-run output quantity; its lines name component
  items with a quantity per output unit. Output and components must be
  registered stock items (validated FKs — a BOM cannot name a ghost).
  UNIQUE `(book, name)` and UNIQUE `(bom, component)`.

No `rpt.*` changes: the reports are pack folds (positions derive from
documents, and the kernel is meta-blind), plus master reads through the
plugin API's resolver handles.

### 2. Plugin API 0.12 — the stock-master reads on `Resolver`

Four read-only handles, the `ledger`/`group` discipline applied to stock
masters — frozen data objects, never ORM rows, book-scoped always:

- `stock_item(name=...)` — the item master or `None`;
- `stock_items()` — every item master of the book (the shortfall fold);
- `stock_bom(name=...)` — a BOM with its components resolved to names;
- `stock_price(item, party=None, on_date=None)` — the effective rate or
  `None` (the date-effective lookup as one call).

The ADR-011 floor keeps every older pack loading. The inventory pack
declares `0.12`.

### 3. The depth document grammar (movements, `meta.stock`)

Every item line of the four ADR-011 movements may now carry:

| Key | Meaning | Guard |
|---|---|---|
| `godown` | the location of the movement's item side (in: where it lands; out: where it leaves; transfer: the source; recount: where counted) | default `"Main location"`; godown positions never go negative (out/transfer/consumption) |
| `batch` | the batch identity | receipts may create or add to a batch; issues without a batch are **FEFO-allocated**; an explicit batch must hold the quantity |
| `expiry` | the batch's expiry date (ISO) | only on goods-in, only with a batch; the first receipt fixes it, later receipts of the same batch must agree (or omit) |
| `serials` | the serial numbers moving (list) | `len(serials) == qty`; on goods-in each serial must be new to the item; on issue each must be in stock (at the godown, in the batch, when those are given) |
| `to_godown` | the transfer's destination location | required when a transfer carries godown semantics |

A fifth movement joins the four: **`manufacture`** — the manufacturing
journal (§5). The grammar stays additive: old documents replay
byte-identically (`_apply_line`'s `(qty, value)` arithmetic is untouched;
depth sub-ledgers default empty), so every existing book upgrades in
place with migration 0009 and no document rewrite.

**Tracking modes are enforced when declared.** An item master with
`track_batches` set makes batch mandatory on its goods-in lines (a
receipt must declare its batch — identity is never auto-invented) and
FEFO-automatic on issues (the roadmap's "FEFO suggestion": omit the batch
and the fold picks the earliest-expiry batches first, ties by batch name,
the allocation recorded in `computed` — deterministic, replayable,
visible). `track_serials` makes serials mandatory on goods-in and issues
both (serials are identity; they are never auto-assigned). Items without
masters keep the ADR-011 string-item freedom: depth keys honoured when
present, nothing demanded.

**Expired stock issues honestly, never silently.** A batch past its
expiry can still be issued (businesses do reclaim, retest, scrap) — the
document's `computed` flags every expired batch it consumed, and the
expiry-ageing report carries the mass. Valuation never notices expiry:
value follows quantity at the moving average, as the roadmap pins.

### 4. Godown-wise quantity positions (the fold)

`_ItemState` gains `godowns: {name → qty}`, `batches: {batch → (qty,
expiry, {godown → qty})}` and `serials: {serial → (batch, godown)}`. The
guards, at posting time only (the replay trusts its own history):

- an issue (or a transfer, or a manufacturing consumption) cannot take
  more from `(item, godown)` than it holds — `policy:allow_negative_stock`
  relaxes the *item* check on issues exactly as before, and the godown
  sub-ledger follows the item overdraft (documented; transfers and
  consumptions stay strict — relocation and production are physical);
- a batch position cannot go negative, at the godown and in total;
- a serial is one physical thing: receiving a serial the item already
  holds is a 422 (duplicates are a data error, not an override policy);
  issuing one that is not in stock, or is at another godown / another
  batch than the line names, is a 422;
- a recount may target `(item, godown)` — default Main location — or
  `(item, batch, godown)`; the counted quantity is absolute for that
  sub-position, the item-level `(qty, value)` arithmetic is exactly
  ADR-011's (average cost, explicit rate when counting up from empty).

### 5. The manufacturing journal (`movement: "manufacture"`)

```
{"movement": "manufacture",
 "bom": "Desk-STD",                     # optional — the named recipe
 "components": [{"item": "Plank", "qty": "6", "godown": "Main location",
                  "batch": "PLK-9", "serials": [...]}],
 "output": {"item": "Desk", "qty": "2", "godown": "Main location",
             "batch": "DSK-1", "expiry": null},
 "charges": [{"ledger": "Production Wages", "amount": "450.00"}]}
```

- **Consumption**: each component line is an issue — moving average,
  FEFO when batch omitted, godown/serial guards — Cr the component's
  inventory ledger at cost.
- **Production**: exactly one output line; Dr the output's inventory
  ledger at the rolled-up total. The output may open a new batch (with
  expiry) at a godown — receiving identity the operator controls.
- **Charges**: additional cost legs, each `{"ledger", "amount"}` — Cr an
  operator-named ledger (wages, power, freight — any ledger the book
  already has outside Stock-in-Hand; the pack refuses a charge against a
  stock ledger, which would be valuing stock with stock). Charges are
  how labour enters the rollup; without them the journal is pure
  material cost.
- **The cost rollup is closed arithmetic**: `output value = Σ component
  costs + Σ charges`, each component cost the exact 2-dp money the fold
  will replay (`money(q × moving average)`), the total distributed to
  the single output line — so the output's value lands as one exact
  amount and its unit cost is simply `total / qty` at 50-digit
  precision. No rounding residue can exist: one output, one amount.
- **With a BOM**: the component set must be exactly the BOM's items
  (extras and omissions are clean 422s) and each component quantity must
  cover `qty_per_output × output_qty` (excess consumption is allowed —
  wastage is real; under-consumption against the recipe is refused).
  Without a BOM: the explicit journal, validated by the same stock
  guards, no recipe to check.
- **Posting shape** (the TallyPrime manufacturing journal): Dr finished
  goods × 1, Cr each component × N, Cr each charge × K — balanced by
  construction, one voucher, the pack's most ambitious fold.
- **Reversal**: empty-meta reversal, both sides skipped by the fold —
  components restored, output removed, charges' legs mirrored by the
  engine as ordinary voucher lines. Net zero, exactly as every other
  movement.

### 6. The reports (pack surfaces — one fold, three renderings)

Registered through `register_report` (API 0.6/0.8): PDF through the
shared print-bridge layout, CSV for machines, JSON (`?format=json`) for
the native and web UIs — the same numbers, never two maths:

- **`stock-position`** — every item: qty, value, average cost, the
  godown and batch breakdowns, serial counts. The positions browser.
- **`stock-godown`** — the godown × item matrix with totals per godown.
- **`stock-expiry`** — batch-wise expiry ageing: expired, ≤30, ≤60, ≤90,
  >90 days, quantity and value-at-average per batch, sortable cut date.
- **`stock-shortfall`** — items at or below their re-order levels:
  on-hand, level, below-by, and the suggested order (the re-order
  quantity); items without masters are absent by definition.
- **`stock-batch`** — batch-wise positions with expiry and godown split.

Price lists are masters (kernel API: CRUD + the effective lookup), not
reports; the goods-out document's `computed` annotates the effective
price-list rate when a party is named — **informational, never applied**
(the matchmaker's law: suggestions never commit; the selling rate stays
the operator's explicit entry).

### 7. The kernel/pack division (the ADR-026 pattern, restated)

- **Kernel `ledgerforge/inventory.py`**: the tables' service layer —
  item/price/BOM create-read-update with the guards (unique names, FK
  components, positive quantities and rates, charge-ledger sanity is
  pack-side), the effective-price lookup, book scoping. Meta-blind.
- **Kernel API `api/inventory.py`**: `/api/v1/inventory/items`,
  `/prices`, `/boms` (+ the `/prices/effective` lookup). Reads need the
  reader role, writes the bookkeeper — the codebase's rule.
- **Pack `contrib/inventory.py`**: everything that interprets
  `meta.stock` — the depth grammar, the guards, FEFO, the manufacture
  fold, the reports, and the `stock` document print view (receipt, issue,
  transfer, recount, manufacturing journal layouts — ADR-013's bridge).
- **Native `nativeapp/inventory.py` + web `InventoryScreen`**: the
  Inventory page — masters (items with re-order levels, price lists,
  BOMs), the positions browser with godown/batch depth, and the
  shortfall/expiry/godown reports one key away.

## Consequences

- **(+)** The comparison table's biggest gap closes at parity's depth:
  batch/serial/expiry, godowns, re-order with shortfall, price lists,
  manufacturing — each with the exact-decimal discipline the kernel
  promises, and every position a deterministic replay.
- **(+)** The promotion is surgical: four new tables (masters only), one
  resolver minor, one pack module grown in place — no `rpt.*` change, no
  document rewrite, old books upgrade with `alembic upgrade head`.
- **(+)** FEFO is a pure function of the fold — the same allocation the
  report shows, recorded in the document's `computed`, so an issue is
  auditable against exactly what the fold knew.
- **(−)** The fold now carries sub-ledgers per item — the O(all stock
  documents) cost of ADR-011 grows a constant factor (the sub-ledger
  updates), not an asymptotic one; the promotion-to-schema escape hatch
  (positions as tables) remains the answer at warehouse scale.
- **(−)** Batch/serial guards make posting stricter for tracked items —
  deliberately: identity discipline is the feature. Existing documents
  (no depth keys) replay unchanged and post as before.
- **(−)** Pure same-ledger godown relocation is refused rather than
  faked with wash entries; the honest answer is a ledger pair (possibly
  an in-transit ledger) today, or the ADR-026-style relocations table the
  day a real deployment demands it. Stated here so the refusal is a
  decision, not an oversight.

Ships as migration `0009_stock_depth` (alembic head moves to
`0009_stock_depth`). The consumer is `ledgerforge/contrib/inventory.py`
(plugin API 0.12) with the kernel service `ledgerforge/inventory.py`, the
API `api/inventory.py`, the native `nativeapp/inventory.py` and the web
`InventoryScreen`. Exercised by `tests/test_inventory.py` (kernel:
masters, prices, BOMs), `tests/test_inventory_depth.py` (the pack:
grammar, guards, FEFO, manufacture, reports, reversal netting, book
isolation), `tests/test_native_inventory.py` and
`frontend/web/tests/inventoryScreen.spec.tsx`.
