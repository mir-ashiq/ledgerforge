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

**Or by API (note: amounts are strings — Law 3):**

```bash
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

Or with the GST plugin doing the tax split for you — add `"meta": {"gst": {"rate": "18"}}` and post only the two base lines.

**Reports (raw SQL works too):**

```bash
curl -s 'localhost:8000/api/v1/reports/trial-balance?as_of=2026-11-06'
curl -s 'localhost:8000/api/v1/reports/profit-loss?from=2026-04-01&to=2027-03-31'
curl -s 'localhost:8000/api/v1/reports/balance-sheet?as_of=2026-11-06'
```

```sql
SELECT * FROM rpt.trial_balance('2026-11-06');
```

## What's inside

```
db/                 schema.sql · functions.sql · seed (TallyPrime's 28 predefined groups)
src/ledgerforge/    engine (posting/reversal/opening) · api · reporting · plugins · contrib
frontend/           keyboard-kit — framework-agnostic command/keybinding/focus framework (TS)
                    web — the voucher-entry screen on keyboard-kit (React + Vite)
tests/              laws, hierarchy, immutability, reports, concurrency, plugins
docs/               Phase-1 analysis, ADRs, keyboard-ux plan, plugin guide, db design
scripts/            benchmarks (bench_hierarchy.py, bench_posting.py), init_db.sh
```

## Performance (measured, reproducible)

`make bench` runs both benchmarks against your database. On the reference environment (PostgreSQL 16.2):

- **Hierarchy rollups:** closure-table and recursive-CTE rollups measure at parity on a 429-group / 3,402-ledger / 120k-entry book (47 ms vs 48 ms for the whole-tree rollup) — the closure model wins on SQL simplicity, integrity, and drill-down queries, not on raw speed. Full discussion: [docs/analysis.md §3.8](docs/analysis.md).
- **Concurrency:** 8 threads × 60 vouchers → 480/480 posted, strictly gapless numbering, zero deadlocks, ~174 vouchers/s on a single type's counter row, Trial Balance exact to the last sub-cent digit.

## Status & roadmap

v0.2.0 — the accounting kernel **plus the voucher-entry screen** (React +
keyboard-kit; `frontend/web`). Next (see [docs/analysis.md §9](docs/analysis.md)):

- [x] React voucher grid + ledger picker on `keyboard-kit` ([frontend/web](frontend/web))
- [ ] AuthN/AuthZ (OIDC) — **required before any production use**
- [ ] Multi-currency, period locking/FY close, multi-book
- [ ] Alembic migrations, community plugin index, e-invoicing packs

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md), the ADRs, and [docs/analysis.md](docs/analysis.md) first — the financial laws are the contract; PRs that bend them will be (politely) rejected. Conventional commits, ruff-clean code, tests for every behavior change.

## License

Copyright (C) 2026 mir-ashiq and contributors.

This program is free software: you can redistribute it and/or modify it under the terms of the [GNU Affero General Public License v3.0](LICENSE) as published by the Free Software Foundation. Plugins integrating through the versioned plugin API are welcome under any license their authors choose.
