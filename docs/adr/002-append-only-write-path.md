# ADR-002: Write path — append-only entries, derived balances, counter-row numbering

- **Status:** Accepted (2026-11-06)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

Hundreds of vouchers post concurrently; hot ledgers (Cash) appear on a large share. Options: row-locked mutable balance columns vs append-only entries with derived balances. Gapless statutory voucher numbering is required. Full analysis: `docs/analysis.md` §4.

## Decision

1. `voucher_entries` is the **only** financial fact table; **no mutable balance columns exist**. Balances are derived via covering index `(ledger_id, voucher_date) INCLUDE (dc, amount)`.
2. The **only** shared mutable row in the write path is `doc_counters`, locked with `SELECT … FOR UPDATE` inside the posting transaction to produce gapless numbers that roll back with the voucher.
3. Isolation stays `READ COMMITTED`; invariants are enforced by constraints and the counter lock, not by isolation level.
4. Law 1 is defended at the database by a `DEFERRABLE INITIALLY DEFERRED` constraint trigger that re-verifies `SUM(D) = SUM(C)` at commit.
5. Idempotency: `UNIQUE (client_token)`; replays return the original voucher.

## Consequences

- **(+)** Concurrent postings to the same ledger never block each other; no lost updates; no deadlocks (single-lock ordering).
- **(+))** Audit trail and time-travel are native (Law 2 by construction).
- **(−)** Reads aggregate instead of reading a column — bounded by covering indexes; scale-out path is partitioning/BRIN/optional caches without changing the model.

## Alternatives rejected

- **Mutable balances + row locks:** hot-row serialization, deadlock discipline tax, lost-update class of bugs, mutable derived state (contradicts Law 2).
- **Sequences for numbering:** gaps on rollback are legally unacceptable.
- **Advisory locks:** invisible in schema, opaque in `pg_locks`, magic-number coupling.
- **SERIALIZABLE everywhere:** retry churn on a hot counter with no anomaly surface to defend against.
