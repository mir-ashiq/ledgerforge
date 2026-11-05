# ADR-001: Account hierarchy storage — adjacency list + closure table

- **Status:** Accepted (2026-11-06)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

TallyPrime's chart of accounts is an arbitrarily deep tree of groups with ledgers attached at any node. Real-time Trial Balance, P&L and Balance Sheet need every group's *subtree* rollup on every request. Candidates: adjacency list, materialized path (incl. `ltree`), closure table, nested sets. Full comparison: `docs/analysis.md` §3.

## Decision

Store the tree **twice, by design**:

- `groups.parent_id` — the authoritative adjacency encoding, protected by a foreign key and the only column structural edits touch.
- `group_closure (ancestor_id, descendant_id, depth)` — a **trigger-maintained read model** used by all reporting queries.

Three triggers maintain the closure on insert / re-parent / prevent cycles. Reports read only the closure.

## Consequences

- **(+)** Whole-tree rollups are one equi-join + `GROUP BY`; measured at performance parity with the best recursive-CTE rollup on a 429-group / 120k-entry synthetic book (48 vs 47 ms; `scripts/bench_hierarchy.py`) — with dramatically simpler, review-safe SQL, and drill-down/ancestor queries as indexed point lookups instead of per-request recursion.
- **(+)** O(1) re-parenting of the authoritative tree; closure rewrite is two set-based statements on a rare operation.
- **(+)** Closure cannot drift — it is derived, not denormalized-by-hand.
- **(−)** Group inserts write `depth+1` closure rows; subtree moves rewrite a closure slice. Acceptable: masters change at human speed.
- **(−)** ~n×avg-depth rows of storage. At chart-of-accounts scale this is thousands of rows.

## Alternatives rejected

- **Pure adjacency:** every report recurses; the whole-tree rollup's upward-propagation SQL is subtle and easy to get wrong in contributor code (performance measured at parity — the risk is correctness, not speed).
- **Materialized path / `ltree`:** no FK-verifiable integrity; moves rewrite subtree paths; prefix joins beat neither recursion-free joins nor portability goals.
- **Nested sets:** insert renumbers half the tree; opaque; no integrity.
