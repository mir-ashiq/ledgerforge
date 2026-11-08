# LedgerForge

**Community-driven, keyboard-first business management with an append-only, exact-decimal double-entry engine — inspired by TallyPrime.**

[![CI](https://github.com/mir-ashiq/ledgerforge/actions/workflows/ci.yml/badge.svg)](https://github.com/mir-ashiq/ledgerforge/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/mir-ashiq/ledgerforge)](https://github.com/mir-ashiq/ledgerforge/releases/latest)
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

**Windows desktop (zero-config):**

Download `LedgerForge-<version>-win64.zip` from
[releases](https://github.com/mir-ashiq/ledgerforge/releases/latest), extract,
and double-click `LedgerForge.exe`. **A native window opens** — menus,
tables and file dialogs of its own, no browser anywhere. An embedded
PostgreSQL and the whole engine run invisibly on your machine — nothing to
install, nothing exposed to the network. Your books live in
`%LOCALAPPDATA%\LedgerForge`. More:
[the native application](#the-native-application-a-real-windows-program-adr-022).

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

## Inventory depth: batches, serials, expiry, godowns, re-order & manufacturing (v0.24, ADR-027)

Phase 6 grows the pack from quantities-and-averages to the depth
TallyPrime users call real inventory — **valuation stays the moving
weighted average**; everything below is a quantity sub-ledger of the same
position, replayed by the same fold:

- **Batches & expiry** on every stock line: the first receipt fixes a
  batch's expiry (later receipts agree or name a new batch); issues name
  their batch or take **FEFO** — the earliest-expiry batches first, ties
  by batch name, the allocation recorded in the document's `computed`.
  The expiry-ageing report buckets every dated batch (expired, ≤30, ≤60,
  ≤90, >90 days) with quantity and value-at-average.
- **Serial numbers** are identity: one per unit, duplicates a 422, issues
  must name serials that are in stock *at that godown, in that batch*.
- **Godowns are a first-class dimension** — a location key on movements
  and positions, not another ledger: `(item, godown) → qty` in the fold,
  and an issue cannot take what the location does not hold (the ADR-011
  limitation, closed). Transfers carry `godown → to_godown` alongside
  their value pair.
- **Stock-item masters** (migration `0009_stock_depth`, the ADR-025
  discipline): tracking modes (TallyPrime's "maintain in batches /
  serial numbers" toggles — enforced when declared, optional otherwise),
  re-order levels with the **shortfall report**, and per-party
  date-effective **price lists** whose effective rate is annotated on
  sales (suggestions never commit — the selling rate stays the
  operator's).
- **The manufacturing journal** (`movement: "manufacture"`): consumes
  components at their moving averages, adds the operator's charge legs
  (wages, power, freight — anything outside Stock-in-Hand), and produces
  the output at the rolled-up cost — `output value = Σ components +
  Σ charges`, one exact amount, no rounding residue. A named **BOM**
  validates the recipe (exact component set, quantities covering the
  per-output shares); without one, the explicit journal stands alone.
  Reversal nets the whole journal — components restored, output gone.

```bash
# Produce 2 Desks from 6 Planks and 40 Screws + 450 of wages
curl -s -X POST localhost:8000/api/v1/vouchers -H 'Content-Type: application/json' -d '{
  "voucher_type": "JV", "voucher_date": "2026-11-08",
  "entries": [{"ledger_name": "Cash", "dc": "D", "amount": "0.01"}],
  "meta": {"stock": {"movement": "manufacture",
    "components": [{"item": "Planks", "qty": "6"}, {"item": "Screws", "qty": "40"}],
    "output": {"item": "Desks", "qty": "2", "batch": "DSK-1"},
    "charges": [{"ledger": "Production Wages", "amount": "450.00"}]}}
}'
# → Dr Desks 1090.00 · Cr Planks 600.00 · Cr Screws 40.00 · Cr Production Wages 450.00
```

Five pack reports over the same fold (PDF, CSV and JSON — one truth,
every surface): `stock-position` (the depth browser), `stock-godown`,
`stock-batch`, `stock-expiry`, `stock-shortfall`. The native Inventory
page (`Alt+7`) and the web screen (`Alt+V`) browse them all. Every stock
document prints as the physical document it is — receipt, issue note,
stock transfer, physical count, manufacturing journal.

Design: [ADR-027](docs/adr/027-inventory-depth.md) (extends
[ADR-011](docs/adr/011-inventory-pack.md)),
[plugin guide](docs/plugin-guide.md).

## GST compliance depth: the 2A/2B reconciliation, GSTR-3B/9, e-way bills (v0.25, ADR-028)

The tax month, closed end to end. Import the portal's GSTR-2A/2B download
(the operator adapter's `lf-gst-2b/1` JSON — all-or-nothing, idempotent;
a changed re-download lands as a new snapshot, never an edit), and the
reconciliation prices every document: matched, mismatched (the reason
named), missing in books, missing in portal — the ITC both sides claim,
exact to the cent.

- **Credit & debit notes** are document modes now: a credit note walks the
  sale backwards on the same ledgers and nets every outward figure; a
  debit note reverses the input credit. GSTR-1 names every document
  INV/CN/DN.
- **Multi-GSTIN**: `supplier_gstin` on the voucher, `?gstin=` on every
  statement — one book, several registrations, each statement scoped.
- **GSTR-3B** (outward, eligible ITC, net payable — the ledger's numbers
  or no numbers) and **GSTR-9** (twelve monthly folds + the annual
  totals, the portal-only tables honestly left to the portal).
- **The e-way-bill outbox**: threshold-crossing documents queue a
  movement envelope after commit (₹50,000 default, Part-B facts riding),
  `lf-ewb-bridge` ships it to your adapter — the IRN discipline, applied
  to movement.

## Payroll (roadmap §9, ADR-012 + the depth, ADR-029)

The third business pack — salaries are documents like invoices and stock:
a **run** accrues a period's payroll (one voucher, the TallyPrime payroll
journal shape), a **payout** settles it. The derived state is a
deterministic fold over posted documents, never stored: **year-to-date
windows by the book's fiscal year** (plugin API 0.4 grew the one fact
that needs: `Resolver.fy_start_month`), while **payable is an all-time
balance** — an accumulator windows, a balance never expires.

Phase 8 (ADR-029) deepened it to TallyPrime's answer for the shop floor:

- **Attendance registers as facts** — `POST /api/v1/payroll/attendance`
  keys one period's register (present / absent / half-day / leave-with-pay
  per employee; a half-day is 0.5, the calendar is the honesty ceiling).
  Idempotent by content fingerprint; a corrected register is a new
  snapshot (latest-batch-wins, append-only history — the GSTR-2B
  discipline restated for the shop floor). An earning component may carry
  `per_day` instead of `amount`: the run derives it — `per_day × payable
  days` of the live register (plugin API 0.14:
  `Resolver.attendance_lines`) — and records the derivation beside the
  amount. No register, no derivation: a 422, never a guess.
- **Loans & advances** — `mode: "loan"` issues Dr the employee's advance
  (`"{employee} Advance"` under *Loans & Advances (Asset)*) / Cr the bank,
  with the exact-Decimal annuity EMI schedule computed into the document
  (rate 0 = interest-free; the final row absorbs its own rounding). The
  schedule is advisory: recovery is the run's new `recovery` component
  kind — it reduces net pay and credits the advance ledger, guarded by
  the outstanding fold (named loans adopt their scheduled EMI
  automatically). `loan_position()` replays the fold;
  `payroll-loans` renders it (JSON/CSV/PDF).
- **The full & final settlement** — `mode: "settlement"` books leave
  encashment (`days × per_day`), dues, withholdings and recoveries in one
  voucher, paying direct from the bank or accruing to the payable. After
  it the employee is **relieved**: runs, loans and further settlements
  refuse (`policy:allow_post_settlement` for the genuinely-late
  correction); payouts stay legal. A reversal reopens the relationship.
- **The payslip outbox** — every run appends one
  `lf-payroll-payslip/1` envelope per employee to `LF_PAYROLL_OUTBOX`;
  `lf-payslip-bridge` renders the PDF batch through the same layout
  engine as Ctrl+P and, only when the operator configures SMTP
  (`LF_PAYROLL_SMTP_*`), emails each payslip to the address the run entry
  carried. **Core never sends mail** — the bridge is the only mailer.

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

# Attendance-derived pay: per_day instead of amount
#   {"name": "Basic Salary", "kind": "earning", "per_day": "1500.00"}
#   → amount = 1500 × payable days of the period's live register

# A staff loan and its automatic recovery
# meta: {"payroll": {"mode": "loan", "employee": "Asha Verma",
#                    "principal": "100000.00", "rate": "9.0",
#                    "months": 20, "bank": "HDFC Bank"}}
# …then, in the next run's components:
#   {"name": "Loan Recovery", "kind": "recovery", "loan": "JV-0042"}
#   → adopts that loan's scheduled EMI for the period

# Settle it
# meta: {"payroll": {"mode": "payout", "employee": "Asha Verma",
#                    "amount": "68200.00", "bank": "HDFC Bank"}}
```

One run per employee per period (advisory-lock-serialized, so concurrent
duplicates cannot both post; policy-overridable for off-cycle
corrections). Reversals net to zero on YTD, payable and the loan
outstanding. `ytd()`, `payable()`, `loan_position()` and `is_settled()`
replay the folds for any later consumer. Guards (all 422): malformed
periods, duplicate employees/components, withheld and recoveries
exceeding gross, employer contributions without a liability, recoveries
crossing the loan outstanding, attendance-derived components without
their register, periods that have not started, mode conflicts with the
other packs, FX lines, and employee names that would silently reuse an
unrelated ledger.

Design: [ADR-012](docs/adr/012-payroll-pack.md),
[ADR-029](docs/adr/029-payroll-depth.md),
[plugin guide](docs/plugin-guide.md).

## Data exchange & the Tally migration path (v0.27, ADR-030)

**The adoption lever: you can move off TallyPrime onto LedgerForge.**

- **CSV/Excel import** — `POST /api/v1/dataexchange/imports`, one front
  door, five kinds: masters (groups & ledgers), opening balances, vouchers
  (with FX lines and cost-aware columns), stock item masters, and bank
  statements (ADR-026's own machinery). Every import is previewable
  (`dry_run` runs the whole pipeline and discards it), validated with the
  row number named, all-or-nothing (one bad row rolls back the file), and
  idempotent — masters create-if-absent by name, vouchers replay by
  content-addressed client token. The engine never grows a bulk lane: an
  imported voucher walks the exact path a keyboard voucher walks.
- **Export everywhere** — `?format=csv|xlsx` on every report and list
  surface (the catalogue: `GET /api/v1/dataexchange/exports`); pack
  reports gain `xlsx` beside their CSV. The digits are the engine's own
  exact decimals — Law 3 through the spreadsheet.
- **The TallyPrime XML import** — `POST /api/v1/dataexchange/tally`:
  masters, opening balances (ONE Opening Journal; `balance_to` absorbs a
  residual explicitly), vouchers with FX, stock item masters — and the
  **verification report**: the source trial balance folded straight from
  the XML against the imported one folded from what landed, per ledger,
  difference column, every skip named. Unmapped voucher types are skipped
  and reported, never guessed; unknown ledgers land under *Suspense A/c*
  and are reported.
- **The analyst seat** — the `views` schema: seven read-only, flat,
  cross-book SQL projections with a `ledgerforge_readonly` role that
  reads the views and nothing else. `POST /api/v1/ops/analyst-access`
  mints/rotates the `lf_analyst` ODBC login (one-time password;
  PostgreSQL keeps the SCRAM verifier); amounts stay NUMERIC end to end.
- **Personal API tokens** — `POST /api/v1/tokens` (admin): long-lived,
  scoped, revocable credentials (`lft_…`, shown once, SHA-256 at rest)
  for integrations without an IdP — role ceiling, books scope, expiry,
  immediate revocation, `last_used_at` observability.

## Bill-wise & outstanding management (roadmap Phase 2, ADR-023)

TallyPrime's party ledgers carry **bill-wise details** — every invoice opens
a named bill, every receipt settles specific bills, and the receivables /
payables position is bill-level. LedgerForge's answer is the bills pack
(`ledgerforge/contrib/bills.py`), the fourth business pack, landing with
**zero schema change** on the pattern proven by inventory and payroll:
documents on `vouchers.meta`, outstanding positions as a deterministic fold.

Allocations ride any voucher that moves a party ledger — a plain receipt, a
`meta.invoice` document, an opening journal — as `meta.bills` refs, each a
dr/cr allocation exactly like a ledger line (TallyPrime's bill-wise columns):

```json
{"bills": {"party": "Acme Traders", "refs": [
  {"kind": "new",     "ref": "INV-42", "dr": "1180.00", "due_days": 30},
  {"kind": "against", "ref": "INV-42", "cr": "500.00"},
  {"kind": "on_account",             "cr": "50.00"}
]}}
```

The pack never builds allocation lines (the existing packs and plain entry
own the accounting); it validates a Law-1-shaped conservation invariant
*after all transformers* — **Σ ref dr − Σ ref cr == the party's net movement
on the voucher** — from which an induction gives the master theorem the tests
assert against the trial balance: **Σ open bills + on-account == the party's
ledger balance**, at every point in the book's history. Settlement is
strictly toward zero (over-settlement is refused — the excess belongs in
`on_account`, the TallyPrime flow); `against` must name a bill a prior
document opened; duplicate open refs collide; payroll payables are out of
scope by design.

On top of the fold (plugin API 0.8: `Resolver.ledger_entries` for the
statement, `PackReport.data` + `?format=json` for UIs):

- **Bills receivable / bills payable** with ageing buckets (not due / 0–30 /
  31–60 / 61–90 / 90+ by due date), on-account pools as their own rows — PDF
  through the print bridge, CSV, JSON;
- the **party ledger statement** — every entry with its voucher's bill
  allocations alongside, opening balance to date, closing balance equal to
  the trial-balance figure;
- **interest on overdue bills** — rate resolution bill → party default →
  request (`bills-interest` previews the exact arithmetic), and the
  `interest_charge` document mode posts Dr party / Cr Interest Income as a
  reviewable voucher that auto-opens a bill for its total — never a side
  effect;
- **the on-account / against-ref decision at entry time**: `Alt+L` on a party
  line in the web voucher screen opens the allocation dialog (open bills,
  amount cells, the remainder offered as on-account or a new ref); the
  native form's *Allocate bills…* (Ctrl+L) does the same; and the web gains
  the `Alt+I` **Outstanding screen** (receivable/payable, ageing columns,
  party filter) while the native reports page carries all four report keys.

Reversals net in the fold exactly as the mirrored entries netted in the
ledger; books are strangers; idempotent replay allocates nothing twice.

Design: [ADR-023](docs/adr/023-bill-wise-outstanding.md),
[plugin guide](docs/plugin-guide.md).

## Order processing: quote to cash, procure to pay (v0.21, ADR-024)

TallyPrime users live in the order chains: a quotation becomes a sales
order, goods go out on a delivery note, the invoice bills the order — and
the purchase side mirrors it (purchase order → GRN → purchase bill).
LedForge now runs both chains as the fifth plugin pack, on `meta.orders`,
with **zero schema change**:

- **Five document kinds**, each on its own numbering series (QT-…, SO-…,
  DN-…, PO-…, GRN-… — created lazily per book through plugin API 0.9's
  `ensure_voucher_type`), each printing as its commercial document through
  the ADR-013 bridge. Orders are not accounting: each posts a
  **zero-net memorandum trace** (one Dr + one Cr on "Order Book (memo)"),
  so the day book shows the order at its value while the trial balance
  never moves — the tests assert the memo ledger's figure is exactly zero
  after any number of documents.
- **Order position tracking** as a deterministic fold: sales order lines
  track delivered (delivery notes) and invoiced (billing vouchers);
  purchase order lines track received (GRNs) and billed. Status runs
  open → partial → closed; the guards are the caps (nothing fulfils or
  bills beyond the ordered quantity, per item, per order, same party).
- **Order → invoice linkage**: an invoice carries
  `meta.orders = {"settle": [...]}` — the annotation layer ADR-023
  proved, applied to quantities. The validator resolves the order among
  the parties the voucher actually moves and writes the resolution back
  into the stored document, so the fold replays it without guessing.
  PO → GRN → purchase bill works the same way.
- **Pending orders** reports on both sides (quantities, pending quantity
  and value, status, per-party totals — PDF/CSV/JSON) and
  **orders-open** per kind (the picker feed).
- **The native window's document pickers**: an Orders page joins the
  sidebar (Alt+3), *New Order…* posts any of the five kinds with an
  against-picker fed by the live fold, and the voucher form's
  *Order…* (Ctrl+O) picks the open orders an invoice settles.

Reversals net in the fold (reversing an order cancels it; reversing a
delivery note, GRN or settling invoice restores the position); books are
strangers; a refused post consumes no number.

Design: [ADR-024](docs/adr/024-order-processing.md),
[plugin guide](docs/plugin-guide.md).

## Cost centres & cost categories: the break-up (v0.22, ADR-025)

Where did this money go, and along which axis? A rent line splits across
branches; a salary line across departments; the break-up report rolls those
splits up trees. This is the roadmap's **first core-schema change since the
scale-out revision** — deliberately: allocations must aggregate with the same
rigour as entries (exact sums, closure rollups, append-only history), which
the `meta` store cannot promise. Migration `0007`:

- **Two trees, the groups' machinery twice**: cost categories (the
  classification axes — "Department", "Location", "Project") and cost centres
  (the allocation tree), each an adjacency list with a trigger-maintained
  closure, one seeded root per book, cycle- and root-guards, duplicate and
  cross-book parenting schema-impossible. Every real centre answers to
  exactly one category (TallyPrime's rule, a CHECK constraint); the root is
  the container and receives no allocations.
- **`cost_allocations`** — one row per (voucher entry, centre, amount), Dr/Cr
  preserved from the entry, amounts exact, `voucher_date` and `book_id`
  denormalized so reports never join the write path, append-only. The
  **conservation law** is database-enforced like Law 1: a deferrable
  constraint trigger proves Σallocation = entry amount *exactly* at commit —
  a line is fully allocated or left unallocated, never partly — and the
  engine mirrors it with a clean 422 naming the line.
- **Both shapes of `voucher_entries`**: the allocation→entry FK exists on the
  plain shape; on ADR-018's partitioned shape (whose PK is `(id,
  voucher_date)`) it cannot, so the insert-time trigger keeps every guarantee
  there, the partition swap drops it, and unpartitioning re-adds it.
- **The break-up reports**, through the same closure-table machinery as group
  rollups: the **centre summary** (a parent carries its subtree) with the
  **Unallocated** mass surfaced so allocated + unallocated is always the
  window's whole entry mass; one centre's **ledger-wise break-up** (own
  allocations, total proven against the rows); the **category rollup**
  (primary categories carry their secondaries). FYC sweeps excluded on both
  sides.
- **At entry time, both UIs**: the web's `Alt+K` and the native `Ctrl+K`
  open the split dialog — every line with its allocated-so-far and its
  remainder (always visible), exact client-side arithmetic, inline centre
  creation, "Leave unallocated" as the explicit other honest state. The
  native Masters page gains Cost Centres / Cost Categories tabs and the
  Reports page the break-up trio; reversals mirror the splits so every
  centre's net walks back to exactly zero. Plugin API 0.10:
  `DraftLine.cost_allocations` ride the draft through every hook.

Design: [ADR-025](docs/adr/025-cost-centres.md),
[plugin guide](docs/plugin-guide.md).


## Banking: reconciliation, cheques & the gateway seam (v0.23, ADR-026)

The books say one thing, the bank says another; the reconciliation statement
is where the two agree — and where every disagreement is priced, exactly.
The roadmap's **second core-schema change** (migration `0008`), for the same
reason as the first: imported statement facts need database identity
(idempotent re-import is a `UNIQUE` verdict, not a check-then-insert race)
and the proof aggregates them with exact-decimal rigour.

- **Statement import (CSV + MT940)** — validated, all-or-nothing, with a
  `dry_run` preview; the content fingerprint (the statement's own identity +
  each line's facts + occurrence) makes re-importing the same statement a
  clean no-op while a later statement's genuine repeats land beside the
  first. MT940's `:60F:/:62F:` balances become the proof's foldable anchor.
- **The matchmaker never commits** — suggestions are a pure function:
  amount-exact (+50), a date window, reference-fuzzy evidence (cheque
  number, voucher number, narration overlap), ranked. Only the explicit
  match writes, and a manual match may disagree on amount — the proof
  carries the difference as its own row, honestly priced.
- **The match annotation is the kernel's one deliberately mutable table**
  — match/un-match is revisable judgement, not financial history (Law 2
  guards the statement facts; `bank_line_matches` is UPDATE-only, DELETE
  never, provenance columns always). Un-match and re-match at will.
- **The reconciliation statement is a proof** — `book − statement =
  unmatched-book − unmatched-statement + pair differences` (with the
  statement's own opening as a baseline row), exact Decimals, asserted
  every time. The reconciled balance rides along.
- **The cheque register is a fold** — declarations ride vouchers
  (`meta.banking.cheque`: number, bank ledger, PDC maturity), validated to
  ride a voucher that moves their own bank account with per-ledger number
  uniqueness (advisory-locked). *Issued* when posted, *presented* when an
  imported line references the number (precise digit-runs — 448812 never
  matches 4488123), *cleared* when that line is matched.
- **The gateway seam (plugin API 0.11)** — `register_webhook(key, handler)`
  claims `POST /api/v1/hooks/{key}` (book-scoped, bookkeeper role): the
  banking pack's `bank-statement-gateway` accepts the generic gateway JSON
  in-process; a per-bank adapter is a drop-in behind the ADR-016 trust
  gate, and the kernel never learns a bank's name. After-commit events
  (`bank.statement.imported`, `bank.line.matched`, `bank.line.unmatched`)
  fire for bridges; `Resolver.bank_lines` is the sanctioned fold read.
- **Both surfaces**: the web Banking screen (`Alt+G` — import with preview,
  the line book, `M` match / `U` un-match, the proof, the register) and the
  native Banking page (import dialog, suggestions pane, one-click match,
  the proof dialog, the register).

Design: [ADR-026](docs/adr/026-banking-pack.md),
[plugin guide](docs/plugin-guide.md).

## Print bridge: payslips & tax invoices as PDF (ADR-013)

Every posted voucher prints. `GET /api/v1/vouchers/{id}/pdf` offers the
voucher's meta keys to the plugin registry's **document-view factories**
(plugin API 0.5): the payroll pack claims runs and prints the **payslip**
of `?employee=` — components with YTD, gross/deductions/net, and the
payable position replayed through the pack's own fold, so a payslip that
disagrees with the ledger is impossible by construction. The invoicing
pack claims invoices and prints the **tax invoice** (items, GST slabs,
computed totals, due date). Everything else — plain vouchers, reversals,
payouts — prints as the **voucher slip** (Dr/Cr table, equal totals,
Law 2 note). Nothing 404s for lack of a layout.

The writer is core and **dependency-free**: PDF 1.4, base-14 Helvetica
(nothing to embed, nothing to install), exact-decimal formatting at the
edge, uncompressed streams. Packs describe *what* a document says
(`DocumentView`); one layout engine owns pagination and geometry — one
visual language across every pack's documents, and a community pack
ships a printable document by registering one function.

```bash
# The payslip of one employee on a multi-employee run
curl -s "localhost:8000/api/v1/vouchers/42/pdf?employee=Asha%20Verma" -o payslip.pdf

# Any other voucher: the voucher slip
curl -s localhost:8000/api/v1/vouchers/7/pdf -o voucher.pdf
```

In the web UI: `Alt+P` opens the payslip screen — posted runs indexed by
employee and period, figures previewed as strings (Law 3), `Ctrl+P`
prints the PDF, `Esc` is back. The voucher screen got the same power:
post anything, then `Ctrl+P` (or the Print chip) prints that voucher
through the same endpoint — a sale prints as its voucher slip, a payroll
run as a payslip, an invoice as its tax invoice.

Design: [ADR-013](docs/adr/013-print-bridge.md),
[ADR-015](docs/adr/015-daybook-browser.md) (the daybook browser),
[plugin guide](docs/plugin-guide.md#printing-a-document-api-05-adr-013).

## The daybook browser (ADR-015)

The accountant's oldest screen: the chronological register of everything
posted, any line of which reprints as its document. `Alt+B` opens it in
the web UI — `↑/↓` (or `j/k`) walk the list, the selection's entries
preview on the right, `Enter` / `Ctrl+P` print the selected voucher
through the same endpoint as above, `Esc` is back to entry. Filters: type,
date range (engine-side) and a client-side find over voucher numbers,
narrations and ledger names. Chips mark what each voucher carried
(payroll, payout, invoice, stock, reversal); a multi-employee payroll run
points at the payslip screen (it needs an employee to print), everything
else — payouts, reversals, plain vouchers — prints as its voucher slip.

```bash
# The engine surface it browses — the amount column is server-computed
# (the debit-side total, exact Decimal, quantized to cents: Law 3)
curl -s "localhost:8000/api/v1/vouchers?type=SAL&date_from=2026-10-01&date_to=2026-10-31" |
  python3 -m json.tool | head
```

Reprints are identical: posted entries are immutable (Law 2), so the
hundredth reprint is byte-for-byte the document that printed the day it
was posted.

## The trust bridge: community index & signed plugins (ADR-016)

The last planned roadmap line: who vouches for the code that runs inside
your posting transactions? Three loading channels, three answers —
first-party packs (reviewed in this repo), pip installs (you chose them),
and drop-ins, which now load **only with proof**:

- **A publisher's signature** — Ed25519 over the release *facts*
  (`name`, `version`, `api_compat`) plus the artifact's `sha256`, so a
  signature cannot be replayed onto another release or changed bytes —
  from a key your trust store approved; or
- **Your acknowledgement** — `ledgerforge plugins ack` pins the exact
  bytes of a plugin you reviewed yourself.

Everything else is refused at boot — **before the first line of the
plugin executes** (the gate reads the manifest statically, no import),
loudly, with the reason recorded. The refusals are the feature's other
half: `ledgerforge plugins list` shows them, `GET /api/v1/plugins`
(admin) returns them, and the plugins screen (`Alt+X`) renders both
tables with the trust labels explained in place.

The **community plugin index** (`community/plugins/index.json`, curated
by pull request) is where signatures and publisher keys meet operators:
one command verifies and lands a release end to end.

```bash
# the whole lifecycle — author side and operator side
ledgerforge plugins keygen --out ~/.ledgerforge-keys   # author identity
ledgerforge plugins sign my_pack.py --key ~/.ledgerforge-keys/publisher_ed25519.pem
ledgerforge plugins index                               # what's curated
ledgerforge plugins install webhook-forwarder --path ./plugins
ledgerforge plugins ack hometown_pack --path ./plugins  # your own reviewed code
ledgerforge plugins list --path ./plugins               # what loads, and why
```

Design: [ADR-016](docs/adr/016-plugin-trust.md), the
[plugin guide](docs/plugin-guide.md#installation--the-trust-model-adr-016),
the [curation policy](community/plugins/README.md). Pinned by
`tests/test_plugin_trust.py` (25 tests, including "an unsigned drop-in
never executes" and "the committed index entry verifies against the
committed artifact").

## Statutory bridge: PF, ESI & TDS statements (ADR-014)

The payroll pack's wage folds are the input to the statements an
Indian employer files — the **statutory pack** (the fourth business pack,
zero schema change) publishes them as the first consumer of plugin API
0.6 **pack reports**: `register_report(key, factory)`, served at
`GET /api/v1/reports/pack/{key}` — PDF through the ADR-013 layout engine
by default, `?format=csv` for the machine format. The kernel stays
report-blind; the pack owns the domain. Three statements ship: **EPF**
(`pf`), **ESI** (`esi`) and **TDS** (`tds`) — the tax actually withheld
at source, per deductee: a deduction record of what the runs withheld
(`TDS` / `Income Tax` components by default), never a slab computation,
with PANs riding the run entries exactly as posted.

Statements fold the *same* documents the payslips print
(`payroll.period_wages(resolver, period)`) — reversals net out, off-cycle
corrections sum, other months never leak — so a PF figure that disagrees
with a payslip is impossible by construction. Rates, ceilings and the
wage-component set are parameters with long-standing defaults; UAN / ESI
IP numbers / PANs ride the run documents' employee entries (`uan`,
`esi_ip`, `pan`) — blank when absent, never invented. This is a reporting
bridge, not a portal-certified file: every statement footer carries the
applied rules.

```bash
# EPF statement for October 2026 — PDF for checking, CSV for upload prep
curl -s "localhost:8000/api/v1/reports/pack/pf?period=2026-10" -o pf.pdf
curl -s "localhost:8000/api/v1/reports/pack/pf?period=2026-10&format=csv" -o pf.csv

# ESI, with every rule as a parameter
curl -s "localhost:8000/api/v1/reports/pack/esi?period=2026-10&threshold=21000&round=rupee" -o esi.pdf

# PF wage base is Basic + DA by default; widen or cap it explicitly
curl -s "localhost:8000/api/v1/reports/pack/pf?period=2026-10&wage=Basic Salary,DA&ceiling=15000" -o pf.pdf

# TDS — the second pack report: what the runs actually withheld,
# per deductee (a deduction record, never a slab computation)
curl -s "localhost:8000/api/v1/reports/pack/tds?period=2026-10" -o tds.pdf
curl -s "localhost:8000/api/v1/reports/pack/tds?period=2026-10&component=TDS,Income Tax&format=csv" -o tds.csv
```

Design: [ADR-014](docs/adr/014-statutory-bridge.md),
[plugin guide](docs/plugin-guide.md#publishing-a-report-api-06-adr-014).

## GST compliance pack: place of supply, HSN tables, GSTR & e-invoicing (ADR-017)

The plugin guide's worked example grew up: the **gst pack** (the fifth
business pack, zero schema change, issue #6) is a real India localization.
Post a document-mode sale and the transformer derives the TallyPrime lines
— party at the grand total, one line per item, tax under *Duties & Taxes* —
with the levy decided by **place of supply**: same state as the supplier ⇒
CGST + SGST (the SGST line the exact remainder), different state ⇒ IGST.
An item's slab comes from an **HSN rate table with effective dates** —
mobile phones were 12% until 2020-03-31 and 18% from 2020-04-01, and a
2019-dated voucher gets 12% because the calendar says so, not because
someone edited the code (operators load their own notification history via
`LF_GST_HSN_JSON`; the shipped table is an illustrative seed).

Two **pack reports** serve the statements: `gstr1` (the outward B2B invoice
register, GSTIN-wise, reversals netted) and `gst-hsn` (the HSN summary,
`side=outward|inward|both`) — PDF via the shared layout engine or CSV,
quick-tax vouchers footnoted rather than invented into GSTIN rows.

The **e-invoicing bridge** is split along the plugin contract: B2B sales
queue after commit to the `LF_GST_OUTBOX` JSONL outbox (never a network
call inside posting), and `lf-irn-bridge` — a separate process — ships
them to the operator's IRP adapter: 2xx records the IRN, 4xx parks with
the error, 5xx/network retries on the next pass.

```bash
# a Karnataka supplier sells to Tamil Nadu — IGST, slab from the HSN table
curl -s -X POST localhost:8000/api/v1/vouchers -H 'Content-Type: application/json' -d '{
  "voucher_type": "SAL", "voucher_date": "2026-11-07",
  "entries": [{"ledger_name": "Cash", "dc": "D", "amount": "0.01"}],
  "meta": {"gst": {
    "mode": "sale", "party": "Madras Machines", "party_gstin": "33ABCDE1234F1Z5",
    "supplier_state": "29", "place_of_supply": "33",
    "items": [{"description": "Feature phones", "hsn": "8517", "qty": 4, "rate": "1250.00"}]
  }}}'

# the month's outward B2B register, and its HSN summary
curl -s "localhost:8000/api/v1/reports/pack/gstr1?period=2026-11" -o gstr1.pdf
curl -s "localhost:8000/api/v1/reports/pack/gst-hsn?period=2026-11&side=both&format=csv" -o hsn.csv

# the out-of-process bridge against your IRP adapter
LF_GST_OUTBOX=/var/lib/ledgerforge/irn.jsonl lf-irn-bridge --endpoint https://asp.example/irp --once
```

Design: [ADR-017](docs/adr/017-gst-pack.md),
[plugin guide](docs/plugin-guide.md).

## Report scale-out: the balance cache & yearly partitions (ADR-018)

Books that grow past a million entries used to have a "known and boring"
scaling path written down but not built. It is built now, opt-in on both
axes, and the write path is untouched:

- **The balance cache** (`rpt.balance_cache`) freezes exactly what the
  period gate proves unpostable — everything below the earliest OPEN period
  (closed periods are terminal, uncovered dates are refused). A row trigger
  on `periods` deletes cached rows the moment a new or reopened open period
  would make their zone writable again, *in the same transaction* — the
  cache can only ever be slow, never wrong. `GET /reports/trial-balance
  ?fast=true` (also `profit-loss`, `balance-sheet`) reads it; a cold or
  stale cache is a full scan, not a wrong number.
- **`rpt.refresh_balance_cache()`** rebuilds incrementally (the newly
  frozen delta zone only), is advisory-locked per book, idempotent, and
  maintains both inclusive sums (TB/Balance Sheet) and FYC-exclusive ones
  (P&L — a close sweep enters the cached zone when its period closes in
  turn).
- **`ops.partition_entries_by_year()`** converts `voucher_entries` to
  yearly range partitions in one transactional maintenance call: same
  columns, checks, FKs and triggers (the deferrable Law-1 check stays sound
  because a voucher's entries share its date and never span partitions),
  covering index parent-level, BRIN on `voucher_date`, and a
  `SECURITY DEFINER` trigger on *vouchers* creating missing year partitions
  on demand — postings never stall at year boundaries and the app role
  never needs DDL rights. `ops.unpartition_entries()` is the tested mirror;
  cache rows are shape-agnostic and survive both directions.

The operator surface (partition/unpartition are owner-only by design):

```bash
ledgerforge scale-out status                # shape, partitions, cache state
ledgerforge scale-out refresh-cache         # incremental, idempotent
ledgerforge scale-out partition --headroom 2   # maintenance window
ledgerforge scale-out ensure 2031 2031      # the year-end ritual
ledgerforge scale-out unpartition           # the tested revert
```

Admin API: `GET /api/v1/ops/scale-out`, `POST /api/v1/ops/scale-out/refresh-balance-cache`.
Reproduce the numbers: `python scripts/bench_scale_out.py`.

Design: [ADR-018](docs/adr/018-report-scale-out.md).

## Sandbox UI slots: plugin dashboards (ADR-019)

The Phase-3 extensibility surface (§5.5): packs can ship dashboards, and
the host renders them without giving them anything but a rectangle and a
relay.

```python
PLUGIN = PluginManifest(
    name="my-pack",
    version="1.0.0",
    api_compat="0.7",
    slots=(SlotSpec(key="my-dash", title="My dashboard", html=HTML),),
)
```

- **Hardened sandbox**: the web UI embeds every slot as
  `<iframe sandbox="allow-scripts">` — opaque origin: no cookies, no
  localStorage, no host DOM. Inline HTML is served by the host at
  `GET /api/v1/slots/{key}` under `Content-Security-Policy: sandbox`.
- **Relay-fed**: the slot speaks the versioned REST API through the host's
  GET-only postMessage relay (`lf-slot:request` → `lf-slot:response`) —
  it holds no credentials and reaches nothing the host declines to fetch.
- **Keyboard scope stays with the host** (M4 + D4): the slot registers
  *commands* (`lf-slot:register-commands`); the host binds their keys only
  while the slot is open and lists them in the command palette — mouse-free
  discoverability for third-party functionality, and Esc inside the sandbox
  still means back.
- `Alt+U` opens the picker: one row per slot, with its owning pack and
  trust basis. The in-repo teaching pack
  (`ledgerforge.contrib.example_slot`, "Balance glance") exercises the
  whole contract — try it in `make dev`.

Design: [ADR-019](docs/adr/019-sandbox-ui-slots.md),
[plugin guide](docs/plugin-guide.md).

## The native application: a real Windows program (ADR-022)

Everything above assumes an operator: PostgreSQL somewhere, containers,
ports. The native application is the other promise kept — *download,
double-click, a window opens, start typing*:

- **`LedgerForge.exe`** (built per release on a real Windows runner by the
  [`release-windows`](.github/workflows/release-windows.yml) workflow) opens
  a **native Qt window**: Day Book, Masters, Reports (Trial Balance, P&L,
  Balance Sheet, GSTR-1 pack with statutory PDF export), Periods, Sandbox —
  native menus and file dialogs, a taskbar icon, no console. **No browser
  is launched; no web UI ships.** The engine — an **embedded PostgreSQL**
  (the pgserver portable distribution) plus the FastAPI core — runs in a
  **child process** (ADR-022: Qt and libpq do not share an address space
  gracefully), loopback only, stopped when the window closes. Data lives in
  `%LOCALAPPDATA%\LedgerForge` and survives restarts, upgrades, and the
  v0.17 → v0.18 transition unchanged.
- **Keyboard-first survives the platform change**: `Ctrl+N` posts a voucher
  from anywhere, `F5` refreshes, `Enter` opens, `Alt+1..5` navigates — the
  entry form's balance gate unlocks the Post button only when debits equal
  credits to the cent, mirroring the engine's own Law-1 check.
- **The engine is not downgraded.** No SQLite fork: the same deferrable
  Law-1 trigger, the same `NUMERIC(26,8)`, the same partitions — one engine,
  web, Docker and desktop. The window is an API client, zero business logic
  in the GUI.
- **Loopback-only and single-user by construction**: `LF_AUTH_MODE=disabled`
  on a port no other machine can reach; the `LF_REQUIRE_AUTH` guard stays
  armed for network deployments (ADR-020).
- **The trust gate still gates.** Drop signed community plugins into
  `%LOCALAPPDATA%\LedgerForge\plugins` — the ADR-016 verify-before-execute
  gate is unchanged, and their dashboards appear in the Sandbox page.
- **Every release self-proves**: the workflow runs `LedgerForge.exe --smoke`
  offscreen (boot → schema → `/health` → the native window → day book +
  trial balance + GSTR-1) before publishing the zip + SHA-256 checksums. A
  failed smoke test fails the release.

From source (any OS — pgserver ships Linux/macOS binaries too):

```bash
pip install -e ".[gui]"
ledgerforge-native             # the window
ledgerforge-native --smoke     # the same self-check the release runs
```

The window lives in `src/ledgerforge/nativeapp/`, the packaging in
[desktop/](desktop/) (PyInstaller spec + build guide), the decision record
in [ADR-022](docs/adr/022-native-shell.md). The v0.17 browser-based local
edition (`ledgerforge-desktop`, ADR-021) still runs from source for those
who want it.

## What's inside

```
db/                 schema.sql · functions.sql · migrations/ — Alembic tree:
                    frozen 0001 baseline + drift guard, 0002 multi-currency,
                    0003 period locking, 0004 multi-book, 0005 voucher meta,
                    0006 report scale-out (ADR-006/007/008/009/010/018).
                    The chart-of-accounts seed is code: ledgerforge/seeding.py
                    (one definition for bootstrap and POST /books — ADR-009).
src/ledgerforge/    engine (posting/reversal/opening) · api (+books) · auth (OIDC
                    + roles + per-book claims) · reporting (reports, printing:
                    the PDF writer + layout engine) · seeding · plugins ·
                    contrib (example_gst and example_slot — the teaching
                    modules — plus the gst, invoicing, inventory, payroll,
                    statutory and bills packs; the gst pack ships with
                    irn_bridge, the out-of-process e-invoicing bridge)
frontend/           keyboard-kit — framework-agnostic command/keybinding/focus framework (TS)
                    web — the voucher-entry screen (with the print chip and the
                    bill-allocation dialog), command palette, payslip screen,
                    daybook browser, outstandings screen and sandboxed slot
                    host (React + Vite)
tests/              laws, hierarchy, immutability, reports, concurrency, plugins, auth,
                    migrations, multi-currency, period locking, multi-book, invoicing,
                    inventory, payroll, printing, statutory (PF/ESI/TDS), scale-out (the
                    balance cache and the partition round-trip), deep audit
                    (adversarial: races, rounding boundaries, gate bypasses)
docs/               Phase-1 analysis, ADRs, keyboard-ux plan, plugin guide, db design
scripts/            benchmarks (bench_hierarchy.py, bench_posting.py,
                    bench_scale_out.py), init_db.sh, regen_baseline.py
desktop/            the native application's packaging (ADR-022): the PyInstaller
                    spec, the frozen-app entry point, the icon, the quickstart
                    that ships in the zip — plus release-windows.yml building
                    it per tag
src/ledgerforge/
  nativeapp/         the Qt window (ADR-022): client, pages, dialogs, smoke
  desktop.py         the ADR-021 engine stack both editions share
```

## Performance (measured, reproducible)

`make bench` runs both benchmarks against your database. On the reference environment (PostgreSQL 16.2):

- **Hierarchy rollups:** closure-table and recursive-CTE rollups measure at parity on a 429-group / 3,402-ledger / 120k-entry book (47 ms vs 48 ms for the whole-tree rollup) — the closure model wins on SQL simplicity, integrity, and drill-down queries, not on raw speed. Full discussion: [docs/analysis.md §3.8](docs/analysis.md).
- **Concurrency:** 8 threads × 60 vouchers → 480/480 posted, strictly gapless numbering, zero deadlocks, ~174 vouchers/s on a single type's counter row, Trial Balance exact to the last sub-cent digit.
- **Scale-out:** on a 400k-entry book (200k vouchers, 7 fiscal years, 5 closed), the warm balance cache reads a Trial Balance ~1.4× faster than the direct scan on both the plain and the partitioned shape (93ms vs 130ms; 107ms vs 148ms), with fast == slow asserted to the cent and partition pruning on every dated range — the gap widens as the frozen zone grows. `python scripts/bench_scale_out.py`.

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

v0.27.0 — everything below, plus the payroll depth
(v0.26, ADR-029: attendance registers as facts, loans with EMI
recovery, the full & final settlement, payslip distribution) and
**data exchange & the Tally migration path (v0.27, ADR-030: CSV/XLSX
import & export, the TallyPrime XML migration with a verification
report, SQL views + ODBC, personal API tokens)** — the accounting core
is at parity.

v0.24.0 — the accounting kernel, the voucher-entry screen, the command palette
with persisted key rebinding, OIDC authentication, multi-currency books,
period locking with the financial-year close, multi-book (v0.5), invoicing
as the first plugin pack (v0.6), inventory as the second — zero schema
change (plugin API 0.3) — payroll as the third: runs and payouts as
documents, fiscal-year YTD folds, zero schema change (plugin API 0.4:
Resolver.fy_start_month) — the print bridge (v0.9): every voucher as PDF
via plugin document views (plugin API 0.5), payslips, tax invoices and the
voucher-slip fallback, plus the payslip screen — the statutory
bridge (v0.10): PF & ESI statements folded from the payroll runs as the
first pack report (plugin API 0.6), with `Ctrl+P` on the voucher screen
printing any posting voucher — the daybook browser (v0.11): `Alt+B`
browses every posted voucher with engine-computed amounts and reprints any
of them, while the statutory pack gains TDS as the second pack report —
what the runs actually withheld, per deductee — the trust bridge
(v0.12): a signed community plugin index
([community/plugins](community/plugins)) with the full author/operator
lifecycle (`ledgerforge plugins keygen/sign/install/ack/list`), a
boot-time trust gate that refuses unsigned, tampered or unapproved
drop-ins *before executing a line of them*, and the plugins screen
(`Alt+X`) showing every loaded pack's channel and trust basis plus every
refusal and why (ADR-016) — **and the GST compliance pack (v0.13, issue
#6): place of supply (CGST+SGST vs IGST), HSN rate tables with effective
dates, GSTR-1 and HSN-summary statements as pack reports, and the
e-invoicing bridge — the IRN outbox plus the out-of-process
`lf-irn-bridge` (ADR-017)** — the report scale-out (v0.14, ADR-018): the
balance cache and the transactional yearly-partition conversion behind
`ledgerforge scale-out`, opt-in on both axes — the sandbox UI slots
(v0.15, ADR-019, plugin API 0.7): plugin dashboards in opaque-origin
iframes fed through the host's relay, never keyboard scope — and the
operations hardening (v0.16, ADR-020): the `LF_REQUIRE_AUTH` boot guard
with `/health` reporting `auth_mode`, tooled curator-key rotation
(`plugins verify-index` / `rotate-curator`), the browser-level E2E tier
proving the full keyboard flow in real Chromium (CI job `e2e`), and the
production runbook — `ops/` backup + restore drills, the hardened
`docker-compose.prod.yml`, [docs/deployment.md](docs/deployment.md) —
and, beyond the planned roadmap, **the desktop edition (v0.17, ADR-021): a
Windows application** — and its v0.18 correction, **the native application
(ADR-022): a real Windows program** — `LedgerForge.exe` opens a native Qt
window (no browser), the embedded engine runs in a child process, released
per tag with a self-proving offscreen smoke gate — and **v0.19, the
completeness pass (Phase 1 of the [roadmap](ROADMAP.md))**: multi-book in
the web UI (the `Alt+O` Books screen — create, rename, archive, switch,
with `X-Book-Id` plumbed through the client and the open company named in
the entry bar), reversal from the daybook (`Ctrl+R`, two-key confirm — Law
2's correction path, no longer API-only), the periods screen (`Alt+E` —
lock, unlock, the close preview, the confirmed close), the `master.created`
event the plugin guide always promised, FX line entry and F4–F9 type keys
in the native window's voucher form, and `cryptography` as the direct
dependency the trust gate always imported — and **v0.20, bill-wise &
outstanding management (Phase 2 of the roadmap, ADR-023)**: bill references
(new / against / on-account) on any party-moving voucher as the fourth
plugin pack — zero schema change — with the conservation law that keeps
Σ open bills + on-account equal to every party's ledger balance, bills
receivable/payable with ageing buckets, the bill-linked party ledger
statement, interest on overdue bills posted as reviewable vouchers, and
the allocation decision at entry time (`Alt+L` in the web form, *Allocate
bills…* in the native one) plus the web `Alt+I` outstandings screen — and
**v0.21, order processing (Phase 3 of the roadmap, ADR-024)**: the
quote-to-cash and procure-to-pay chains as the fifth plugin pack —
quotation, sales order, delivery note, purchase order and GRN, each on its
own numbering series (plugin API 0.9: `ensure_voucher_type`), each posting
a zero-net memorandum trace (orders are not accounting; the memo ledger's
trial-balance figure is exactly zero by construction), the position fold
(delivered / received, invoiced / billed, open → partial → closed, the
caps as the guards), the order → invoice settle annotation riding billing
vouchers, pending-orders reports on both sides, and the native window's
Orders page with its document pickers.
What comes next is written down as a plan of record —
**[ROADMAP.md](ROADMAP.md): eleven phases to v1.0, “everything an Indian
SMB's daily accounting needs, on LedgerForge, without wishing for
TallyPrime”** — Phase 1 landed in v0.19.0, Phase 2 (bill-wise &
outstanding) in v0.20.0, Phase 3 (order processing) in v0.21.0, Phase 4
(cost centres) in v0.22.0, Phase 5 (the banking pack) in v0.23.0, and
Phase 6 (inventory depth) in v0.24.0. **v0.25, GST compliance depth** —
the 2A/2B reconciliation, GSTR-3B/9, credit/debit notes, multi-GSTIN and
the e-way-bill outbox (Phase 7, ADR-028). Each phase tracks as a GitHub
milestone with a tracking issue:

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
- [x] Print bridge: every voucher as PDF — payslips & tax invoices as
      plugin document views, voucher-slip fallback, payslip screen
      (ADR-013, plugin API 0.5)
- [x] Statutory bridge: PF & ESI statements from the payroll folds as a
      pack report (PDF + CSV, parameterized rules), plus `Ctrl+P` on the
      voucher screen printing the last posted voucher (ADR-014,
      plugin API 0.6)
- [x] Daybook browser: every posted voucher browsable (`Alt+B`) and
      reprintable through the print bridge, with the engine-computed
      amount column; TDS as the second pack report (ADR-015 + ADR-014)
- [x] Community plugin index + signed plugin trust model: Ed25519 release
      signatures (facts + bytes), the boot-time drop-in gate (refuse before
      execution), the `ledgerforge plugins` CLI, the admin plugins API and
      the `Alt+X` screen (ADR-016)
- [x] GST compliance pack (issue #6): place of supply (CGST+SGST vs IGST),
      HSN rate tables with effective dates, GSTR-1 & HSN-summary statements
      as pack reports, and the e-invoicing bridge — the IRN outbox + the
      out-of-process `lf-irn-bridge` (ADR-017)
- [x] Report scale-out (v0.14, ADR-018): the balance cache (exact below
      the open-period horizon, invalidated in-transaction when periods
      change) with `?fast=true` report reads, and the transactional
      yearly-partition conversion (`ledgerforge scale-out`) with a tested
      revert
- [x] Sandbox UI slots (v0.15, ADR-019, plugin API 0.7): manifest-declared
      plugin dashboards rendered in opaque-origin iframes, fed through the
      host's GET-only relay, with slot commands in the host's command
      palette — never keyboard scope
- [x] Operations hardening (v0.16, ADR-020): the `LF_REQUIRE_AUTH`
      production guard (`/health` reports `auth_mode`), curator key
      rotation tooled and audited (`verify-index` / `rotate-curator`),
      browser-level E2E of the keyboard flow in CI, `ops/` backup +
      restore drills (Law-1 verified), `docker-compose.prod.yml` and
      [docs/deployment.md](docs/deployment.md)
- [x] The desktop edition (v0.17, ADR-021): `LedgerForge.exe` — embedded
      PostgreSQL (pgserver), the web UI same-origin on loopback, data in
      `%LOCALAPPDATA%\LedgerForge`, the trust gate intact, built and
      smoke-gated per release on `windows-latest`
- [x] The native application (v0.18, ADR-022): `LedgerForge.exe` opens a
      native Qt window — no browser — with the engine in a child process,
      the embedded PostgreSQL underneath, day book / masters / reports /
      periods / sandbox screens, offscreen-proven in CI and smoke-gated
      per release
- [x] The completeness pass (v0.19, Phase 1 of the roadmap): multi-book in
      the web UI (Books screen, `X-Book-Id`), reversal from the daybook,
      the periods screen, `master.created`, FX line entry + F4–F9 in the
      native form, direct `cryptography` dependency, docs honest
- [x] Bill-wise & outstanding management (v0.20, Phase 2, ADR-023):
      bill references as the fourth plugin pack (zero schema change) with
      the conservation law (bills + on-account == the party's ledger
      balance, proven against the trial balance), bills receivable/payable
      with ageing, the bill-linked ledger statement, interest on overdue
      bills as reviewable vouchers, allocation dialogs in both UIs and the
      web outstandings screen (plugin API 0.8: `ledger_entries`, JSON pack
      reports)
- [x] Order processing (v0.21, Phase 3, ADR-024): quotation, sales
      order, delivery note, purchase order and GRN as the fifth plugin
      pack (zero schema change) — their own numbering series (plugin API
      0.9: `ensure_voucher_type`), the zero-net memorandum trace (orders
      never move the trial balance), the position fold with cap guards,
      the order→invoice settle annotation, pending-orders reports both
      sides, and the native Orders page + document pickers
- [x] Cost centres & cost categories (v0.22, Phase 4, ADR-025): the first
      core-schema change since the scale-out — cost_centres/cost_categories
      trees with closures (migration 0007), cost_allocations with the
      database-enforced conservation law (fully allocated or unallocated,
      never partly — the deferrable Law-1 discipline), the break-up reports
      through the closure machinery (centre summary + Unallocated,
      ledger-wise within centre, category rollup), and the split dialogs in
      both UIs (Alt+K / Ctrl+K, plugin API 0.10)
- [x] The banking pack (v0.23, Phase 5, ADR-026): the second core-schema
      change (migration 0008) — bank statement batches/lines with
      content-fingerprint idempotency, the match annotation as the kernel's
      one deliberately mutable table (UPDATE-only, DELETE never), the
      matchmaker as a pure function (suggestions never commit), the
      reconciliation proof (book − statement = the unmatched nets + pair
      differences + the statement baselines, exact), the cheque register
      fold (issued → presented → cleared, PDC maturity), the gateway
      webhook seam + after-commit events (plugin API 0.11:
      register_webhook, Resolver.bank_lines), and both surfaces (web Alt+G
      + the native Banking page)
- [x] The inventory depth (v0.24, Phase 6, ADR-027): batches, serials
  and expiry with FEFO, godowns as a first-class dimension, re-order
  levels with the shortfall report, per-party date-effective price
  lists, and the manufacturing journal with its BOM-validated cost
  rollup — the depth stays in the document fold (ADR-011's extension);
  the masters get migration `0009_stock_depth` (plugin API 0.12: the
  stock-master reads on the Resolver).
- [x] The GST compliance depth (v0.25, Phase 7, ADR-028): 2A/2B ITC reconciliation, GSTR-3B/9, credit/debit notes, multi-GSTIN, the e-way-bill outbox + bridge

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md), the ADRs, and [docs/analysis.md](docs/analysis.md) first — the financial laws are the contract; PRs that bend them will be (politely) rejected. Conventional commits, ruff-clean code, tests for every behavior change.

## License

Copyright (C) 2026 mir-ashiq and contributors.

This program is free software: you can redistribute it and/or modify it under the terms of the [GNU Affero General Public License v3.0](LICENSE) as published by the Free Software Foundation. Plugins integrating through the versioned plugin API are welcome under any license their authors choose.
