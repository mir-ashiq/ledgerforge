# ADR-018: Report scale-out — the balance cache and yearly entry partitions

- **Status:** Accepted (2026-11-07)
- **Deciders:** mir-ashiq
- **Relates to:** ADR-002 (append-only entries), ADR-006 (migration discipline),
  ADR-008 (period locking — this ADR leans on its guarantees), ADR-009 (multi-book)

## Context

`docs/analysis.md` §4.3 recorded the scaling path as "known and boring" and
deferred it: *partition `voucher_entries` by date range, add BRIN indexes, and
(only if needed) maintain an eventually-consistent balance cache outside the
write path — the source of truth remains append-only — only when books pass
millions of entries.* The roadmap kept it at **Watch**.

Three properties of the shipped system make the deferral safe but also make
the implementation delicate:

1. **The write path is sacred.** Nothing in this ADR may add a single lock,
   row, or statement to posting (Law 1/2 and the gapless counter stay
   untouched).
2. **Backdating is real.** Any cache keyed by date is wrong the moment a
   backdated voucher lands inside the "cached" range — unless the range is
   *provably* unpostable.
3. **The default shape must not regress.** Books that never hit a million
   entries must keep today's plans byte-for-byte; scale-out is opt-in at
   read/ops time, not a new default.

## Decision

Three deliverables, one migration (`0006_report_scale_out`), zero default-path
change.

### 1. `rpt.balance_cache` — a cache that can only be slow, never wrong

One row per (book, ledger): `(horizon, dr, cr)` meaning *"the sum of this
ledger's entries with `voucher_date < horizon` is exactly `dr`/`cr`, and no
entry dated below `horizon` can ever be posted again."* The second half of
that sentence is where the correctness lives, and it is provided by ADR-008's
posting gate: once a book defines periods, **only dates inside an OPEN period
post** — closed periods are final (terminal state), and dates in gaps or
locked periods are refused. Therefore:

- **horizon = `min(starts_on)` over the book's OPEN periods** (a book whose
  periods are all locked/closed is fully frozen: horizon = `9999-12-31`;
  a book with no periods has no provable horizon at all — the cache stays
  empty and the fast path falls back to the direct scan).
- The zone below the horizon is closed-period dates (frozen forever) plus
  gap dates (unpostable — the gate refuses uncovered dates) and pre-period
  dates (also uncovered, also refused).

The one way frozen territory can become writable again is **administrative**,
not transactional: an operator can create a new OPEN period, or reopen a
locked one, covering dates below the horizon. A row trigger on `periods`
(`trg_periods_cache`, fires on insert-as-open and reopen-to-open) deletes
every cache row of that book whose horizon exceeds the new period's
`starts_on`, **in the same transaction** as the period change — there is no
window in which a posting can land below a surviving row's horizon.

`rpt.refresh_balance_cache(book?)` rebuilds the cache (SECURITY DEFINER,
advisory-locked per book, idempotent):

- existing rows are **extended incrementally** — the delta zone
  `[old horizon, new horizon)` was writable at the last refresh and is frozen
  now, so `old + delta` is exact;
- ledgers without a row get a full sum below the horizon;
- rows whose horizon exceeds the freshly computed one (defensive; the
  trigger should already have removed them) are deleted and rebuilt.

FYC close vouchers are always dated the **first day of the next period**
(ADR-008), which must be open — so at posting time they live at or above
the horizon. But when that period closes in turn, the sweep enters the
cached zone: the cache therefore carries **two sum variants** per ledger —
the inclusive one (Trial Balance / Balance Sheet) and the FYC-exclusive one
(`dr_no_fyc`/`cr_no_fyc`, for P&L). Both are maintained by the same
incremental refresh pass.

### 2. `p_fast` on the read path — opt-in, always exact

`rpt.trial_balance`, `rpt.group_rollups`, `rpt.balance_sheet` and
`rpt.profit_and_loss` gain `p_fast BOOLEAN DEFAULT false` and source their
per-ledger aggregation from one helper, `rpt._ledger_sums_upto(book, upto,
exclude_fyc, fast)`:

- **slow branch (default)** — the current aggregation, unchanged;
- **fast branch** — per ledger with a usable row (`horizon <= upto + 1`):
  the cached sums plus a tail scan over `[horizon, upto]` (a per-ledger index
  range scan on the covering index; a stale cache is a full scan, never a
  wrong number); per ledger without a row: the direct scan.

P&L is a date range, and the cache is lifetime-shaped, so the fast path
computes it as `S(to) − S(from − 1)` — both point-in-time aggregates the
cache serves, and the FYC exclusion applies identically to both terms, which
is arithmetically the same as excluding it from the range.

The API exposes this as `?fast=true` on the three report endpoints (reader
role); the shapers keep proving their invariants (difference exactly zero,
balance sheet balances) on every call, fast or slow. The report functions
also gained deterministic `ORDER BY`s so the two paths return byte-identical
JSON (the fast path's UNION/LATERAL shape emits rows in a different physical
order than the slow path's hash aggregate — a strictly-better API contract,
normalized on both paths).

### 3. `ops.partition_entries_by_year()` — declarative yearly partitions, operator-initiated

Books that pass the million-entry mark convert in one transactional call
(maintenance window; the copy is `ORDER BY voucher_date, id` so every
partition lands BRIN-friendly):

- shadow table `PARTITION BY RANGE (voucher_date)` with the same columns,
  checks and foreign keys; the primary key becomes `(id, voucher_date)` and
  the line uniqueness `(voucher_id, line_no, voucher_date)` — equivalent
  because a voucher's entries all share its date (proven by
  `trg_entries_check_date`), which also means **a voucher's entries never
  span partitions**, so the deferrable Law-1 constraint trigger
  (`trg_entries_balanced`) still sees the whole voucher at commit — verified
  on PostgreSQL 16/17;
- yearly partitions `voucher_entries_YYYY` from the oldest entry year to
  `current year + headroom` (default 2); the covering index moves to the
  parent (cascades to partitions), a BRIN on `voucher_date` is added, and
  the old plain table is dropped — PostgreSQL's transactional DDL makes the
  swap atomic to every other session;
- a **BEFORE INSERT trigger on `vouchers`** (not on `voucher_entries` —
  row triggers on a partitioned table fire *after* routing, and DDL fired
  from an in-flight statement on the same table dead-locks on plan
  invalidation) creates the year partition on demand via a SECURITY
  DEFINER function, so postings keep working across year boundaries with
  zero engine changes and zero app-role privileges (routed inserts check
  the parent's ACL only; the partition is owned by the table owner).

`ops.unpartition_entries()` is the mirror image (rebuild the plain shape,
drop the parent — partitions go with it — restore the original index and
trigger set). **Balance-cache rows survive both directions**: they sum by
date, not by table shape.

`ops.scale_out_status()` reports the shape, partition count, entry estimate
(catalog statistics, not a `count(*)`) and cache state per book.

### What deliberately did *not* happen

- **No default-shape change.** Migration 0006 adds a table, a trigger and
  functions; the entries table, its indexes and the default report plans are
  untouched. The structural drift guard (`tests/test_migrations.py`) proves
  `alembic upgrade head` ≡ `db/schema.sql + db/functions.sql` as always.
- **No write-path change.** Posting does not consult, invalidate or lock the
  cache; the only cache maintenance is the `periods` trigger (period DDL is
  rare and administrative) and the operator's refresh.
- **No new kernel knowledge.** The horizon rule is ADR-008's gate restated;
  partitions are infrastructure, not accounting semantics.

## Consequences

- Fast reads are exactly as correct as slow ones — equality with the slow
  path is asserted by the test suite across period topologies (open only,
  closed, locked, gaps, all-closed, no periods, reopen, insert-below-
  horizon), and the invariants (TB difference 0, BS balances) hold on both
  paths.
- The cache rewards period discipline: books that close their history can
  cache it; books that never adopt periods simply never cache (fast falls
  back, never lies).
- Partition conversion is a one-transaction maintenance op with a tested
  revert; the engine, API and plugins are unaware of it.
- Downgrade restores the exact pre-ADR function texts (ADR-006 discipline);
  the cache table and ops schema drop cleanly; entries were never touched.
