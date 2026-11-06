# LedgerForge

**Community-driven, keyboard-first business management with an append-only, exact-decimal double-entry engine — inspired by TallyPrime.**

[![CI](https://github.com/mir-ashiq/ledgerforge/actions/workflows/ci.yml/badge.svg)](https://github.com/mir-ashiq/ledgerforge/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org)
[![PostgreSQL 16+](https://img.shields.io/badge/PostgreSQL-16%2B-336791.svg)](https://www.postgresql.org)

> Read the Phase-1 research that produced this architecture: **[docs/analysis.md](docs/analysis.md)** — hierarchy models, concurrency strategies, plugin architecture, keyboard-first UX, and the engineering controls for the three financial laws.

## Why LedgerForge

TallyPrime proved that accounting software can be *faster than the accountant*. LedgerForge brings that philosophy to an open, community-owned stack:

- **A precise engine, not a CRUD app.** Postings run through one ACID transaction guarded by three non-negotiable laws (below), enforced at the application layer *and* the database layer.
- **Tally-grade keyboard UX.** `Enter` forward, `Esc` back, `Alt+C` create-ledger-here, F-key voucher switching — a full command/keybinding/focus framework, not an afterthought. See [docs/keyboard-ux.md](docs/keyboard-ux.md).
- **A plugin commons.** Tax packs (GST/VAT/sales tax), dashboards and vertical workflows build against a versioned plugin API and never touch core. See [docs/plugin-guide.md](docs/plugin-guide.md).
- **Plain, reviewable SQL.** The chart of accounts uses a closure-table hierarchy; reports are database functions (`rpt.trial_balance`, `rpt.profit_and_loss`, `rpt.balance_sheet`) callable as raw SQL.

## The three financial laws

| Law | Enforcement | Proof |
|---|---|---|
| **1. Debits = Credits, exactly** | `Decimal` validation before insert + a `DEFERRABLE INITIALLY DEFERRED` constraint trigger re-verifies `SUM(D) = SUM(C)` at commit | `tests/test_double_entry.py` |
| **2. History is immutable** | No update/delete code paths; `BEFORE UPDATE OR DELETE` triggers raise; the app DB role has only `SELECT, INSERT`; corrections are linked reversal vouchers | `tests/test_immutability.py` |
| **3. Exact decimals only** | `NUMERIC(26,8)` columns, `decimal.Decimal` end-to-end, and JSON amounts must be **strings** — floats are rejected with HTTP 422 | `tests/test_decimal_law.py` |

## Architecture at a glance

```
                    ┌──────────────────────────────────────────┐
                    │        Keyboard-first frontend           │
                    │  command registry · keymap · focus stack │
                    │        (frontend/keyboard-kit)           │
                    └────────────────────┬─────────────────────┘
                                         │ REST (amounts as strings)
                    ┌────────────────────▼─────────────────────┐
   plugins ────────▶│            FastAPI engine                │──── domain events
 (transformers,     │  post · reverse · opening · masters      │     (after commit)
  validators,       │  ACID: counter lock → insert → commit    │
  event hooks)      └────────────────────┬─────────────────────┘
                                         │ SQLAlchemy (psycopg 3)
                    ┌────────────────────▼─────────────────────┐
                    │             PostgreSQL 16+               │
                    │ groups ── group_closure (triggers)       │
                    │ vouchers ── voucher_entries (append-only)│
                    │ currencies/fx_rates · periods (FY close) │
                    │ doc_counters (gapless numbering)         │
                    │ rpt.* reporting functions (exact NUMERIC)│
                    └──────────────────────────────────────────┘
```

Decisions are recorded as ADRs in [docs/adr/](docs/adr/).

## Quickstart

**Docker:**

```bash
docker compose up -d        # PostgreSQL 17 + API on :8000 + voucher UI on :5173
```

**Local development:**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/ledgerforge
make init-db                # apply db/schema.sql + functions + Tally-style seed
make dev                    # uvicorn ledgerforge.main:app --reload
make dev-ui                 # voucher-entry UI on http://localhost:5173 (keyboard-first)
```

**Post your first voucher — with the keyboard (the Tally contract):**

Start `make dev-ui`, then: type `Cash` → `Enter` · `1180` → `Enter` → `Enter` ·
type `Local Sales` → `Enter` → `Enter` → `1000` → `Enter` … `Ctrl+A` accepts.
`Alt+C` creates a missing ledger mid-flight, `F2` changes the date,
`F4`–`F9` switch the voucher type, `Ctrl+K` shows every binding.

**Or by API (note: amounts are strings — Law 3).** The seed ships only the
TallyPrime defaults (`Cash`, `Profit & Loss A/c` and the FX gain/loss ledgers),
so create the ledgers you need first:

```bash
curl -s -X POST localhost:8000/api/v1/ledgers -H 'Content-Type: application/json' \
  -d '{"name": "Local Sales", "group_name": "Sales Accounts"}'
curl -s -X POST localhost:8000/api/v1/ledgers -H 'Content-Type: application/json' \
  -d '{"name": "CGST Payable", "group_name": "Duties & Taxes"}'
curl -s -X POST localhost:8000/api/v1/ledgers -H 'Content-Type: application/json' \
  -d '{"name": "SGST Payable", "group_name": "Duties & Taxes"}'

curl -s -X POST localhost:8000/api/v1/vouchers -H 'Content-Type: application/json' -d '{
  "voucher_type": "SAL",
  "voucher_date": "2026-11-06",
  "client_token": "9af1c412-6a1f-4b0e-9b76-2c2a5b46d1aa",
  "entries": [
    {"ledger_name": "Cash",        "dc": "D", "amount": "1180.00"},
    {"ledger_name": "Local Sales", "dc": "C", "amount": "1000.00"},
    {"ledger_name": "CGST Payable","dc": "C", "amount": "90.00"},
    {"ledger_name": "SGST Payable","dc": "C", "amount": "90.00"}
  ]
}'
```

Or with the GST plugin doing the tax split for you — add `"meta": {"gst": {"rate": "18"}}`
and post only the two base lines; the plugin creates the CGST/SGST ledgers
under *Duties & Taxes* on first use.

**Reports (raw SQL works too):**

```bash
curl -s 'localhost:8000/api/v1/reports/trial-balance?as_of=2026-11-06'
curl -s 'localhost:8000/api/v1/reports/profit-loss?from=2026-04-01&to=2027-03-31'
curl -s 'localhost:8000/api/v1/reports/balance-sheet?as_of=2026-11-06'
```

```sql
SELECT * FROM rpt.trial_balance(1, '2026-11-06');  -- book id first (ADR-009)
```

## Multi-currency (issue #4)

One base currency per book (`books.base_currency`, seeded INR); every entry
posts a **base-currency amount** — that is what balances and what the reports
aggregate. A foreign-currency line snapshots the trio on the immutable entry:
`currency`, `amount_fx`, `fx_rate` (base units per foreign unit,
`NUMERIC(30,10)`), and the database itself enforces
`amount = round(amount_fx × fx_rate, 8)` for **any** writer. Reference rates
live in `fx_rates` (dated, correctable, never deleted); realized/unrealized
exchange gain/loss ship as seeded system ledgers.

```bash
curl -s -X POST localhost:8000/api/v1/vouchers -H 'Content-Type: application/json' -d '{
  "voucher_type": "JV",
  "voucher_date": "2026-11-06",
  "entries": [
    {"ledger_name": "USD Bank", "dc": "D", "amount": "8350.50000000",
     "currency": "USD", "amount_fx": "100.00", "fx_rate": "83.5050000000"},
    {"ledger_name": "Cash", "dc": "C", "amount": "8350.50000000"}
  ]
}'
```

Design: [ADR-007](docs/adr/007-multi-currency.md).

## Period locking & the financial-year close (issue #5)

Periods are named, non-overlapping date ranges with a state machine
`open → locked → closed`. Once any period exists, a voucher date must fall in
an **open** one — enforced by a row-locking trigger that serializes postings
against concurrent locks. Closing a year is two-step (lock, then close): the
close posts `rpt.fy_close`'s sweep — zeroing every income/expense ledger into
*Profit & Loss A/c* — as an `FYC` voucher dated the first day of the **next**
period, so both years' reports stay correct. `closed` is final, and every
lock/close is audited with its actor.

```bash
curl -s localhost:8000/api/v1/periods
curl -s localhost:8000/api/v1/periods/1/close-preview   # exactly what close posts
curl -s -X POST localhost:8000/api/v1/periods/1/lock
curl -s -X POST localhost:8000/api/v1/periods/1/close
```

Design: [ADR-008](docs/adr/008-period-locking.md).

## Multi-book (roadmap §9, ADR-009)

One database, many companies — TallyPrime's "select company", made a header.
Every book is a legal entity's complete accounting context: its own chart of
accounts, voucher types, numbering (each book's `SAL-2026-27-00001` starts at
1), periods, base currency and fiscal-year start. `POST /api/v1/books`
creates a book **and seeds its TallyPrime defaults** (28 groups, system
ledgers, 10 voucher types) in one action; books are strangers — cross-book
references are 404s, and the schema itself refuses cross-book hierarchy and
entry/voucher book disagreement.

Clients send `X-Book-Id: <id>` on every request. With exactly one active book
the header is optional — every single-book-era client keeps working unchanged;
with several, omitting it is a 400 that lists the books to pick from.

```bash
curl -s -X POST localhost:8000/api/v1/books -H 'Content-Type: application/json' \
  -d '{"name": "Acme Traders", "base_currency": "INR"}'   # created + seeded
curl -s localhost:8000/api/v1/books                          # the picker list
curl -s localhost:8000/api/v1/ledgers -H 'X-Book-Id: 2'     # select company
```

`base_currency` and `fy_start_month` are immutable once the book has vouchers
(amounts are stored in the base currency — changing it would restate history,
which Law 2 forbids). Per-book permissions ride on the token's `books` claim
(`["*"]` or a list of book ids; `admin` always passes; no claim = every book).

Design: [ADR-009](docs/adr/009-multi-book.md).

## Invoicing (roadmap §9, ADR-010)

The first complete business document shipped **as a plugin, not in core**
(`ledgerforge/contrib/invoicing.py`, loaded at boot). Describe the commercial
document; the pack derives the accounting and the voucher *is* the invoice —
items, rates and computed totals persist with it (`vouchers.meta`, ADR-010)
and come back from `GET /vouchers/{id}`, immutable like the amounts.

```bash
# A 10×100 GST-18% sale + 250 exempt service, due in 30 days
curl -s -X POST localhost:8000/api/v1/vouchers -H 'Content-Type: application/json' -d '{
  "voucher_type": "SAL", "voucher_date": "2026-11-06",
  "entries": [{"ledger_name": "Sales", "dc": "C", "amount": "0.01"}],
  "meta": {"invoice": {
    "customer": "Acme Traders",
    "items": [
      {"description": "Widgets", "qty": "10", "rate": "100.00", "tax_rate": "18"},
      {"description": "Installation", "qty": "1", "rate": "250.00", "tax_rate": "0"}
    ],
    "due_days": 30
  }}
}'
# → Dr Acme Traders 1430.00 (auto-created under Sundry Debtors)
#   Cr Sales 1000.00 + Cr Sales 250.00 (per item, qty @ rate in narrations)
#   Cr CGST Payable 90.00 + Cr SGST Payable 90.00 (under Duties & Taxes)
#   meta.invoice.computed: subtotal 1250.00, tax 180.00, grand 1430.00, due 2026-12-06

# Settle it
# meta: {"receipt": {"customer": "Acme Traders", "amount": "1430.00",
#                    "bank_ledger": "HDFC Bank", "reference": "NEFT-42"}}
```

Guard rails (all 422): tax rates outside the GST slabs `0/5/12/18/28`,
non-positive quantities, double-tax (`gst` + `invoice`), ambiguous documents
(`invoice` + `receipt`), and future-dated documents (policy-overridable).
Idempotent replay does not re-issue a document; reversals are new documents
with empty meta; auto-created masters are book-scoped (ADR-009) — the same
customer in two books is two ledgers.

Design: [ADR-010](docs/adr/010-voucher-meta.md),
[plugin guide](docs/plugin-guide.md).

## Inventory (roadmap §9, ADR-011)

The second business pack — and the first roadmap feature shipped with
**zero schema change**. Stock positions are *derived*: the pack folds every
posted `meta.stock` document (goods-in, goods-out, transfer, recount) the
way reports fold entries, valuing issues at a **moving weighted average**
whose rounding closes exactly — issuing the last unit costs precisely the
remaining value. Plugin API 0.3 grew the two primitives this needs:
`Resolver.prior_documents()` (document-store reads, reversals included) and
`Resolver.lock()` (transaction-scoped advisory locks that serialize
same-item movements like the engine's numbering counters).

```bash
# Buy 10 Widgets @ 100 — perpetual purchase: Dr stock, Cr supplier
curl -s -X POST localhost:8000/api/v1/vouchers -H 'Content-Type: application/json' -d '{
  "voucher_type": "PUR", "voucher_date": "2026-11-06",
  "entries": [{"ledger_name": "Cash", "dc": "D", "amount": "0.01"}],
  "meta": {"stock": {"movement": "in", "party": "Omega Supplies",
    "items": [{"item": "Widgets", "qty": "10", "rate": "100.00"}]}}
}'
# → Cr Omega Supplies (Sundry Creditors) 1000.00 · Dr Widgets (Stock-in-Hand) 1000.00
#   computed: avg_cost_after 100.0000

# Sell 3 @ 150 — the full perpetual sale with its margin annotated
curl -s -X POST localhost:8000/api/v1/vouchers -H 'Content-Type: application/json' -d '{
  "voucher_type": "SAL", "voucher_date": "2026-11-06",
  "entries": [{"ledger_name": "Cash", "dc": "D", "amount": "0.01"}],
  "meta": {"stock": {"movement": "out", "party": "Acme Traders",
    "items": [{"item": "Widgets", "qty": "3", "rate": "150.00"}]}}
}'
# → Dr Acme Traders 450.00 · Cr Sales 450.00 · Dr COGS 300.00 · Cr Widgets 300.00
#   computed: gross_margin 150.00
```

Transfers move value between godown ledgers at average cost; recounts
adjust the book to a physical count against *Stock Adjustment*. Reversals
net to zero on stock — reversing a goods-out restores it, exactly as the
mirrored entries restore the ledgers. Guards (all 422): insufficient stock
(partial overdrafts policy-overridable; an empty position never issues),
mode conflicts with the invoicing/GST packs, FX lines, future dates, and
items that would silently reuse an unrelated ledger.

Design: [ADR-011](docs/adr/011-inventory-pack.md),
[plugin guide](docs/plugin-guide.md).

## Payroll (roadmap §9, ADR-012)

The third business pack — the second in a row with **zero schema change**,
and the last planned roadmap feature. Salaries are documents like invoices
and stock: a **run** accrues a period's payroll (one voucher, the
TallyPrime payroll journal shape), a **payout** settles it. The derived
state is a deterministic fold over posted documents, never stored:
**year-to-date windows by the book's fiscal year** (plugin API 0.4 grew
the one fact that needs: `Resolver.fy_start_month`), while **payable is
an all-time balance** — an accumulator windows, a balance never expires.

```bash
# October payroll: earnings, withholdings and the employer's contribution
curl -s -X POST localhost:8000/api/v1/vouchers -H 'Content-Type: application/json' -d '{
  "voucher_type": "JV", "voucher_date": "2026-11-07",
  "entries": [{"ledger_name": "Cash", "dc": "C", "amount": "0.01"}],
  "meta": {"payroll": {"mode": "run", "period": "2026-10",
    "employees": [{"employee": "Asha Verma", "components": [
      {"name": "Basic Salary", "amount": "50000.00", "kind": "earning"},
      {"name": "HRA", "amount": "20000.00", "kind": "earning"},
      {"name": "PF Payable", "amount": "1800.00", "kind": "withheld"},
      {"name": "Employer PF", "amount": "1800.00", "kind": "employer",
       "liability": "PF Payable"}
    ]}]}}
}'
# → Dr Basic Salary 50000 · Dr HRA 20000 (Indirect Expenses)
#   Dr Employer PF 1800 · Cr PF Payable 1800 + 1800 (Duties & Taxes)
#   Cr Asha Verma 68200 (Provisions — net pay)
#   computed: gross 70000 / withheld 1800 / net 68200 + fiscal-year YTD

# Settle it
# meta: {"payroll": {"mode": "payout", "employee": "Asha Verma",
#                    "amount": "68200.00", "bank": "HDFC Bank"}}
```

One run per employee per period (advisory-lock-serialized, so concurrent
duplicates cannot both post; policy-overridable for off-cycle
corrections). Reversals net to zero on YTD and payable. `ytd()` and
`payable()` replay the folds for any later consumer. Guards (all 422):
malformed periods, duplicate employees/components, withheld exceeding
gross, employer contributions without a liability, periods that have not
started, mode conflicts with the other packs, FX lines, and employee
names that would silently reuse an unrelated ledger.

Design: [ADR-012](docs/adr/012-payroll-pack.md),
[plugin guide](docs/plugin-guide.md).

## What's inside

```
db/                 schema.sql · functions.sql · migrations/ — Alembic tree:
                    frozen 0001 baseline + drift guard, 0002 multi-currency,
                    0003 period locking, 0004 multi-book, 0005 voucher meta
                    (ADR-006/007/008/009/010).
                    The chart-of-accounts seed is code: ledgerforge/seeding.py
                    (one definition for bootstrap and POST /books — ADR-009).
src/ledgerforge/    engine (posting/reversal/opening) · api (+books) · auth (OIDC
                    + roles + per-book claims) · reporting · seeding · plugins ·
                    contrib (example_gst + the invoicing, inventory and payroll packs)
frontend/           keyboard-kit — framework-agnostic command/keybinding/focus framework (TS)
                    web — the voucher-entry screen + command palette (React + Vite)
tests/              laws, hierarchy, immutability, reports, concurrency, plugins, auth,
                    migrations, multi-currency, period locking, multi-book, invoicing,
                    inventory, payroll, deep audit (adversarial: races, rounding
                    boundaries, gate bypasses)
docs/               Phase-1 analysis, ADRs, keyboard-ux plan, plugin guide, db design
scripts/            benchmarks (bench_hierarchy.py, bench_posting.py), init_db.sh, regen_baseline.py
```

## Performance (measured, reproducible)

`make bench` runs both benchmarks against your database. On the reference environment (PostgreSQL 16.2):

- **Hierarchy rollups:** closure-table and recursive-CTE rollups measure at parity on a 429-group / 3,402-ledger / 120k-entry book (47 ms vs 48 ms for the whole-tree rollup) — the closure model wins on SQL simplicity, integrity, and drill-down queries, not on raw speed. Full discussion: [docs/analysis.md §3.8](docs/analysis.md).
- **Concurrency:** 8 threads × 60 vouchers → 480/480 posted, strictly gapless numbering, zero deadlocks, ~174 vouchers/s on a single type's counter row, Trial Balance exact to the last sub-cent digit.

## Authentication

The API ships with two modes, picked by `LF_AUTH_MODE`:

- **`disabled`** (default) — development only: every caller is an anonymous
  admin. A loud warning is logged at startup; never expose this to untrusted
  networks.
- **`oidc`** — production: requests carry `Authorization: Bearer <jwt>`, verified
  against your OIDC provider's JWKS (Keycloak, Auth0, …). Roles come from the
  token (the `roles` claim, or Keycloak's `realm_access.roles`):

| Role | Powers |
|---|---|
| `reader` | GET masters, vouchers, reports |
| `bookkeeper` | reader + create groups/ledgers, post & reverse vouchers |
| `admin` | bookkeeper + everything else |

```bash
LF_AUTH_MODE=oidc \
LF_OIDC_ISSUER=https://keycloak.example.test/realms/ledgerforge \
LF_OIDC_AUDIENCE=ledgerforge \
LF_DATABASE_URL=postgresql+psycopg://... \
uvicorn ledgerforge.main:app
```

`GET /api/v1/auth/me` answers with the live subject, roles, and mode. JWKS keys
are cached (15 min) and refreshed on unknown `kid` rotation. With auth on, the
API is same-origin only — set `LF_CORS_ORIGINS` if you really need cross-origin
calls. Per-book permissions (ADR-009) ride on the token's `books` claim:
`["*"]` or a list of book ids the principal may touch at its global role;
`admin` always passes; a token with no `books` claim sees every book.

## Database migrations

`db/schema.sql` remains the bootstrap path (`make init-db`, Docker) and stays
idempotent. Alembic ([ADR-006](docs/adr/006-migration-baseline.md)) versions
the same schema: the frozen revision `0001_baseline` embeds the schema as it
stood at the freeze (preserved under `db/migrations/baseline/`), forward
revisions `0002_multi_currency`, `0003_period_locking` and `0004_multi_book`
carry the rest, and
`tests/test_migrations.py` proves that `alembic upgrade head` produces an
identical structure to the bootstrap — schemas, tables, columns, indexes,
constraints, triggers, functions, grants — via catalog fingerprints. If the
two ever drift apart, CI fails.

```bash
make migrate            # alembic upgrade head
make migrate-stamp      # adopt on a schema.sql-bootstrapped database
alembic current         # where this database stands
```

The bootstrap stamps `alembic_version` to head automatically, so databases
provisioned either way converge and a later `alembic upgrade head` is a
no-op, not a re-run. `python -m ledgerforge.migrations upgrade head` works
from any install. The baseline is frozen (ADR-006 amendment): schema changes
ship as new revisions that also update the SQL files in the same PR — see
[db/migrations/README.md](db/migrations/README.md).

## Status & roadmap

v0.8.0 — the accounting kernel, the voucher-entry screen, the command palette
with persisted key rebinding, OIDC authentication, multi-currency books,
period locking with the financial-year close, multi-book (v0.5), invoicing
as the first plugin pack (v0.6), inventory as the second — zero schema
change (plugin API 0.3) — **and payroll as the third: runs and payouts as
documents, fiscal-year YTD folds, zero schema change (plugin API 0.4:
Resolver.fy_start_month)**. The planned roadmap is now fully landed; what
remains is deliberately conditional (see
[docs/analysis.md §9](docs/analysis.md)):

- [x] React voucher grid + ledger picker on `keyboard-kit` ([frontend/web](frontend/web))
- [x] Command palette + user key-rebinding persistence (keyboard-ux §8 phase 3)
- [x] AuthN/AuthZ: OIDC bearer tokens + `reader`/`bookkeeper`/`admin` roles
- [x] Alembic migration baseline: `0001_baseline` + structural drift guard
- [x] Multi-currency: base-currency posting rules, FX snapshots, gain/loss ledgers
- [x] Period locking + FY close: the posting gate, the P&L sweep, finality
- [x] Multi-book: one database, many companies (ADR-009)
- [x] Invoicing: sales invoices + AR receipts as a plugin pack (ADR-010)
- [x] Inventory: stock documents, weighted-average valuation, derived
      positions as a plugin pack (ADR-011)
- [x] Payroll: runs/payouts, fiscal-year YTD and all-time payable folds
      as a plugin pack (ADR-012)
- [ ] Community plugin index, signed plugin trust model
- [ ] Report scale-out (partitioning + BRIN) — only when books pass
      millions of entries

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md), the ADRs, and [docs/analysis.md](docs/analysis.md) first — the financial laws are the contract; PRs that bend them will be (politely) rejected. Conventional commits, ruff-clean code, tests for every behavior change.

## License

Copyright (C) 2026 mir-ashiq and contributors.

This program is free software: you can redistribute it and/or modify it under the terms of the [GNU Affero General Public License v3.0](LICENSE) as published by the Free Software Foundation. Plugins integrating through the versioned plugin API are welcome under any license their authors choose.
