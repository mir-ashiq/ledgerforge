# ADR-011: Inventory as a plugin pack — document-store reads and advisory locks

- **Status:** Accepted (2026-11-06)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

The roadmap (§9) lists inventory as a plugin, not core: "built on the same hook taxonomy + document store". Invoicing (ADR-010) proved the write half of that promise — a document persists in `vouchers.meta` and the voucher *is* the invoice. Inventory needs the read half: a stock position is a **fold over every stock document ever posted**, which means a plugin must be able to query prior documents from inside the posting transaction. It also needs serialization: two concurrent goods-out movements of the same item must not both pass an insufficient-stock check, exactly as two concurrent postings must not both take counter number 41.

Neither capability existed. `Resolver` could only look up masters. And without locks, any quantity/valuation state a plugin derived could be raced between read and write — the class of bug the kernel solves with `SELECT … FOR UPDATE` on the numbering counter (ADR-002).

## Decision

### 1. Plugin API 0.3 — two meta-blind primitives on `Resolver`

- **`prior_documents(key)`** — every posted voucher of the book whose `meta` carries `key`, oldest first, as `PriorDocument(voucher_id, voucher_no, voucher_date, reversal_of, meta)`; **plus every voucher reversing one of those**, so a fold can net an undone document against its correction. The kernel returns payloads exactly as stored and interprets nothing — this is storage access, not schema indirection (the ERPNext lesson, §8, still holds). A reversal of a reversal cannot exist (engine rule), so the netting is a single level.
- **`lock(name)`** — `pg_advisory_xact_lock(hashtextextended('{book_id}:{name}'))`: transaction-scoped, auto-released at commit/rollback, namespaced to the book. The same discipline as the engine's counter lock, opened to plugins. Deadlock discipline is the caller's, documented and tested: a voucher locks **all** its names in **one sorted pass** — ordered acquisition, the PostgreSQL `LOCK`-ordering advice (§10.16) applied to advisory locks.

### 2. `api_compat` is a floor, not a pin

The old compatibility check demanded an exact minor match, which would have made every additive API release eject the entire plugin ecosystem. Semantics now: a plugin declaring an **older** minor loads on a newer build (additions are backwards compatible); a plugin declaring a **newer** minor refuses loudly. `example-gst` and `invoicing` still declare `0.2` and keep loading — a live proof.

### 3. The inventory pack (`ledgerforge/contrib/inventory.py`)

Four document modes on `meta.stock`, zero schema change — the first roadmap feature to land with **no migration at all**:

| Mode | Posts | Stock effect |
|---|---|---|
| `in` | Cr supplier (Sundry Creditors) · Dr inventory per item (Stock-in-Hand) — the perpetual TallyPrime purchase | qty+, value+ (at cost) |
| `out` | Dr COGS · Cr inventory at average cost; **with party**: + Dr customer · Cr revenue per item — a full perpetual sale with annotated gross margin | qty−, value− (at average cost) |
| `transfer` | Dr destination · Cr source inventory ledger (godowns), same amount | none — value-preserving by construction |
| `recount` | Dr/Cr inventory vs *Stock Adjustment* per direction of the count delta | qty→counted, value adjusts |

**Valuation is a moving weighted average, and the arithmetic is closed under rounding**: state is `(qty, value)` where `value` only ever moves by posted 2-dp line amounts; the unit cost is `value / qty` at 50-digit precision and each issue costs `money(q × unit)`. Issuing the *last* unit therefore costs exactly the remaining value — a position empties to `0.00` with no residue to sweep. The transformer and every later re-derivation run the **same fold** (`_apply_line`) over the same document sequence, so a position is a deterministic replay, never stored state — the reports' philosophy (§4) applied to quantities.

**Reversals net to zero**: a reversal carries empty meta (ADR-010), so `prior_documents` delivers it alongside its original; the fold skips both — undoing a goods-out restores the stock, undoing a goods-in removes it, mirroring what the mirrored entries did to the ledgers.

**Guards** (all 422): insufficient stock (`policy:allow_negative_stock` overrides partial overdrafts; an *empty* position never issues — no cost basis), transfers never go negative, recounts that change nothing, zero-valued lines, document-mode conflicts (`stock` vs `gst`/`invoice`/`receipt`, vetoed by **both** packs), FX lines, future dates (policy-overridable), and stock items whose auto-created ledger name would silently reuse an unrelated existing ledger.

## Consequences

- **(+)** The roadmap's inventory promise is kept *entirely outside core*: no tables, no migration, no `rpt.*` change — the pack is one module + tests, and uninstalling it removes inventory without touching a single row of accounting history.
- **(+)** The two new Resolver primitives are generic (any document store, any named resource), immediately useful to payroll, a plugin-maintained invoice series, or a warehouse bridge — and available to third-party packs under the versioned API.
- **(+)** The floor semantics un-break the ecosystem contract: publishing 0.3 did not orphan 0.2 plugins.
- **(−)** The fold is O(all stock documents of the book) per posting — fine at TallyPrime-shop scale (thousands of movements), not at warehouse scale. When a book needs it, the ADR-010 escape hatch applies: promote positions to real schema through a migration and keep the document as provenance, rather than growing `rpt.*` JSONB queries.
- **(−)** Advisory locks serialize same-item movements across the whole book — correct but coarse: a book posting thousands of movements per second on one item would queue. The escape hatch is the same promotion to schema.
- **(−)** v1 scope, documented: no FIFO/batch/serial tracking (weighted average only); godowns are value-level ledgers, not quantity-level locations (issuing from a godown ledger the stock does not sit in drives that *ledger* negative while the item position stays correct); no manufacturing/repack consuming items into others; tax stays with the invoicing pack (sale side) or a manual voucher (purchase side). A production pack that needs these promotes to schema per ADR-010 — or ships as a richer plugin using the same 0.3 primitives.

Shipped with no migration: `alembic` head remains `0005_voucher_meta`. The consumer is `ledgerforge/contrib/inventory.py`, registered as an entry point and loaded at boot; exercised by `tests/test_inventory_plugin.py` (shapes, average discipline, reversal netting, book isolation, idempotent replay, and a 3-thread concurrent-issue race that the advisory lock turns into exactly two successes).
