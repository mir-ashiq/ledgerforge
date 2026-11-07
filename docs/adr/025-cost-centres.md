# ADR-025: Cost centres & cost categories — the allocation store joins the core schema

- **Status:** Accepted (2026-11-08)
- **Deciders:** mir-ashiq
- **Supersedes:** —
- **Extends:** ADR-001 (adjacency + closure), ADR-002 (append-only write path), ADR-006 (migration baseline), ADR-009 (book scoping), ADR-010 (the meta store — and its honest limit), ADR-018 (the partitioned shape)

## Context

TallyPrime's cost centres answer the question every growing business asks
mid-year: *where did this money go, and along which axis?* A rent line splits
across branches; a salary line across departments; the break-up report rolls
those splits up trees — centre summaries, ledger-wise details within a centre,
category rollups. ROADMAP.md §3 Phase 4 (v0.22.0, issue #10) commits to exactly
that, and the comparison table carries it as a **gap**: TallyPrime ✔,
LedgerForge ✖.

Three facts shaped the design.

**First — and this is the structural decision — allocations are not plugin
documents.** Every business pack since inventory (ADR-011) has ridden
`vouchers.meta` with zero core schema change, and that pattern was evaluated
here and *rejected*: allocations must aggregate with the same rigour as
entries. Exact sums over date windows, closure-table rollups at every tree
level, book scoping enforced by the schema, append-only history with the
database re-proving the law at commit — `meta` is a JSONB document store whose
integrity discipline is "the plugin validates itself"; a financial aggregate
that the trial balance's own machinery will read deserves the machinery the
trial balance has. So Phase 4 is **the first core-schema change since 0006**
(ADR-018), landing as migration `0007_cost_centres`.

**Second — the conservation law has the same shape as Law 1.** An entry's
allocations, if any, must sum to the entry's amount *exactly*: a line is fully
allocated or left unallocated, never partly. This is the ADR-002/ADR-006
discipline verbatim — enforced in the engine (a clean 422 naming the line) and
re-enforced by a DEFERRABLE INITIALLY DEFERRED constraint trigger that fires at
commit, precisely like `trg_entries_balanced` proves double-entry. Dr/Cr is
preserved from the entry (an allocation never invents a side), the entry's
`voucher_date` and `book_id` are denormalized onto the allocation row so
reports never join the write path (the `voucher_entries` precedent), and the
table is append-only: correcting a split means reversing the voucher.

**Third — the partitioned shape constrains the schema.** ADR-018's
`ops.partition_entries_by_year` swaps `voucher_entries` for a partitioned table
whose primary key is `(id, voucher_date)`; no unique index on `id` alone can
exist there, so the allocation→entry FK cannot either. The FK therefore exists
only on the plain shape: created conditionally in 0007 (a database that was
already partitioned when it upgrades simply never gets it), dropped across the
partition swap, re-added by `ops.unpartition_entries` when the plain shape
returns. On the partitioned shape the insert-time trigger `trg_cost_allocations_check`
keeps the integrity — same entry, same book, same D/C, same date — which is
every guarantee the FK gave, checked at the only write path an append-only
table has.

## Decision

### 1. Two trees, the ADR-001 machinery, twice

- **`cost_categories`** — the classification axes ("Department", "Location",
  "Project"): a `parent_id` adjacency with one seeded root ("Primary") per book
  and a trigger-maintained `cost_category_closure`. TallyPrime's categories are
  usually flat; the tree costs nothing and makes "primary/secondary" (the
  roadmap's word) structural.
- **`cost_centres`** — the allocation tree, the same adjacency + closure pair.
  Every real centre answers to exactly one category — TallyPrime's rule, made
  a CHECK constraint (`chk_cost_centre_root_category`): the root is the
  container (no category of its own, receives no allocations — a guard
  mirrored in the engine and the API), every leaf is classified.
- Both trees' triggers are the group triggers of ADR-001 with the tables
  renamed: insert inherits the ancestor chain, re-parenting is cycle- and
  root-guarded, a move rewrites the subtree in two set-based statements.
  Duplicate names per book, cross-book parents and cross-book categories are
  schema-impossible (the composite-FK pattern of `groups`).

### 2. `cost_allocations` — the first core financial table that is not a voucher

One row per (entry, centre, amount): `dc` preserved from the entry, `amount`
NUMERIC(26,8) exact, `voucher_date`/`book_id` denormalized, append-only
(`trg_forbid_mutation`, INSERT-only privilege for the app role). Two triggers:

- `trg_cost_allocations_check` (BEFORE INSERT, immediate) — the entry exists,
  same book, same D/C, same date, the centre exists in the same book, never
  the root. This is the integrity provider on *both* voucher_entries shapes.
- `trg_cost_allocations_exact` (CONSTRAINT TRIGGER, DEFERRABLE INITIALLY
  DEFERRED) — at commit, Σallocation = entry amount exactly, or the whole
  posting transaction dies: nothing partial ever survives, the Law-2
  discipline.

### 3. The wire and the plugin seam (plugin API 0.10)

`VoucherLineIn.cost_allocations: [{centre_id | centre_name, amount}]` — a
first-class line field, not `meta`: the engine validates after every plugin
hook (a transformer that splits a line without splitting its allocations gets
a clean 422), resolves centres inside the book (another book's centre id is a
404), and inserts the rows in the posting transaction. `DraftLine` carries
`cost_allocations` (with `DraftCostAllocation`) so hooks see and preserve
them; the API floor semantics of ADR-011 keep every older pack loading.
`VoucherOut` returns each line's allocations with centre names resolved.
Reversals mirror the splits — same centres, same amounts, the flipped D/C
riding the flipped line — so every centre's net walks back to exactly zero.

### 4. The break-up reports — the closure machinery, twice again

Three `rpt` functions, SQL STABLE, exact NUMERIC arithmetic, FYC sweeps
excluded (they transfer results, they are not operating facts — the
`profit_and_loss` precedent of ADR-008):

- **`rpt.cost_centre_summary(book, from, to)`** — every centre's subtree
  rollup through `cost_centre_closure` (a parent carries its subtree), plus
  the **Unallocated** row: the mass of entries nobody allocated, surfaced so
  that *allocated + unallocated is always the window's whole entry mass* —
  the honest conservation story, assertable against an independent fold.
- **`rpt.cost_centre_breakup(book, centre, from, to)`** — one centre's own
  ledger-wise detail (not its subtree's — sub-centres have their own
  break-ups), with the total row the shaper proves against the ledger rows
  (the trial-balance discipline: the engine's own output is never returned
  unproven).
- **`rpt.cost_category_summary(book, from, to)`** — allocations through the
  centre→category edge and the category closure: a primary category carries
  its secondaries' centres.

Served at `/api/v1/reports/cost-centres`, `/cost-centres/{id}/breakup`,
`/cost-categories` — JSON, the core reports' pattern.

### 5. Both surfaces, at entry time

- **Web**: `Alt+K` opens the split dialog — every complete line with its
  allocated-so-far and its remainder (always visible, the roadmap's law), a
  per-line split editor with exact BigInt arithmetic (Law 3), and inline
  centre creation (the Alt+C philosophy — no detour out of the entry flow).
  A partial split cannot be armed; "Leave unallocated" is the explicit other
  honest state. Armed splits ride their lines; an edited line drops its stale
  arming (it stays unallocated rather than posting a lie).
- **Native**: `Ctrl+K` on the voucher form, the same dialog and the same
  match-at-post discipline; the Masters page gains Cost Centres and Cost
  Categories tabs (create dialogs included); the Reports page gains the
  break-up trio, tree-nested, CSV-exportable.

## Consequences

- **Migration discipline holds**: 0007's upgrade blocks are generated from
  `db/schema.sql` + `db/functions.sql` verbatim; the drift guard
  (`test_migrations.py`) proves `alembic upgrade head` ≡ bootstrap, and the
  stepwise test proves the revision independently reversible. 0007 also
  carries the shape-aware replacements of 0006's two swap functions —
  existing databases get them on upgrade; both paths land on identical text.
- **The scale-out story survives**: partitioning drops the entry FK (a
  no-op-guarded statement), the per-row trigger keeps integrity on the
  partitioned shape, unpartitioning re-adds the FK with existing rows
  validated. Tested by the scale-out suite on both shapes.
- **What this ADR does not do**: no per-ledger "cost centres applicable"
  toggle (TallyPrime's optional per-ledger opt-out — the roadmap keeps
  allocation opt-in per line, which subsumes it); no ratio-based category
  allocation methods (TallyPrime's "On Nett Transaction / Ratio" — an input
  convenience over the same amount allocations, for a later UI pass); no
  budgeting. All additive, none blocking.
- **The pattern is now three-deep**: Law 1 proves vouchers, ADR-023's
  conservation law proves bill-wise refs, and this law proves cost
  allocations — each a deferrable database verdict the engine mirrors with a
  clean 422. The kernel remains the thing that proves itself.
