# ADR-008: Period locking and the financial-year close

- **Status:** Accepted (2026-11-06)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

Issue #5 asks for a `periods` table, a posting gate that refuses inserts into closed dates, and the FY-close report that sweeps P&L into *Profit & Loss A/c*. The design tension: a lock must be race-free against in-flight postings (the engine is concurrent by design — ADR-002), closing a year must not corrupt either year's reports, and "closed" must mean something irreversible without violating Law 2 (vouchers are never edited or deleted — including the close sweep itself).

## Decision

1. **Periods are named, non-overlapping date ranges** with a status machine: `open → locked → closed`. Overlaps are rejected by a gist `EXCLUDE` constraint on `daterange(starts_on, ends_on, '[]')` (no extension needed — range types ship with gist support). Dates are immutable once created; deletion is trigger-guarded (only open, empty periods). The feature is **opt-in**: with an empty `periods` table every date posts, exactly as before — existing books are unaffected until they define periods.

2. **Once periods exist, a voucher date must fall in an OPEN period.** Dates covered by no period are refused too ("create the period first") — which also catches typo dates like 2206-11-06 that happen to be past-looking. This is the discipline TallyPrime applies to financial years, made structural.

3. **The gate is a BEFORE INSERT trigger on `vouchers` that takes `FOR SHARE` on the covering period row.** A posting holds a share lock until commit; a concurrent lock/close takes the row exclusively and waits for in-flight postings first. Whichever commits first defines the outcome for the other — no race window, no lock-order deadlock (single row, share-compatible among posters). The engine repeats the check before insert for friendly 409/422 errors; the trigger is the commit-time truth, the same pairing as Law 1.

4. **Closing is two-step: lock, then close.** `lock` freezes dates for review (postings get 409). `close` is the financial-year finish:
   - the sweep is computed by `rpt.fy_close(as_of, pl_ledger_id)` — for every income/expense ledger with a non-zero **closing balance** (cumulative, so prior unswept years are swept too), the zeroing line, plus the balancing line on *Profit & Loss A/c*. Balanced by construction (every line is an ABS of a balance; the P&L line absorbs the algebraic sum — Law 1 exact);
   - it posts as an **FYC** voucher dated the **first day of the next period** (which must exist and be open) — so the closed year's own P&L stays intact and the next year starts from zeroed income/expense ledgers with the accumulated result in P&L A/c;
   - then the period flips to `closed` — final. `closed` can never transition anywhere (trigger-enforced), and `closed_at`/`closed_by` are stamped (the guard trigger demands `closed_by`).

5. **`rpt.profit_and_loss` excludes FYC vouchers** (revision 0003 also ships this function change; its downgrade restores the baseline version). Closing entries are transfers, not earnings — without the exclusion, the year *after* a close would report the sweep as a loss. Trial balance and balance sheet deliberately include FYC: the zeroed ledgers and the P&L A/c balance are real balances, and the balance-sheet equation holds before and after a close (the sweep moves profit from the synthetic plug into a real ledger).

6. **Crash safety and idempotency.** The sweep's `client_token` is `uuid5(NAMESPACE_URL, "ledgerforge:fy-close:{period_id}")` — a retried close replays instead of double-sweeping. A sweep that posted before a failed status flip is reused by the retry. **Unlock refuses once a sweep exists** — vouchers are immutable (Law 2), so a reopen between sweep and close could leave a stale sweep; the refusal message points at a correcting journal instead.

7. **Period mutations are admin-role actions** (the API layer is the enforcement point; `reader` can list and preview). Lock/close/unlock record the acting principal in the periods audit trail (`app.actor`), so who closed the year is always answerable.

## Consequences

- **(+)** Locking is race-free by row locking, not by hope; the FOR SHARE pattern serializes postings against locks without blocking posters among themselves.
- **(+)** "Closed" is database-final — no API path, and no app-role SQL, can reopen or post into it.
- **(+)** The close preview IS the close: `GET /periods/{id}/close-preview` (and `GET /reports/fy-close?as_of=…`) return exactly the lines `close` will post.
- **(+)** Both years' reports stay correct after a close (FYC exclusion), and the balance sheet balances before and after.
- **(−)** Closing a year requires the next period to exist and be open — honest bookkeeping, but a two-step setup. The 422 says exactly what to create.
- **(−)** `FYC` is a reserved system voucher type: manually posting FYC vouchers is possible (any balanced journal is) but they are excluded from P&L reporting.
- **(−)** No partial/soft locks yet (e.g., "locked except adjustments by role X") — the state machine has three states; richer policies belong in the plugin layer when asked for.

Shipped as revision `0003_period_locking` (ADR-006 post-freeze policy) together with the FYC-aware `profit_and_loss`, the seeded FYC voucher type, and the `rpt.fy_close` report function; `db/schema.sql` + `db/functions.sql` were updated in the same change and the drift guard proves the paths identical.
