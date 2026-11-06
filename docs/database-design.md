# Database Design Reference

**Schema source of truth:** `db/schema.sql` (bootstrap DDL) · `db/functions.sql` (reporting functions) · `ledgerforge/seeding.py` (TallyPrime-style chart of accounts — one definition for bootstrap and `POST /api/v1/books`, ADR-009) · **Versioned as:** Alembic revisions `0001_baseline` → `0005_voucher_meta` (ADR-006; `tests/test_migrations.py` keeps the head and the bootstrap identical) · **Decisions:** ADR-001, ADR-002, ADR-009, ADR-010, `docs/analysis.md` §3–§4.

## Entity map

```
books (one row per company: base_currency, fy_start_month — ADR-009)
groups (tree: parent_id + same-book composite FK, per-book unique names)
  ←── group_closure (read model, trigger-maintained)
  └── ledgers (unique name per book, like Tally per company)
voucher_types (RCT, PYT, JV, CN, SAL, PUR, CRN, DBN, OPEN, FYC … — per book)
  └── vouchers (immutable: uuid, client_token per book, voucher_no, date, reversal_of, meta document)
        └── voucher_entries (immutable: line_no, ledger_id, book_id, dc D|C, amount NUMERIC(26,8),
                             FX snapshot: currency, amount_fx, fx_rate — issue #4 / ADR-007)
currencies (ISO 4217, code-keyed)  ←── fx_rates (dated pairs, NUMERIC(30,10) — ADR-007)
periods (non-overlapping ranges, open/locked/closed — issue #5 / ADR-008)
doc_counters (gapless numbering, the only locked row in the write path)
audit_log (append-only trigger journal for masters)
settings (fy_start_month, base_currency, company_name)
```

## Tables

### `groups`
| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT IDENTITY PK` | |
| `name` | `TEXT UNIQUE NOT NULL` | Tally-style unique group names |
| `parent_id` | `BIGINT → groups.id` | `ON DELETE RESTRICT`; `NULL` only for the root "Primary" |
| `nature` | `TEXT CHECK IN ('asset','liability','equity','income','expense')` | drives Balance Sheet / P&L placement |
| `affects_gross` | `BOOLEAN` | drives the trading-account (gross profit) split |
| `code`, `created_at`, `updated_at` | | audit columns |

### `group_closure`
`(ancestor_id, descendant_id, depth)` PK on the pair; maintained by triggers on `groups` (insert / re-parent; cycles rejected `BEFORE UPDATE`). Reports read **only** this table.

### `ledgers`
`id`, `name UNIQUE`, `group_id → groups`, `code`, `is_system` (Cash, Profit & Loss A/c), timestamps.

### `voucher_types`
`id`, `code UNIQUE` (`RCT`, `PYT`, `JV`, `CN`, `SAL`, `PUR`, `CRN`, `DBN`, `OPEN`), `name`, `number_prefix`, `restart_yearly`, `is_system`.

### `vouchers` (immutable)
`id`, `uuid`, `client_token UNIQUE` (idempotency), `voucher_type_id`, `voucher_no` (+ `UNIQUE (voucher_type_id, voucher_no)`), `sequence` (gapless int), `voucher_date`, `narration`, `reversal_of → vouchers.id`, `posted_at`, `meta JSONB NOT NULL DEFAULT '{}'` — the plugin document store (ADR-010): opaque to the kernel, never read by core validation or `rpt.*`; the engine persists the post-transform draft meta so plugin annotations (e.g. `invoice.computed`) live with the voucher. `BEFORE UPDATE OR DELETE` trigger raises — Law 2 covers documents too.

### `voucher_entries` (immutable)
`id`, `voucher_id`, `line_no`, `ledger_id`, `dc CHAR(1) CHECK IN ('D','C')`, `amount NUMERIC(26,8) CHECK (amount > 0)` — the **base-currency** figure, `voucher_date` (denormalized, trigger-proven equal to the voucher's), `narration`, and the FX snapshot trio (ADR-007): `currency CHAR(3) → currencies`, `amount_fx NUMERIC(26,8)`, `fx_rate NUMERIC(30,10)`, all-or-none and `CHECK (amount = round(amount_fx * fx_rate, 8))` when present — the database proves the product for any writer. Index: `(ledger_id, voucher_date) INCLUDE (dc, amount)` — the covering index every balance/report query uses.

### `currencies` (issue #4, ADR-007)
`code CHAR(3) PK` (ISO 4217), `name`, `symbol`, `minor_unit` (0–8, display rounding), `is_system` (INR ships seeded), timestamps. Audited via the code-keyed variant of the master audit trigger.

### `fx_rates` (issue #4, ADR-007)
`id`, `from_ccy`/`to_ccy → currencies` (`CHECK` distinct), `rate NUMERIC(30,10) > 0`, `valid_on`, `source`, `created_at`; `UNIQUE (from_ccy, to_ccy, valid_on)`. Advisory reference data — postings snapshot the applied rate on the entry row. App role: SELECT/INSERT/UPDATE (no DELETE; corrections are updates, journaled).

### `periods` (issue #5, ADR-008)
`id`, `name UNIQUE`, `starts_on`, `ends_on` (`CHECK` ordered, gist `EXCLUDE` no-overlap), `status open|locked|closed` (transition-guarded trigger: open↔locked, locked→closed final, closed_by required), `locked_at`, `closed_at`, `closed_by`. A BEFORE INSERT trigger on `vouchers` takes `FOR SHARE` on the covering period and refuses locked/closed/uncovered dates once any period exists. Audited.

### `doc_counters`
`scope_key TEXT PK, last_value BIGINT`. Locked with `FOR UPDATE` inside each posting transaction — gapless by construction (ADR-002).

### `audit_log` (append-only)
Trigger journal for `groups`/`ledgers`/`voucher_types` changes: `occurred_at, actor, table_name, operation, record_id, row_data JSONB`. The actor is taken from the `app.actor` session GUC when present.

## The three invariants and where they live

1. **Debits = Credits** — `acc_check_voucher_balanced()` as a `DEFERRABLE INITIALLY DEFERRED` constraint trigger on `voucher_entries` (fires at commit; also requires ≥ 2 lines).
2. **Immutability** — `acc_forbid_mutation()` `BEFORE UPDATE OR DELETE` on `vouchers`, `voucher_entries`, `audit_log`; plus the application role is granted only `SELECT, INSERT`.
3. **Exact decimals** — `NUMERIC(26,8)` everywhere money appears; application layer additionally rejects scale > 8 before insert (PostgreSQL would otherwise silently round).

## Reporting functions (`rpt` schema)

| Function | Purpose |
|---|---|
| `rpt.trial_balance(p_as_of DATE)` | per-ledger debit/credit/closing + group rollups + grand totals |
| `rpt.group_rollups(p_as_of DATE)` | every group's subtree dr/cr in one closure join |
| `rpt.profit_and_loss(p_from, p_to DATE)` | trading (direct, `affects_gross`) and P&L (indirect) sections → gross & net profit |
| `rpt.balance_sheet(p_as_of DATE)` | assets / liabilities / equity sections + computed net-profit plug |

Call as raw SQL (`SELECT * FROM rpt.trial_balance('2026-11-06')`) or through the REST API (`GET /api/v1/reports/trial-balance?as_of=…`).

## Roles

`schema.sql` creates a `ledgerforge_app` role (no login) and grants it exactly what the engine needs: `SELECT, INSERT` on the immutable financial tables, full DML on masters/counters, `EXECUTE` on `rpt.*`. Defense in depth: even a SQL injection in the application cannot update or delete financial history.
