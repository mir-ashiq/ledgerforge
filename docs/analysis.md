# LedgerForge — Architecture Research & Deep Analysis (Phase 1)

| | |
|---|---|
| **Project** | LedgerForge — a community-driven, keyboard-first, double-entry business management engine |
| **Author** | mir-ashiq |
| **Version** | 0.1.0 (Phase 1, accepted) |
| **Date** | 2026-11-06 |
| **Status** | Implementation of the decisions recorded here is complete in this repository (Phase 2) |

---

## Contents

1. [Executive Summary](#1-executive-summary)
2. [Scope, Method & Domain Context](#2-scope-method--domain-context)
3. [Research Area 1 — Modeling the Account Hierarchy](#3-research-area-1--modeling-the-account-hierarchy)
4. [Research Area 2 — High-Concurrency & the Write Path](#4-research-area-2--high-concurrency--the-write-path)
5. [Research Area 3 — Open-Source Modularity & Plugin Architecture](#5-research-area-3--open-source-modularity--plugin-architecture)
6. [Research Area 4 — Keyboard-First UX Architecture](#6-research-area-4--keyboard-first-ux-architecture)
7. [The Three Financial Laws → Engineering Controls](#7-the-three-financial-laws--engineering-controls)
8. [Implementation Overview (Phase 2)](#8-implementation-overview-phase-2)
9. [Risks & Roadmap](#9-risks--roadmap)
10. [References](#10-references)

---

## 1. Executive Summary

LedgerForge is an open-source business management system whose core is a **high-precision, keyboard-driven, double-entry accounting engine** inspired by TallyPrime. Before any code was written, this document researched and settled the four hardest architectural questions the system faces, under three non-negotiable financial laws:

- **Law 1 — Double-Entry:** total debits must equal total credits for every transaction, to the last fraction of a cent, or the transaction aborts.
- **Law 2 — Append-Only Immutability:** financial history is never updated or deleted; corrections happen only through explicit reversal/rectification vouchers.
- **Law 3 — Decimal Precision:** floating-point arithmetic is banned; every currency value is an arbitrary-precision decimal end-to-end.

### Headline decisions

| # | Question | Decision | Runner-up considered | Why |
|---|----------|----------|----------------------|-----|
| D1 | How to store Tally's deep-nested Groups → Ledgers hierarchy | **Adjacency list as source of truth + trigger-maintained closure table as the read model** | PostgreSQL `ltree` (materialized path) | Real-time statement rollups reduce to one indexed equi-join over the closure table — no recursion, no prefix string matching — while `parent_id` keeps referential integrity and O(1) subtree moves. Measured at performance parity with the best recursive-CTE rollup (§3.8), and dramatically simpler, safer SQL (§3.7). |
| D2 | How to process hundreds of concurrent voucher postings | **Append-only entry ledger (no mutable balance columns) + a single locked counter row for gapless voucher numbering** | Row-locked mutable balance columns | Balance updates are *derived*, so concurrent postings to a hot ledger (Cash) cannot block or lose updates; the only serialized point is a microsecond-scale `SELECT … FOR UPDATE` on the numbering counter. Verified empirically: 8 threads × 40 vouchers, zero deadlocks, zero gaps, exact balance (§4.8). |
| D3 | How to let a worldwide community extend the system without touching core | **Hexagonal core + versioned in-process plugin API (entry-points + manifest) with a domain event bus and hook taxonomy; sandboxed UI extension slots** | Out-of-process microservice plugins (reserved for integrations) | Tax computation must run *inside* the posting transaction to preserve Law 1, so localization packs must be in-process; anything latency-tolerant (dashboards, e-invoicing, gateways) stays out-of-process by contract. |
| D4 | How to replicate Tally's mouse-free speed on the web | **React + TypeScript host with a framework-agnostic command registry / keybinding resolver / focus-scope stack, modeled on the VS Code interaction model and TallyPrime's key map** | Svelte/Solid host | The plugin dashboard ecosystem (D3) overwhelmingly targets React; raw framework speed is not the bottleneck — *keystroke-to-echo latency* is, and that is solved by architecture (in-memory indexes, optimistic echo), not by swapping renderers. |
| D5 | License | **AGPL-3.0 for the core** | GPL-3.0 / LGPL-3.0 / Apache-2.0 | Strong copyleft plus the network clause protects the community against closed SaaS forks while the plugin API boundary keeps third-party extensions free to choose their own license (§5.7). |

### The three laws, at a glance

| Law | Application-layer control | Database-layer control | Proof |
|-----|--------------------------|------------------------|-------|
| Double-entry (Debits = Credits) | Exact `Decimal` summation before insert; abort on any mismatch | `DEFERRABLE INITIALLY DEFERRED` constraint trigger re-verifies `SUM(debit) = SUM(credit)` per voucher at commit, in the engine's own transaction | `test_double_entry.py` — off-by-a-fraction payloads rejected, DB rejects direct unbalanced SQL inserts |
| Append-only immutability | Engine exposes only INSERT paths; corrections = reversal vouchers linked via `reversal_of` | `BEFORE UPDATE OR DELETE` triggers raise exceptions on `vouchers` / `voucher_entries`; the application DB role is granted only `SELECT, INSERT` | `test_immutability.py` — `UPDATE`/`DELETE` raise; net effect of a reversal is exactly zero |
| Decimal precision | Python `Decimal` everywhere; JSON amounts **must** be strings — floats are rejected with HTTP 422 | `NUMERIC(26,8)` columns (exact, arbitrary precision); scale > 8 rejected at validation | `test_decimal_law.py` — `0.1 + 0.2 = 0.3` posts correctly as strings; float payloads rejected |

---

## 2. Scope, Method & Domain Context

### 2.1 What we are building (and, deliberately, not yet)

The Phase-2 deliverable in this repository is the **accounting kernel**: masters (groups, ledgers), voucher posting, reversal, opening balances, reporting (Trial Balance, Profit & Loss, Balance Sheet), the plugin runtime with a worked GST example, and the keyboard interaction framework for the frontend. Multi-currency and period locking landed in v0.4.0 (ADR-007/008), multi-book in v0.5.0 (ADR-009), invoicing in v0.6.0 as the first plugin pack on the voucher document store (ADR-010), inventory in v0.7.0 as the second pack — the first roadmap feature to land with zero schema change, riding the new document-store read and advisory-lock primitives of plugin API 0.3 (ADR-011) — and payroll in v0.8.0 as the third, folding fiscal-year YTD and all-time payable balances from the same document store (plugin API 0.4, ADR-012). Every remaining roadmap item is *buildable on top of* this kernel precisely because the plugin boundaries were designed first.

### 2.2 The TallyPrime reference model

TallyPrime organizes the chart of accounts as a tree of **Groups** with **Ledgers** attached at any node. TallyPrime ships **28 predefined groups, of which 15 are primary groups** — e.g. *Capital Account*, *Current Assets*, *Sundry Debtors* (assets side), *Current Liabilities*, *Duties & Taxes*, *Sundry Creditors* (liabilities side), *Sales Accounts*, *Purchase Accounts*, *Direct/Indirect Incomes/Expenses* (P&L) — and users can nest sub-groups to arbitrary depth. This is documented by Tally Solutions in their "Ledgers and Groups in TallyPrime" guide (see References).

Three properties of that model drive our database design:

1. **The tree is deep and user-defined.** Reporting must roll ledger balances up through *every* ancestor group, so a Trial Balance line for *Sundry Debtors → North Region → Chennai Branch* must aggregate correctly no matter how the user reshapes the tree later.
2. **Ledger names are unique book-wide** in Tally; the rapid-entry UX depends on type-ahead over a single flat, in-memory name index.
3. **The predefined groups carry accounting semantics**: their *nature* (asset / liability / income / expense) decides whether balances surface in the Balance Sheet or the P&L, and Tally's trading-account split (gross vs. net profit) is driven by group *behavior* flags (direct vs. indirect). LedgerForge models both explicitly (`groups.nature`, `groups.affects_gross`).

### 2.3 Evaluation method

Each research area was settled by the same procedure:

1. **Requirements first** — write the queries/operations the engine must support before admiring any technique.
2. **Candidate survey** — enumerate the established options (including at least one deliberately rejected one, to make the trade-off honest).
3. **Decision matrix** — score candidates against query cost, write cost, integrity, portability, and implementation complexity.
4. **Empirical validation** — benchmark the finalists on a synthetic book inside this repository (`scripts/bench_hierarchy.py`, `scripts/bench_posting.py`), so every performance claim in this document is reproducible by anyone.

The workload model used throughout: a mid-size business book with ~300 groups, ~3,400 ledgers and 120,000 voucher entries, posting peaks of a few hundred vouchers per minute, and report latency budgets under one second.

---

## 3. Research Area 1 — Modeling the Account Hierarchy

### 3.1 Requirements

- R1.1 Arbitrary-depth group nesting with ledgers attachable at any node.
- R1.2 **Rollup**: for every group, the exact debit/credit totals of its *entire subtree*, computed in real time for Trial Balance, P&L and Balance Sheet.
- R1.3 **Ancestor chains**: render report trees (drill-down from Primary → leaf) and validate placement.
- R1.4 **Safe moves**: re-parenting a group must atomically move the whole subtree and must be impossible to create cycles.
- R1.5 **Integrity**: referential integrity enforced by the database, not by application discipline.
- R1.6 **Portability**: prefer pure SQL over engine-specific extensions for the core (the plugin API is where engine-specific richness is allowed).
- R1.7 Masters are low-churn: group DDL happens hundreds of times a year, not millions — write amplification on moves is acceptable if reads get dramatically faster.

### 3.2 Candidate A — Adjacency List

Every row stores `parent_id`:

```sql
CREATE TABLE groups (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    parent_id  BIGINT REFERENCES groups(id) ON DELETE RESTRICT,
    nature     TEXT NOT NULL CHECK (nature IN ('asset','liability','equity','income','expense')),
    affects_gross BOOLEAN NOT NULL DEFAULT FALSE
);
```

**Strengths.** It is the canonical relational encoding: one FK gives R1.5; re-parenting is a single-row `UPDATE` (R1.4, O(1)); storage is minimal; every SQL engine supports it. PostgreSQL ≥ 14 even adds `WITH RECURSIVE … SEARCH/CYCLE` to tame recursive queries.

**Weaknesses.** Everything hierarchical becomes a recursive CTE:

```sql
WITH RECURSIVE subtree AS (
    SELECT id FROM groups WHERE id = :root
    UNION ALL
    SELECT g.id FROM groups g JOIN subtree s ON g.parent_id = s.id
)
SELECT sum(...) FROM ... WHERE group_id IN (SELECT id FROM subtree);
```

Recursion is per-level executor machinery: each level is a new subplan execution, each row visited costs interpreter overhead, and the planner cannot use a single index scan for the whole traversal. For one subtree this is fine (a chart of accounts is thousands of rows, not billions). The *hard* adjacency problem is R1.2 — computing **every group's** subtree rollup in one pass. It is possible (seed the recursion with per-group ledger totals, then propagate child sums upward level by level), but the SQL is subtle, the executor still pays per-level recursion, and every report pays it again on every request:

```sql
WITH RECURSIVE rollup AS (
    SELECT g.id AS group_id, COALESCE(sum(e.amount), 0) AS total
    FROM groups g LEFT JOIN ledgers l ON l.group_id = g.id
         LEFT JOIN voucher_entries e ON e.ledger_id = l.id
    GROUP BY g.id
    UNION ALL
    SELECT p.id, r.total FROM rollup r JOIN groups p ON p.id = (SELECT parent_id FROM groups WHERE id = r.group_id)
)
SELECT group_id, sum(total) FROM rollup GROUP BY group_id;
```

(Note the duplicated rows per ancestor level that the final `GROUP BY` must collapse — recursion *fan-out* does the work the closure table stores explicitly.)

### 3.3 Candidate B — Materialized Path (including PostgreSQL `ltree`)

Each node stores its full path, either as text (`'/1/17/42/'`) or with PostgreSQL's `ltree` type (`root.assets.current.debtors`).

**Strengths.** A subtree is a prefix query — `WHERE path <@ 'root.assets.current'` with a GiST index, no recursion; ancestors are the path's own prefixes (parse or `subpath()`); depth is `nlevel(path)`; inserts are O(1) + path build.

**Weaknesses.**

- **Integrity is unverifiable (R1.5).** Nothing in the database proves `/1/17/` exists or that 17's parent is 1. The denormalized path *will* drift; drift in a financial rollup is a silent misstatement, the worst failure class an accounting system can have.
- **Subtree moves rewrite every descendant** (O(subtree) `UPDATE`s) and, under concurrency, a long move transaction can make two readers see half-moved trees without careful locking.
- **The killer query is awkward.** "Sum of every group's subtree" becomes a self-join where each ledger's group path must be matched against every candidate ancestor's path (`l.path <@ g.path`), a join on an expression over GiST — workable, but strictly more machinery than an integer equi-join, and the planner cannot attack it with the same index-only scans.
- `ltree` specifically is PostgreSQL-only (fails R1.6) and its label syntax leaks into master data.

Text materialized paths without `ltree` trade the GiST index for `LIKE 'prefix/%'` with `text_pattern_ops` — portable but slower still, and they inherit every integrity problem above.

### 3.4 Candidate C — Closure Table

A separate table stores every ancestor→descendant pair with its depth (the pattern described classically by Joe Celko and validated in the research literature on relational hierarchies — see References):

```sql
CREATE TABLE group_closure (
    ancestor_id   BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    descendant_id BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    depth         INT    NOT NULL CHECK (depth >= 0),
    PRIMARY KEY (ancestor_id, descendant_id)
);
```

**Strengths.** Every hierarchical question becomes one flat, indexed join:

```sql
-- R1.2 + R1.3, the statement-engine query: every group's subtree rollup, one pass
SELECT c.ancestor_id            AS group_id,
       sum(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END) AS dr,
       sum(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END) AS cr
FROM voucher_entries e
JOIN ledgers l       ON l.id = e.ledger_id
JOIN group_closure c ON c.descendant_id = l.group_id
GROUP BY c.ancestor_id;
```

No recursion, no functions, no prefix matching — the planner turns it into hash joins over integer keys, and the primary key gives index-only scans for pure tree questions (`WHERE ancestor_id = :x` = whole subtree, `WHERE descendant_id = :x` = full ancestor chain with `depth` for free). Depth also solves "show children only" (`depth = 1`) without `NOT EXISTS` gymnastics.

**Weaknesses.** Storage is O(n × average depth) — for a 340-group chart at depth ~6 that is ~2,000 rows, i.e. nothing; at pathological scale (millions of nodes) it would matter. Inserts write `depth+1` rows; a subtree move rewrites the closure slice for that subtree (two set-based statements, §3.7). Both are master-data operations that happen at human speed — R1.7 says this trade is correct.

### 3.5 Aside — Nested Sets (rejected early)

Nested sets (`lft`/`rgt` intervals) answer subtree queries with range scans, but every insert renumbers up to half the tree, moves are worse, nothing about the encoding is FK-verifiable, and the technique is opaque to the average contributor. It optimizes for read-mostly trees with near-zero structural change — the opposite of a user-editable chart of accounts. Rejected.

### 3.6 Decision matrix

Grades: ★★★ excellent · ★★ good · ★ acceptable · ✗ poor.

| Criterion (requirement) | Adjacency list | Materialized path / ltree | Closure table |
|---|---|---|---|
| Subtree rollup for **one** group (R1.2) | ★★ recursive CTE, one query | ★★ prefix/GiST scan | ★★★ single indexed join |
| Subtree rollup for **all** groups (R1.2, the TB/P&L/BS case) | ★ subtle upward-propagation recursion, fan-out collapse | ★ expression join over paths | ★★★ one join + `GROUP BY` |
| Ancestor chain (R1.3) | ★★ recursion | ★★ path parse / prefixes | ★★★ `WHERE descendant_id = :x` |
| Insert node | ★★★ one row | ★★★ one row | ★★ `depth+1` rows |
| Move subtree (R1.4) | ★★★ one `UPDATE` | ★ rewrite subtree paths | ★★ two set-based closure rewrites |
| Referential integrity (R1.5) | ★★★ FK on `parent_id` | ✗ none possible | ★★★ FKs on both columns (+ adjacency FK kept) |
| Cycle prevention (R1.4) | ★★ trigger on re-parent | ✗ manual | ★★★ `BEFORE UPDATE` closure check |
| Portability (R1.6) | ★★★ | ★★ (text) / ★ (`ltree`) | ★★★ |
| Storage at chart-of-accounts scale | ★★★ | ★★★ | ★★★ (thousands of rows) |
| Contributor familiarity | ★★★ | ★ | ★★ |

*Note on the grades: they score SQL complexity, integrity and robustness — not raw speed. The measured performance of the two rollup shapes is equal at chart-of-accounts scale (§3.8).*

### 3.7 Decision D1 — Hybrid: adjacency as truth, closure as the read model

LedgerForge keeps **both**: `groups.parent_id` remains the authoritative, FK-protected tree (so re-parenting is one `UPDATE`, and no report can ever contradict the adjacency facts), and `group_closure` is a **trigger-maintained read model** — three small triggers keep it exactly in sync:

- `AFTER INSERT` on `groups`: insert `(self, self, 0)` plus the new node's row for every ancestor of `parent_id`;
- `BEFORE UPDATE OF parent_id`: raise if the new parent is inside the moved subtree (cycle) or the node is the root;
- `AFTER UPDATE OF parent_id`: delete the subtree's links to the *old* ancestor chain, insert links to the *new* chain — both as set-based `DELETE`/`INSERT … SELECT` over the closure itself.

Because the closure is derived and trigger-synced, it cannot drift (the materialized-path failure mode is structurally impossible), and because reports read *only* the closure, they never recurse. Group moves are rare and their cost is bounded and predictable.

### 3.8 Empirical validation

Method: `scripts/bench_hierarchy.py` builds a synthetic book — 429 groups (Tally's 28 predefined groups + 310 synthetic, depth 7), 3,402 ledgers, 120,000 voucher entries — and compares the recursive-CTE rollup against the closure rollup with `EXPLAIN ANALYZE`, median of 5 warm runs.

Results (PostgreSQL 16.2, single node, warm cache):

| Query | Recursive CTE (adjacency) | Closure table | Ratio |
|---|---|---|---|
| Whole-tree group rollup (every group's subtree total) | 48 ms | 47 ms | 1.0× |
| Trial Balance (ledger totals + group rollups) | 125 ms | 124 ms | 1.0× |

**The honest headline: at chart-of-accounts scale the two models are at performance parity** — both an order of magnitude inside the one-second report budget. That result is itself informative: at this tree size the dominant cost is aggregating 120k entries, not walking the hierarchy, and the decision therefore rests on the qualities a stopwatch cannot measure:

1. **Correctness surface.** The recursive rollup is *subtle* SQL — an upward-propagation with fan-out rows that the final `GROUP BY` must collapse (§3.2). The closure rollup is a two-line join a reviewer can verify at a glance. In a community project where hundreds of contributors will touch reporting code, review-verifiability is worth more than milliseconds.
2. **Point queries without recursion.** Drill-down ("subtree of *Sundry Debtors*"), ancestor chains, and depth filters are indexed point lookups over the closure — no per-request recursion to write, test, or get wrong.
3. **Plan shape stability.** Closure cost is one hash join over `groups × avg-depth` rows regardless of tree shape; recursion pays per-level executor work that grows with depth and does not benefit from the same index-only scans.
4. **Portability** (R1.6): plain SQL, no engine-specific types.

The full benchmark is reproducible via `make bench` (see `scripts/bench_hierarchy.py`).

---

## 4. Research Area 2 — High-Concurrency & the Write Path

### 4.1 Workload model

Hundreds of vouchers arrive concurrently. Each voucher is 2–20 lines; each line touches one ledger; hot ledgers (Cash, a bank account, a busy receivable) appear on a large share of vouchers. Tally users type at full human speed and batch-sync at day end, so the engine must sustain burst posting without lock-ups, without numbering gaps, and without ever writing a partially-balanced voucher — while reports run against the same live data.

Two resources are contended: **voucher numbers** (a legal, gapless series per voucher type) and **account balances** (if the design stores them). We evaluate the two competing write-path models.

### 4.2 Model A — Row-locking with mutable balances

The classic OLTP design: `ledgers.balance` is a live column; posting runs inside one transaction:

```sql
BEGIN;
SELECT id FROM ledgers WHERE id = :cash FOR UPDATE;      -- lock the account
UPDATE ledgers SET balance = balance + :amount WHERE id = :cash;
INSERT INTO vouchers (...) VALUES (...);
INSERT INTO voucher_entries (...) VALUES (...);
COMMIT;
```

**Strengths.** O(1) balance reads (no aggregation); overdraft/limit checks are trivial; the mental model matches "updating an account".

**Weaknesses — and they are fatal here:**

1. **Hot-row serialization.** Every posting that touches Cash queues on the same physical row lock. `SELECT … FOR UPDATE` holds that lock until commit — the *entire* transaction, including network round-trips, runs while the hottest row in the database is pinned. PostgreSQL's own documentation is explicit that `FOR UPDATE` "prevents them from being locked, modified or deleted by other transactions until the current one commits" — the exact behavior we must not amplify on a hot account.
2. **Deadlock exposure.** A voucher debiting Cash and crediting HDFC Bank, posted concurrently with one that debits HDFC Bank and credits Cash, deadlocks unless every code path locks accounts in a globally consistent order. The database's advice — "make sure all transactions acquire locks on the same objects in the same order" — is a discipline tax every future plugin author would pay forever.
3. **Lost-update bugs** the moment any contributor forgets the lock (a plain `UPDATE balance = balance + x` under `READ COMMITTED` looks fine in dev and corrupts books in production).
4. **It contradicts Law 2.** A balance column is, by definition, mutable derived financial state. Every update is an opportunity for drift between the column and the entries — and the audit story becomes "trust the code", not "trust the ledger".

### 4.3 Model B — Append-only ledger with derived balances

LedgerForge's choice. `voucher_entries` is the *only* financial fact table; balances are always computed from it:

```sql
-- A ledger's balance as of a date: one indexed aggregation
SELECT sum(CASE WHEN e.dc = 'D' THEN e.amount ELSE -e.amount END)
FROM voucher_entries e
JOIN vouchers v ON v.id = e.voucher_id
WHERE e.ledger_id = :ledger AND v.voucher_date <= :as_of;
```

**Why it wins:**

1. **Concurrent inserts do not contend.** PostgreSQL MVCC gives every inserting transaction its own copy of modified pages; two vouchers posting to Cash simultaneously touch no common row. There is nothing to lock.
2. **No lost updates are possible** — there are no updates.
3. **The audit trail *is* the data** (Law 2 by construction), and time-travel ("balance as of any past date") is native.
4. **Read cost is bounded and index-friendly.** A covering index `(ledger_id, voucher_date) INCLUDE (dc, amount)` turns the balance query into an index-only scan; at 120k entries a full Trial Balance is ~50 ms (§3.8), and per-ledger balances are single-digit milliseconds. When books grow to millions of entries, the scaling path is known and boring: partition `voucher_entries` by date range, add BRIN indexes, and (only if needed) maintain an *eventually-consistent* balance cache outside the write path — the source of truth remains append-only.

**Cost accepted:** reports aggregate instead of reading a column. That is the right trade for an accounting engine, where reads happen at human reporting cadence and correctness is absolute.

### 4.4 Transaction isolation

| Level | What it gives | Cost | Used for |
|---|---|---|---|
| `READ COMMITTED` (default) | Statement-level snapshots; no dirty reads | — | **Posting path.** Insert-only writes + one locked counter row have no write-skew anomaly surface; no retries needed. |
| `REPEATABLE READ` | Transaction-level snapshot |serialization-failure retries if writes conflict | Multi-statement report *composition* (not needed in v1: each report is a single SQL function call, which is atomic under MVCC). |
| `SERIALIZABLE` (SSI) | Full serializability; database aborts dangerous overlaps | Retry loops everywhere; false positives under hot rows | Deliberately **not** used for posting: our invariants are enforced by constraints and locks, not by isolation, and SSI's abort/retry churn on hot counters would be pure overhead. If future features add read-modify-write patterns (e.g. budget checks), they should adopt SSI *locally* with bounded retries, not globally. |

The PostgreSQL wiki's `Serializable` page describes SSI's predicate-lock machinery; it is the right tool for "I can't express this invariant declaratively" — and our double-entry invariant, by design, can always be expressed declaratively (§4.6).

### 4.5 Gapless voucher numbering

Requirement: each voucher type's number series must be **continuous** — no gaps, no duplicates — because statutory records (GST e-invoice series, statutory registers) assume them. Options:

1. **`SEQUENCE`** — rejected: sequences burn numbers on rollback, so crashes and validation failures would leave legal gaps.
2. **Advisory locks** (`pg_advisory_xact_lock(type_id)`) — viable, but the lock is invisible in the schema, unmonitored in `pg_locks` by table, and couples correctness to a magic number convention.
3. **Counter table + `SELECT … FOR UPDATE`** — chosen. The counter update lives in the *same transaction* as the voucher, so a rollback rolls the number back too; a commit publishes both atomically. The critical section is one indexed row:

```sql
INSERT INTO doc_counters (scope_key, last_value) VALUES (:scope, 0)
    ON CONFLICT (scope_key) DO NOTHING;              -- first use, race-safe
SELECT last_value FROM doc_counters WHERE scope_key = :scope FOR UPDATE;
UPDATE doc_counters SET last_value = last_value + 1 WHERE scope_key = :scope;
```

Vouchers of the same type serialize for microseconds on this row; different types proceed in parallel. With ~400 vouchers/minute on one type, expected queue depth is effectively zero. And because this is the *only* shared mutable row in the write path, the deadlock analysis is trivial: one lock, then insert-only work ⇒ no lock cycles.

### 4.6 Enforcing the double-entry invariant under concurrency

Law 1 must hold even if a rogue client (or a future contributor) inserts entries with raw SQL. Application validation is not enough; the database itself must refuse unbalanced vouchers. The problem: a CHECK constraint cannot span rows, and an immediate per-row trigger would see a *partially inserted* voucher mid-statement.

Solution: a **deferrable constraint trigger**, the PostgreSQL mechanism built for exactly this class of invariant. It queues during the transaction and fires at commit, when all the voucher's rows are visible:

```sql
CREATE CONSTRAINT TRIGGER trg_entries_balanced
AFTER INSERT ON voucher_entries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION acc_check_voucher_balanced();
-- acc_check_voucher_balanced(): if SUM(debits) <> SUM(credits) for the
-- voucher, RAISE EXCEPTION → the entire transaction, including the
-- consumed counter value, rolls back atomically.
```

Numeric equality here is exact — `NUMERIC` comparison has no epsilon. Off by 0.00000001 → abort, every time.

### 4.7 Idempotency, retries, and failure policy

- **Duplicate submissions** (double-click, retry after timeout) are killed by a `UNIQUE (client_token)` on `vouchers`; a concurrent duplicate insert gets a unique violation, the engine re-reads and returns the original voucher instead.
- **Validator/transformer plugins** run inside the transaction; any exception rolls the whole voucher back — a plugin can never corrupt a posting.
- **Post-commit event listeners** run after `COMMIT`; their failures are logged and isolated — a flaky notification plugin can never prevent a legal posting from being recorded.
- **Network-level retries** of rejected payloads are always safe: the engine is stateless between requests, and idempotency keys make replays free.

### 4.8 Decision D2 & measured throughput

**Decision:** append-only entries as the single source of truth; balances derived through covering indexes; the only row lock is the numbering counter; `READ COMMITTED`; deferred constraint triggers as the last line of defense for Law 1.

Measured (`scripts/bench_posting.py`, PostgreSQL 16.2, 8 concurrent worker threads through the public engine API, same voucher type):

| Metric | Result |
|---|---|
| Vouchers posted | 480 / 480 (0 failures, 0 retries needed) |
| Numbering | strictly gapless 1…480 across all threads |
| Deadlocks / serialization failures | 0 |
| Wall-clock throughput | **174 vouchers/s** (2-line vouchers, single type counter row) |
| Trial Balance after the run | exact — `SUM(debit) = SUM(credit)` to the last sub-cent digit |

(For context, the full test suite's concurrency case — `test_concurrency.py` — hammers the same path with 320 vouchers and asserts gaplessness and exact balance on every run in CI.)

---

## 5. Research Area 3 — Open-Source Modularity & Plugin Architecture

### 5.1 Requirements

- **M1** Build a tax-compliance pack (GST, VAT, US sales tax), a custom dashboard, or a vertical workflow **without modifying core files**.
- **M2** A plugin must be able to *participate in posting* — e.g. split a sale into CGST/SGST lines — while Law 1 still holds at commit time.
- **M3** Core/plugin API must be versioned and degradable: a plugin written for 0.1 must keep working across patch releases and fail loudly, not corruptly, across breaking ones. (Realized as the `api_compat` floor: older-minor plugins keep loading on newer builds — ADR-011.)
- **M4** Untrusted UI contributions must be sandboxable without poisoning the keyboard experience.
- **M5** The licensing must make both "community-driven core" and "commercial plugin ecosystem" possible (§5.7).

### 5.2 Precedent scan

| System | Mechanism | What we adopt | What we avoid |
|---|---|---|---|
| **Odoo** (LGPL-3 Community) | In-process modules; `_inherit` re-opens and *mutates* core model classes at load time | In-process speed; module manifests; the LGPL precedent for proprietary add-ons | Monkey-patch-style extension — silent coupling to core internals; every plugin a potential breakage of every other |
| **ERPNext** (GPL-3) | Frappe DocTypes: schema and forms generated from metadata records | Metadata-driven extensibility as a *roadmap* feature (custom fields) | Metadata indirection for the core ledger — we want the accounting kernel in plain, reviewable SQL |
| **WordPress** | Global `add_action`/`add_filter` hooks | The simplicity of "register a callback for a named event"; massive ecosystem proof | Global mutable state; no capability scoping; quality free-for-all |
| **VS Code** | Contribution points + **command registry** + activation events; full process isolation for untrusted extensions | Commands/contributions as the extension currency; the interaction model we mirror in D4; activation-on-event | Building an entire desktop-class process manager for v1 — we sandbox UI slots instead, and reserve out-of-process for integrations |
| **Firefly III** (AGPL-3) | Deliberately small API surface, rules engine | Focus: a small, stable, well-documented API beats a large accidental one | — |
| **GnuCash** | None (C core, data file) | Reminder that domain excellence ≠ extensibility | — |

### 5.3 In-process vs out-of-process

The decisive argument is **transactional correctness** (M2): computing GST lines must happen *inside* the posting transaction, so that the tax split, the base lines, and the balancing check commit or roll back as one unit. That rules out an RPC hop for anything on the write path. LedgerForge therefore defines two clean tiers:

- **In-process plugins** (Python, loaded via entry-points or a directory): `transformers`, `validators`, `report contributors`. They run inside the transaction and are treated as trusted code — like Odoo modules or Django apps.
- **Out-of-process integrations** (anything, via HTTP webhooks / message queue): subscribe to **domain events** (`voucher.posted`, `master.created`) after commit. Payment gateways, e-invoicing bridges (e.g. India's IRN flow), BI sync all live here. Latency-tolerant by contract.

### 5.4 The LedgerForge plugin runtime

```
Voucher payload
     │
     ▼
 [transformer hooks]  ── e.g. GST pack appends CGST/SGST lines, adjusts party line
     │
     ▼
 [validator hooks]    ── e.g. "no Sunday posting" policy, credit-limit check → veto
     │
     ▼
 Engine validation (Law 1/3: exact Decimal balance, scale, ledger existence)
     │
     ▼
 Counter lock → number → INSERT voucher + entries → COMMIT
     │
     ▼
 [event listeners]    ── after-commit: webhooks, notifications, caches
```

- **Discovery:** `importlib.metadata` entry-points (group `ledgerforge.plugins`) for pip-installed packs, plus a scan of `LF_PLUGIN_PATH` for drop-in directories — the Odoo-style "just put the folder there" experience.
- **Contract:** a plugin module exposes `PLUGIN = PluginManifest(name, version, api_compat)` and `def register(registry: PluginRegistry)`. The registry is the *only* sanctioned integration surface; it is semver'd as `ledgerforge.plugin_api`.
- **Hook taxonomy (v0.1):** `register_transformer(fn)`, `register_validator(fn)`, `on(event, handler)`; report contributors and UI slots are registered by manifest declaration and activated by the host.
- **Failure isolation:** transformers/validators raising ⇒ whole posting aborts cleanly (a plugin can veto but never corrupt); event handlers raising ⇒ logged, isolated, posting already safe.
- **Worked example in this repo:** `ledgerforge/contrib/example_gst.py` — an intra-state GST transformer that turns a ₹1,000 sale into CGST/SGST lines with exact Decimal math, plus a no-weekend-posting policy validator.

### 5.5 UI & dashboard extensibility (design, Phase-3 delivery)

- Dashboards are **plugin-declared slots** rendered by the host; a slot may be a federated module (Module Federation, shared React runtime — trusted sources) or a sandboxed `iframe` speaking the versioned REST API (untrusted sources). Sandboxed slots never receive keyboard scope: they must register *commands* with the host, and the host binds keys to them — so M4 and D4 hold together.
- Every plugin command appears in the **command palette**, giving mouse-free discoverability to third-party functionality for free.

### 5.6 Governance

- `ledgerforge.plugin_api` follows semver; breaking changes ship with a deprecation window (two minor releases), and `api_compat` lets the loader refuse loudly instead of misbehaving.
- A community plugin index (curated list + CI badge requirements) is a roadmap milestone; until then, entry-points + the `plugins/` convention serve as the mechanism.

### 5.7 Licensing decision (D5)

| Candidate | SaaS/network fork protection | Plugin ecosystem freedom | Precedent in accounting OSS | Verdict for core |
|---|---|---|---|---|
| **AGPL-3.0** | ★★★ (§13 network clause) | ★★ (boundary needed, see below) | Firefly III (AGPL-3.0, ~25k stars — verified via GitHub API) | **Chosen** |
| GPL-3.0 | ★★ (SaaS hole) | ★★ | ERPNext (GPL-3.0, ~41k stars — verified) | Runner-up |
| LGPL-3.0 | ★ | ★★★ | Odoo Community | Better if proprietary plugins were the priority |
| Apache-2.0 / MIT | ✗ | ★★★ | — | Rejected: permits closed forks of the commons |

**Decision:** the core is **AGPL-3.0**. A community-driven accounting engine lives or dies by the commons staying open — including against hosted derivatives that would otherwise take without giving back. The plugin boundary is designed to keep the ecosystem healthy: plugins interact with the core exclusively through the versioned, documented `PluginRegistry` API at arm's length, and may carry any license their authors choose. (This is engineering guidance on where the boundary sits, not legal advice; commercial redistributors should seek their own counsel.)

---

## 6. Research Area 4 — Keyboard-First UX Architecture

### 6.1 What makes TallyPrime fast (interaction analysis)

TallyPrime's speed is not one feature; it is an interaction contract:

1. **Two keys do all navigation.** `Enter` moves forward (accept field, next field, submit); `Esc` moves back (leave field, leave screen, previous menu). There is no third navigation verb, so muscle memory is universal across every screen.
2. **Every menu item is one keystroke.** Gateway menus are navigated by the item's highlighted letter — no arrow-key hunt.
3. **Voucher types are F-keys.** `F4` Contra, `F5` Payment, `F6` Receipt, `F7` Journal, `F8` Sales, `F9` Purchase; `F2` changes the date — mode switches never touch the mouse.
4. **Masters are created mid-flight.** `Alt+C` inside any ledger field creates the missing ledger right there and returns to the voucher — the flow never breaks to a different app area.
5. **Type-ahead selection over an in-memory list.** Typing ledger-name prefixes jumps to matches instantly; the whole chart of accounts is already in memory, so selection latency is zero.
6. **`Ctrl+A` accepts the voucher; `Alt+D` deletes a line/entry** — accept and destroy are also single chords.

The web reinterpretation must preserve the *contract*, not the keystrokes per se — though the default keymap (below) keeps Tally veterans productive on day one.

### 6.2 Web platform realities

- **Key events:** one `keydown` listener at `document` with `capture: true` sees every key before widget code; `event.key` gives portable semantics; `code` distinguishes physical keys (numpad `Enter` vs main `Enter` — Tally uses that distinction, and so can we).
- **IME/composition:** non-Latin input (Hindi transliteration, CJK) fires `compositionstart/update/end`; a keyboard framework must suppress chord handling during composition or it will eat characters.
- **Browser-reserved chords:** `Ctrl+W`, `Ctrl+N`, `Ctrl+T` cannot be intercepted; the default map must avoid them (this is why accept is `Ctrl+A`, not `Ctrl+S`, which the browser tolerates but users associate with "save file" in some hosts).
- **Focus is the state.** The DOM owns focus; a keyboard architecture is fundamentally a *focus management* architecture (§6.4).
- **Latency is physics.** Tally reads a local file; we post over HTTP. The answer is optimistic UI + in-memory indexes, with the network only in the critical posting path (§6.6).
- **Accessibility is a feature, not a tax:** an explicit command/focus model is precisely what screen readers understand (`role`, `aria-activedescendant`, roving tabindex).

### 6.3 Frontend framework choice (D4)

| Criterion | React 18 + TS | Vue 3 | Svelte 5 | SolidJS |
|---|---|---|---|---|
| Ecosystem for plugin dashboards (D3/M5) | ★★★ (largest; Module Federation mature) | ★★ | ★ | ★ |
| Controlled-input reliability for rapid numeric entry | ★★★ | ★★★ | ★★ | ★★ |
| Talent pool for a community project | ★★★ | ★★ | ★ | ★ |
| Raw render performance | ★★ (more than sufficient) | ★★ | ★★★ | ★★★ |
| Hiring/onboarding friction | ★★★ | ★★ | ★ | ★ |

**Decision:** a **React 18 + TypeScript + Vite host**, with the entire keyboard framework written as **framework-agnostic TypeScript** (`frontend/keyboard-kit`) and only thin React bindings on top. This keeps the interaction core portable (a Svelte or even web-components host remains possible) while betting the ecosystem layer on the largest community — which is what a plugin marketplace needs. Preact's `react` alias is available as a compatibility escape hatch for low-end terminals.

### 6.4 The interaction architecture: commands, keymaps, focus scopes

The heart of the design — a VS Code-grade interaction spine, in four pieces:

1. **Command Registry.** Every action is a `Command { id, title, run(args), when(context) }`. UI never handles keys directly; it registers commands. This one indirection buys: discoverability (the command palette lists everything), remappability, macro recording (roadmap), and plugin actions for free.
2. **Keybinding Resolver.** Declarative bindings `Keybinding { key: 'Alt+C', command: 'ledger.create.here', when: 'voucherEditor.active' }`, with chord support (`'K K'`), platform normalization (⌘/Ctrl), and **context predicates** evaluated against a stack of scopes. Resolution order: deepest scope wins; a binding marked `when: 'input.focused'` yields to plain text entry unless it is a chord.
3. **Focus Scope Stack.** Screens push scopes (`gateway`, `voucherEditor`, `ledgerPicker`); `Esc` pops the top scope (Tally's universal back); scopes own roving-focus lists (`ArrowUp/Down` inside a picker, `Enter` to drill in). Focus is *never* left implicit.
4. **Single capture-phase pipeline.** One `keydown` listener → suppress during IME composition → resolve against current scopes → if matched: `preventDefault()` + `execute(command)`; else let the browser type. One pipeline means zero key-handling races between widgets — the classic source of double-handled keys in web apps.

The default keymap ships as data (`tallyKeymap.ts`), mirroring TallyPrime: `Esc` back · `Enter` forward · `Ctrl+A` accept voucher · `Alt+C` create ledger here · `Alt+D` delete line · `F2` date · `F4/F5/F6/F7/F8/F9` voucher-type switch · `Ctrl+Enter` alter selection · `Ctrl+P` print. Users rebind via `setKeybinding`; the map persists to settings and the palette always shows the current binding.

### 6.5 The voucher entry grid

- A virtualized line grid; `Enter` moves to the next cell and auto-appends a new line at the last cell (no "Add line" trip to the mouse), `Alt+D` removes the line.
- **Ledger picker:** type-ahead over a prefetched ledger index (a book has thousands of ledgers, not millions — the entire index ships to the client on login and stays warm via websocket invalidation). Ranking: exact match → prefix match → substring; `ArrowUp/Down` + `Enter` selects; `Alt+C` creates a new ledger in-place through the API without leaving the grid.
- **Amount fields are strings, formatted but never parsed through floats** (Law 3 on the wire: JSON amounts are strings; display uses `Intl.NumberFormat` with fixed scale; the engine is the only arithmetic authority).
- **Auto-balance HUD:** a persistent "Amount left: 250.00" footer — the remaining difference computed as exact decimal string math client-side — so the balancing line is always one `Enter` away, exactly like Tally's accept-time balance prompt.

### 6.6 Latency budget

| Interaction | Budget | Technique |
|---|---|---|
| Keystroke → echo | < 16 ms (one frame) | Local echo; controlled inputs; no per-key network |
| Ledger picker open | < 50 ms | In-memory index; virtualized list |
| Voucher accept → engine ack (LAN) | < 300 ms P50 | Optimistic "Posting…" state; single POST; idempotency key enables safe retry |
| Report open (Trial Balance) | < 1 s | Server-side SQL functions (§3.8); streamed JSON; render-as-you-parse |
| Cold app load | < 2 s | Vite code-splitting, HTTP caching, masters prefetched during login |

### 6.7 Accessibility

Keyboard-first *is* the accessibility model: every command has a human title (palette = screen-reader command list), focus is explicit and visible, roving tabindex keeps grid semantics (`role="grid"` / `gridcell`), and live regions announce posting results. A mouse-first UI retrofit for a11y is a bug factory; a keyboard-first UI gets it structurally.

### 6.8 Reference implementation

`frontend/keyboard-kit/` contains the framework-agnostic core — `commandRegistry.ts`, `keybindings.ts` (parser + matcher), `keymapService.ts` (the capture-phase pipeline, chords, rebinding), `focusStack.ts` — plus React bindings (`useCommand`, `<KeyScope>`) and the `tallyKeymap.ts` preset. See `docs/keyboard-ux.md` for the full plan and wiring examples.

---

## 7. The Three Financial Laws → Engineering Controls

| Law | Layer | Control | Where | Proven by |
|---|---|---|---|---|
| **L1 · Debits = Credits, exactly, or abort** | App | `Decimal` summation of D and C legs before any insert; mismatch ⇒ `UnbalancedVoucherError`, HTTP 422, nothing written | `engine/posting.py` | `test_double_entry.py::test_off_by_a_hundredth_of_a_cent_rejected` |
| | DB | `DEFERRABLE INITIALLY DEFERRED` constraint trigger re-verifies `SUM(D) = SUM(C)` at commit; also enforces ≥ 2 lines per voucher | `db/schema.sql` (`trg_entries_balanced`) | `test_double_entry.py::test_db_rejects_raw_sql_unbalanced_insert` |
| **L2 · History is immutable** | App | No update/delete code paths exist; corrections = `POST /vouchers/{id}/reverse` which inserts a mirror voucher (swapped D/C) linked via `reversal_of` | `engine/reversal.py` | `test_immutability.py`, `test_posting.py::test_reversal_nets_to_zero_and_original_is_intact` |
| | DB | `BEFORE UPDATE OR DELETE` triggers raise on `vouchers`, `voucher_entries`, `audit_log`; app DB role is granted only `SELECT, INSERT` on them | `db/schema.sql` (`trg_*_immutable`, role grants) | `test_immutability.py::test_update_voucher_raises`, `test_delete_voucher_raises` |
| **L3 · Decimals only, floats banned** | Wire/API | Amounts **must** arrive as JSON strings; `float` payloads are rejected with HTTP 422 (a JSON number has already lost precision by the time JS/JSON parsers are done with it); scale > 8 rejected | `schemas.py` (`AmountIn`) | `test_decimal_law.py::test_float_rejected`, `test_too_many_decimals_rejected` |
| | App | `decimal.Decimal` end-to-end; context precision 50 | `engine/posting.py` | `test_decimal_law.py::test_one_tenth_plus_two_tenths` |
| | DB | `NUMERIC(26,8)` columns — exact, arbitrary-precision decimal storage; sums in SQL are exact | `db/schema.sql` | `test_reports.py::test_trial_balance_exact_zero_difference` |
| **(Derived) Gapless numbering** | DB+App | Counter row locked inside the posting transaction (§4.5) | `engine/posting.py` | `test_concurrency.py::test_gapless_under_threads` |
| **(Derived) Idempotent posting** | DB | `UNIQUE (client_token)`; replay returns the original voucher | `engine/posting.py` | `test_posting.py::test_idempotent_replay_returns_original` |

---

## 8. Implementation Overview (Phase 2)

Everything decided above is implemented and tested in this repository.

### 8.1 Repository layout

```
ledgerforge/
├── db/
│   ├── schema.sql              # DDL: masters, vouchers, closure, counters, triggers, roles
│   ├── functions.sql           # rpt.trial_balance / group_rollups / profit_and_loss / balance_sheet
│   └── (the chart-of-accounts seed is code now: ledgerforge/seeding.py — ADR-009)
├── src/ledgerforge/
│   ├── engine/                 # posting, reversal, opening balances (the ACID core)
│   ├── api/                    # FastAPI routers: masters, vouchers, reports
│   ├── reporting/              # thin Python wrappers over rpt.* SQL functions
│   ├── plugins/                # PluginManager, registry, hooks, events
│   └── contrib/example_gst.py  # worked plugin: GST transformer + policy validator
├── frontend/keyboard-kit/      # framework-agnostic keyboard command framework (TS)
├── tests/                      # 30 tests: laws, hierarchy, immutability, reports, concurrency, plugins
├── scripts/                    # bench_hierarchy.py, bench_posting.py, init_db.sh
├── docs/                       # this analysis, ADRs, keyboard-ux plan, plugin guide
└── docker-compose.yml, Dockerfile, Makefile, .github/workflows/ci.yml
```

### 8.2 Posting flow (engine, one ACID transaction)

1. Idempotency check on `client_token` (return existing voucher if seen).
2. Resolve voucher type + ledgers (by id or by unique name — Tally-style name entry).
3. **Plugin transformers** may add lines (e.g. CGST/SGST split).
4. **Plugin validators** may veto.
5. Engine validation: ≥ 2 lines, `Decimal` amounts > 0, scale ≤ 8, **exact** D = C balance.
6. Lock the type's counter row (`FOR UPDATE`), take the next gapless number.
7. `INSERT` voucher + entries (entry rows carry a denormalized `voucher_date` for covering-index reporting; a trigger proves it equals the voucher's date).
8. The deferred balance trigger re-verifies Law 1 at `COMMIT`.
9. After commit: `voucher.posted` event fires for listeners.

### 8.3 Reporting engine

Implemented as PostgreSQL functions in `db/functions.sql` (callable as raw SQL — `SELECT * FROM rpt.trial_balance('2026-11-06')` — and exposed as REST endpoints). All rollups are closure-table joins (§3); all arithmetic is exact `NUMERIC`. The Trial Balance response computes and returns its own `difference` field, which the test suite asserts is exactly `0` — the engine proves its own balance on every report.

### 8.4 Test results

`make test` runs the full suite against a real PostgreSQL (CI uses the `postgres:17` service container; locally, any PostgreSQL 16+). The suite covers every law (§7), the closure maintenance triggers (including subtree moves and cycle rejection), reporting invariants (Trial Balance nets to zero; Balance Sheet balances via the computed net-profit plug; Gross/Net P&L splits by `affects_gross`), the plugin runtime (GST transformer output, validator veto, after-commit events), and concurrency (§4.8).

### 8.5 Quickstart

```bash
docker compose up -d          # PostgreSQL 17 + API on :8000
# or locally:
make init-db                  # apply schema + seed to $DATABASE_URL
make dev                      # uvicorn --reload
curl -s localhost:8000/api/v1/reports/trial-balance?as_of=2026-11-06
```

---

## 9. Risks & Roadmap

| Item | Status / risk | Plan |
|---|---|---|
| Multi-currency | **Landed (v0.4)** | `NUMERIC(30,10)` FX rates + base-currency posting rules with entry-level snapshots; `0002_multi_currency` over the frozen baseline (ADR-007) |
| Period locking / FY close | **Landed (v0.4)** | `periods` with the open→locked→closed state machine, a row-locking posting gate and the `rpt.fy_close` sweep; `0003_period_locking` (ADR-008) |
| Multi-company / multi-book | **Landed (v0.5)** | `books` table + `book_id` discriminator on every book-owned table (0004, taken while cheap); book-scoped uniqueness, numbering, periods and reports; `X-Book-Id` header with single-book back-compat; per-book permissions as the token's `books` claim (ADR-009) |
| AuthN/AuthZ | **Landed (v0.3)** | OIDC bearer verification (JWKS + rotation refresh) with the `reader` < `bookkeeper` < `admin` role model on every route; `LF_AUTH_MODE=disabled` remains the dev-only default |
| Alembic migrations | **Landed (v0.3/v0.4/v0.5/v0.6)** | Frozen `0001_baseline` with a structural drift guard in CI (ADR-006); forward revisions `0002`–`0005` proved convergent with the bootstrap by catalog fingerprint |
| Invoicing | **Landed (v0.6)** | The first plugin-pack feature, zero core logic: `vouchers.meta` (0005, ADR-010) persists the commercial document; the `invoicing` pack derives the TallyPrime GST-sale lines from `meta.invoice` (items/qty/rate/slabs), settles via `meta.receipt`, and annotates computed totals that round-trip through `GET /vouchers/{id}` |
| Inventory | **Landed (v0.7)** | The second plugin pack, **zero schema change** (ADR-011): goods-in/out/transfer/recount documents on `meta.stock`; positions are a deterministic fold over posted documents (weighted-average valuation whose rounding closes exactly), plugin API 0.3 adds the document-store read (`Resolver.prior_documents`, reversals included) and transaction-scoped advisory locks (`Resolver.lock`); `api_compat` became a floor |
| Payroll | **Landed (v0.8)** | The third plugin pack, again **zero schema change** (ADR-012): run/payout documents on `meta.payroll` derive the TallyPrime payroll journal (earnings, withheld, employer contributions, net pay); YTD is a fiscal-year fold and payable an all-time fold over posted documents — accumulators window, balances never expire; one run per employee per period (lock-serialized); plugin API 0.4 adds the one fact a YTD fold needs (`Resolver.fy_start_month`) |
| Print bridge | **Landed (v0.9)** | Every voucher as PDF (ADR-013): plugin API 0.5 adds document views — packs own their documents' print layouts (`DocumentContext` in, `DocumentView` out, `None` declines), one core layout engine renders them and a dependency-free PDF 1.4 writer (base-14 Helvetica, exact-decimal edge formatting) draws the bytes; `GET /vouchers/{id}/pdf` dispatches meta keys to factories with the voucher slip as the universal fallback — payslips (with YTD and the payable fold), tax invoices, and the payslip screen (`Alt+P`, `Ctrl+P`) |
| Report scale-out | Watch | Partitioning + BRIN + optional eventually-consistent balance cache (§4.3) — only when books pass millions of entries |
| Plugin trust model | Watch | Signed plugin index, capability-scoped registry, sandboxed UI slots (§5.5) |

---

## 10. References

1. Tally Solutions — *Ledgers and Groups in TallyPrime* (28 predefined groups; 15 primary): https://help.tallysolutions.com/ledgers-and-groups-in-tallyprime
2. PostgreSQL Documentation — *Numeric Types* (exact, arbitrary precision; "use the numeric type" for monetary amounts): https://www.postgresql.org/docs/current/datatype-numeric.html
3. PostgreSQL Documentation — *Monetary Types* ("Floating point numbers should not be used to handle money due to the potential for rounding errors"): https://www.postgresql.org/docs/current/datatype-money.html
4. PostgreSQL Documentation — *Transaction Isolation* (MVCC, isolation levels, SSI): https://www.postgresql.org/docs/current/transaction-iso.html
5. PostgreSQL Documentation — *Explicit Locking* (`SELECT … FOR UPDATE` semantics): https://www.postgresql.org/docs/current/explicit-locking.html
6. PostgreSQL Documentation — *LOCK* (deadlock avoidance via consistent lock ordering): https://www.postgresql.org/docs/current/sql-lock.html
7. PostgreSQL Wiki — *Serializable* (SSI, predicate locks): https://wiki.postgresql.org/wiki/Serializable
8. Novotný, P. et al. (2024) — *The relational modeling of hierarchical data in biodiversity databases* (adjacency / materialized path / closure properties): https://pmc.ncbi.nlm.nih.gov/articles/PMC11466226
9. Joe Celko — *Trees and Hierarchies in SQL for Smarties* (closure table pattern, book reference)
10. PostgreSQL Documentation — *ltree* (materialized-path extension, evaluated in §3.3): https://www.postgresql.org/docs/current/ltree.html
11. Python Documentation — *decimal* module (arbitrary-precision exact arithmetic): https://docs.python.org/3/library/decimal.html
12. License verification via GitHub REST API (2026-11-06): frappe/erpnext = GPL-3.0; firefly-iii/firefly-iii = AGPL-3.0 — https://api.github.com/repos/{repo}
13. Odoo — module/extension architecture and licensing (LGPL-3 Community): https://github.com/odoo/odoo
14. VS Code — contribution points & command-based extensibility: https://code.visualstudio.com/api/references/contribution-points
15. GST portal (India) — e-invoicing and series requirements context: https://www.gst.gov.in


