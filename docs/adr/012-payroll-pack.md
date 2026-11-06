# ADR-012: Payroll as a plugin pack — fiscal-year folds on the document store

- **Status:** Accepted (2026-11-07)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

The roadmap (§9) listed payroll as "a plugin on the same hook taxonomy + document store; the v0.7 read/lock primitives are its building blocks". Inventory (ADR-011) proved the pattern — derived state as a deterministic fold over `vouchers.meta`, advisory locks for serialization — for quantities. Payroll needs the same pattern for a different kind of state: an employee's **year-to-date accumulators** (the statutory view: PF/ESI wage bases, Form-16 style totals) and their **payable balance**. Both are folds over posted documents; neither should be stored, because both are recomputable from history the ledger already guarantees immutable (Law 2).

One fact was missing from the plugin API: a YTD window is a property of the *book* — its fiscal-year start month (`books.fy_start_month`, April by default, immutable once the book has vouchers). Reading `Resolver._book` from a pack would break the only-stable-API rule that makes contrib packs proof the API is sufficient.

## Decision

### 1. Plugin API 0.4 — one read-only fact on `Resolver`

**`fy_start_month`** — the book's fiscal-year start month (1–12), the one thing a year-to-date fold needs: months ≥ `fy_start_month` open a fiscal year. Additive, so the floor semantics (ADR-011) hold: `example-gst` (0.2), `invoicing` (0.2) and `inventory` (0.3) keep loading on a 0.4 build; a 0.4 plugin on a 0.3 build refuses loudly. The payroll pack declares `api_compat="0.4"` and is the live proof.

### 2. The payroll pack (`ledgerforge/contrib/payroll.py`)

Two document modes on `meta.payroll`, zero schema change — the second roadmap feature in a row to land with no migration:

| Mode | Posts | Derived state |
|---|---|---|
| `run` | Per employee: Dr each earning expense (*Indirect Expenses*) · Dr employer-contribution expense + Cr its liability · Cr each withheld liability (*Duties & Taxes*) · Cr **net pay** to the employee's payable ledger (*Provisions*, default the employee's name). One voucher carries the whole run — the TallyPrime payroll journal. | FY-windowed YTD per employee and per component, annotated into `meta.payroll.computed` — the payslip data |
| `payout` | Dr the employee's payable · Cr the bank (*Bank Accounts*) — the invoicing pack's receipt, mirrored. | The all-time payable after settlement (`payable_after`) |

Components are pay heads with a `kind`, the TallyPrime taxonomy in miniature: **`earning`** (Dr expense), **`withheld`** (Cr liability, reduces net — the expense was booked at gross), **`employer`** (Dr expense AND Cr a required `liability` — commonly the same "PF Payable" the withheld side feeds; never touches net pay). Ledger placement is overridable per component (`group`, `liability_group`) and per employee (`payable_ledger`, `payable_group`); the auto path refuses to silently reuse an unrelated existing ledger, exactly like inventory's item-name guard.

### 3. Two windows over one fold

`_fold` is the single source of payroll arithmetic, run by the transformer and by every later replay:

- **Accumulators window by fiscal year**: a run's YTD covers the same FY (by `fy_start_month`) with period ≤ the run's own — a September payslip never shows October. `payroll.ytd(resolver, employee, fy=…)` replays the window for bridges and tests.
- **Balances never expire**: payable = accrued net − payouts, all-time, across fiscal years — a March salary paid in April still settles. `payroll.payable(resolver, employee)` replays it; the payout guard is policy on top of it (overpaying a payable ledger is book-legal — it goes negative as a recoverable advance — so the default 422 escapes with `policy:allow_overpay`, mirroring `policy:allow_negative_stock`).

Reversals net to zero on both views: the reversal carries empty meta (ADR-010), `prior_documents` delivers it alongside its original, and the fold skips the pair — undoing a run restores YTD and payable just as the mirrored entries restore the ledgers.

### 4. One run per employee per period

Checked against prior documents (reversed runs don't count), serialized by `resolver.lock("payroll:{employee}")` in one sorted pass per document — the ADR-011 lock discipline, so concurrent duplicate runs cannot both pass the guard (pinned by a 3-thread test: exactly one success). The guard is policy, not law: documented off-cycle corrections override with `policy:allow_duplicate_run` and then count in YTD. Payouts require a payroll relationship — paying a stranger is a plain payment voucher, not a payroll document.

### 5. Guards (all 422)

Malformed periods (strict `YYYY-MM`), duplicate employees within a run, duplicate component names per employee (one name must not resolve two ledgers), kinds outside earning/withheld/employer, non-positive or non-finite amounts, employer contributions without a liability, withheld exceeding gross, periods that have not started (same `policy:allow_future_document_dates` flag as future voucher dates), future voucher dates, document-mode conflicts (`payroll` vs `gst`/`invoice`/`receipt`/`stock`, vetoed by all packs), FX lines, and employee names colliding with unrelated ledgers.

## Consequences

- **(+)** The roadmap's last planned feature line lands *entirely outside core*: no tables, no migration, no `rpt.*` change — one module + tests, and uninstalling it removes payroll without touching a row of accounting history. The plugin API is now proven across three packs and four minors, each addition motivated by exactly one need.
- **(+)** The two-window fold is a reusable pattern for any periodic-accrual domain (subscriptions, rent, royalties): accumulate within a fiscal window, settle against an all-time balance.
- **(−)** The fold is O(all payroll documents of the book) per posting and per query — fine at payroll-shop scale (hundreds of runs), not at 100k-employee scale. The escape hatch is ADR-010's: promote accumulators to schema through a migration, keep the documents as provenance.
- **(−)** v1 scope, documented: attendance-derived pay (per-day rates, leave encashment) and per-employee statutory caps are out — amounts arrive as exact figures, and computing them is the caller's job; payslip PDFs and PF/ESI filing bridges subscribe to the `RUNS` after-commit seam; no payroll-specific voucher type (runs post as `JV`, payouts as `PYT`).

Shipped with no migration: `alembic` head remains `0005_voucher_meta`. The consumer is `ledgerforge/contrib/payroll.py`, registered as an entry point and loaded at boot; exercised by `tests/test_payroll_plugin.py` (journal shape, FY windowing incl. the previous-FY boundary, payout settlement and guards, duplicate-run and reversal semantics, idempotent replay, book isolation, master placement and collision guards, and the concurrent-duplicate race the advisory lock turns into exactly one success).
