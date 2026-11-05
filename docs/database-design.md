# Database Design Reference

**Schema source of truth:** `db/schema.sql` (bootstrap DDL) · `db/functions.sql` (reporting functions) · `db/seed/chart_of_accounts.sql` (TallyPrime-style chart of accounts) · **Decisions:** ADR-001, ADR-002, `docs/analysis.md` §3–§4.

## Entity map

```
groups (tree: parent_id, truth)  ←── group_closure (read model, trigger-maintained)
  └── ledgers (unique name book-wide, like Tally)
voucher_types (RCT, PYT, JV, CN, SAL, PUR, CRN, DBN, OPEN …)
  └── vouchers (immutable: uuid, client_token, voucher_no, date, reversal_of)
        └── voucher_entries (immutable: line_no, ledger_id, dc D|C, amount NUMERIC(26,8))
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
`id`, `uuid`, `client_token UNIQUE` (idempotency), `voucher_type_id`, `voucher_no` (+ `UNIQUE (voucher_type_id, voucher_no)`), `sequence` (gapless int), `voucher_date`, `narration`, `reversal_of → vouchers.id`, `posted_at`. `BEFORE UPDATE OR DELETE` trigger raises.

### `voucher_entries` (immutable)
`id`, `voucher_id`, `line_no`, `ledger_id`, `dc CHAR(1) CHECK IN ('D','C')`, `amount NUMERIC(26,8) CHECK (amount > 0)`, `voucher_date` (denormalized, trigger-proven equal to the voucher's), `narration`. Index: `(ledger_id, voucher_date) INCLUDE (dc, amount)` — the covering index every balance/report query uses.

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
