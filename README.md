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
                    migrations/ — Alembic tree, 0001_baseline + drift guard (ADR-006)
src/ledgerforge/    engine (posting/reversal/opening) · api · auth (OIDC + roles) · reporting · plugins · contrib
frontend/           keyboard-kit — framework-agnostic command/keybinding/focus framework (TS)
                    web — the voucher-entry screen + command palette (React + Vite)
tests/              laws, hierarchy, immutability, reports, concurrency, plugins, auth, migrations
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
calls. Per-book permissions land together with the multi-book schema (roadmap).

## Database migrations

`db/schema.sql` remains the bootstrap path (`make init-db`, Docker) and stays
idempotent. Alembic ([ADR-006](docs/adr/006-migration-baseline.md)) versions
the same schema: revision `0001_baseline` embeds `schema.sql` +
`functions.sql` verbatim, and `tests/test_migrations.py` proves that
`alembic upgrade head` produces an identical structure — schemas, tables,
columns, indexes, constraints, triggers, functions, grants — via catalog
fingerprints. If the two ever drift apart, CI fails.

```bash
make migrate            # alembic upgrade head
make migrate-stamp      # adopt on a schema.sql-bootstrapped database
alembic current         # where this database stands
```

The bootstrap stamps `alembic_version` to head automatically, so databases
provisioned either way converge and a later `alembic upgrade head` is a
no-op, not a re-run. `python -m ledgerforge.migrations upgrade head` works
from any install. Pre-1.0, schema changes land in the SQL files first and
the baseline is regenerated (`make baseline-regen`); after 1.0 the baseline
freezes and changes ship as new revisions — see
[db/migrations/README.md](db/migrations/README.md).

## Status & roadmap

v0.3.0 — the accounting kernel, the voucher-entry screen **plus the command
palette with persisted key rebinding, and OIDC authentication with the role
model**. Next (see [docs/analysis.md §9](docs/analysis.md)):

- [x] React voucher grid + ledger picker on `keyboard-kit` ([frontend/web](frontend/web))
- [x] Command palette + user key-rebinding persistence (keyboard-ux §8 phase 3)
- [x] AuthN/AuthZ: OIDC bearer tokens + `reader`/`bookkeeper`/`admin` roles
- [x] Alembic migration baseline: `0001_baseline` + structural drift guard
- [ ] Multi-currency, period locking/FY close, multi-book
- [ ] Community plugin index, e-invoicing packs

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md), the ADRs, and [docs/analysis.md](docs/analysis.md) first — the financial laws are the contract; PRs that bend them will be (politely) rejected. Conventional commits, ruff-clean code, tests for every behavior change.

## License

Copyright (C) 2026 mir-ashiq and contributors.

This program is free software: you can redistribute it and/or modify it under the terms of the [GNU Affero General Public License v3.0](LICENSE) as published by the Free Software Foundation. Plugins integrating through the versioned plugin API are welcome under any license their authors choose.
