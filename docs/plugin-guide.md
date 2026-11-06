# Plugin Guide

**Runtime:** `ledgerforge.plugins.PluginManager` · **Worked examples:** `ledgerforge/contrib/example_gst.py` (tax transform), `ledgerforge/contrib/invoicing.py` (a complete document pack), `ledgerforge/contrib/inventory.py` (derived state on the document store) and `ledgerforge/contrib/payroll.py` (fiscal-year folds) · **Architecture:** ADR-003, ADR-010, ADR-011, ADR-012, `docs/analysis.md` §5.

## The two tiers

| Tier | Runs | Trust | Use for |
|---|---|---|---|
| **In-process plugin** | inside the posting transaction | trusted code | tax transforms, posting policy validators, report contributors |
| **Event subscriber** | after commit (webhooks/queue) | untrusted-friendly | notifications, e-invoicing bridges, BI sync, caches |

## Anatomy of a plugin

A plugin is a Python module exposing a `PLUGIN` manifest and a `register(registry)` function:

```python
from decimal import Decimal
from ledgerforge.plugins import PluginManifest, ValidationError

PLUGIN = PluginManifest(name="my-gst-pack", version="0.1.0", api_compat="0.2")


def register(registry):
    registry.register_transformer(add_gst_lines)
    registry.register_validator(no_weekend_posting)
    registry.on("voucher.posted", notify_warehouse)


def add_gst_lines(draft, resolve):
    """Intra-state GST: split rate/2 into CGST + SGST, add to the party line."""
    cfg = draft.meta.get("gst")
    if not cfg:
        return draft  # pure function: no meta, no change
    rate = Decimal(str(cfg["rate"]))  # Law 3: never float
    tax = draft.lines[0].amount * rate / Decimal(100)
    ...
    return draft.with_extra_lines([...])  # engine re-validates Law 1 anyway


def no_weekend_posting(draft, resolve):
    if draft.meta.get("policy:no_weekends") and draft.voucher_date.weekday() >= 5:
        raise ValidationError("Weekend posting is blocked by policy")


def notify_warehouse(event): ...  # after commit; exceptions are logged, never break posting
```

### Hook contracts

- **Transformer** `(draft: VoucherDraft, resolve: Resolver) -> VoucherDraft` — may add/adjust lines before engine validation. Runs inside the transaction; raising aborts the whole posting.
- **Validator** `(draft, resolve) -> None` — raise `ValidationError` to veto (HTTP 422). Runs inside the transaction.
- **Event handler** `(event: DomainEvent) -> None` — receives `voucher.posted`, `master.created`, … after commit. Failures are isolated.

`Resolver` (inside-transaction helper):

- `resolve.ledger(name=…)` / `resolve.group(name)` — book-scoped master lookups.
- `resolve.ensure_ledger(name, group_name)` — look up or atomically create under a group of the same book (e.g. `CGST Payable` under *Duties & Taxes*).
- `resolve.prior_documents(key)` — **plugin API 0.3 (ADR-011):** every posted voucher of the book whose `meta` carries `key`, oldest first, as `PriorDocument(voucher_id, voucher_no, voucher_date, reversal_of, meta)` — plus every voucher reversing one of those, so folds can net document pairs. The kernel interprets nothing; the payload is exactly what was stored.
- `resolve.lock(name)` — **plugin API 0.3 (ADR-011):** transaction-scoped advisory lock on `"{book}:{name}"`, released at commit/rollback. Lock a voucher's names in **one sorted pass** (deadlock discipline).
- `resolve.fy_start_month` — **plugin API 0.4 (ADR-012):** the book's fiscal-year start month, 1–12, read-only — the one fact a year-to-date fold needs to window its accumulators. Immutable once the book has vouchers.

`api_compat` is a **floor, not a pin** (ADR-011): a plugin declaring an older minor keeps loading on a newer build; a plugin declaring a newer minor is refused loudly.

## Installation

- **pip path:** declare an entry point in your package —
  ```toml
  [project.entry-points."ledgerforge.plugins"]
  my-gst-pack = "my_gst_pack:PLUGIN_MODULE"
  ```
  (the module containing `register`).
- **Drop-in path:** put the package/folder on `LF_PLUGIN_PATH` (default: `./plugins`); every module with `PLUGIN` + `register` is loaded at startup.

Incompatible plugins (`api_compat` outside the supported range) are refused loudly at boot — never silently misbehaving.

## What never changes

The engine re-validates the three financial laws **after** your transformer runs: unbalanced output, non-decimal amounts, or attempts to mutate history are impossible regardless of plugin quality. A plugin can veto or enrich a posting; it can never corrupt one.

## Worked pack: invoicing (`ledgerforge/contrib/invoicing.py`)

The first complete business document built purely on the runtime — the
roadmap's "invoicing as a plugin, not in core" promise kept. Two document
modes, both riding `meta` (which persists with the voucher and returns from
`GET /vouchers/{id}`, ADR-010):

**Sales invoice** — describe the commercial document; the transformer derives
the accounting:

```json
POST /api/v1/vouchers
{
  "voucher_type": "SAL", "voucher_date": "2026-11-06",
  "entries": [{"ledger_name": "Sales", "dc": "C", "amount": "0.01"}],
  "meta": {
    "invoice": {
      "customer": "Acme Traders",
      "items": [
        {"description": "Widgets", "qty": "10", "rate": "100.00", "tax_rate": "18"},
        {"description": "Installation", "qty": "1", "rate": "250.00", "tax_rate": "0"}
      ],
      "due_days": 30
    }
  }
}
```

What posts (the exact TallyPrime GST-sale shape): one debit to
`Acme Traders` (auto-created under *Sundry Debtors*) for the grand total;
one credit per item on its `revenue_ledger` (default `Sales`, under *Sales
Accounts*) with `Widgets × 10 @ 100.00` narrations; CGST/SGST split lines
per taxed item (under *Duties & Taxes*, the SGST line as remainder so the
split always sums to the tax exactly). The pack annotates
`meta.invoice.computed` — `subtotal`, `tax_total`, `grand_total`,
`due_date` — and the stored voucher **is** the invoice: items, rates and
totals all come back from `GET /vouchers/{id}`, immutable like the amounts
(Law 2 covers documents).

**AR receipt** — settle the invoice:

```json
{"receipt": {"customer": "Acme Traders", "amount": "1430.00",
             "bank_ledger": "HDFC Bank", "reference": "NEFT-42"}}
```

Debits the bank (auto-created under *Bank Accounts*), credits the customer.

Guard rails (all 422): tax rates outside the GST slabs `0/5/12/18/28`,
non-positive quantities, `meta.gst` + `meta.invoice` (double-tax),
`invoice` + `receipt` in one draft, and — as the policy-validator example —
document dates in the future (override with
`meta["policy:allow_future_document_dates"] = true`).

`ISSUED` (the after-commit seam) records every posted invoice/receipt with
its voucher number and customer: the spot an e-invoicing bridge or BI sync
subscribes to. The pack is registered as an entry-point
(`invoicing = "ledgerforge.contrib.invoicing"`), so it loads at boot
alongside the example — and it exercises the full ADR-009 contract:
masters resolve through the book-scoped `Resolver`, so the same customer
name in two books is two ledgers, never a leak.

## Worked pack: inventory (`ledgerforge/contrib/inventory.py`)

The second pack — and the first roadmap feature shipped with **zero
schema change** (ADR-011). Stock positions are *derived* by folding every
posted `meta.stock` document, the way reports fold entries; the pack
reads prior documents through `resolver.prior_documents` and serializes
same-item movements with `resolver.lock` (both plugin API 0.3).

Four document modes:

```json
{"stock": {"movement": "in", "party": "Omega Supplies",
  "items": [{"item": "Widgets", "qty": "10", "rate": "100.00"}]}}
// → Cr Omega Supplies (Sundry Creditors) 1000.00
//   Dr Widgets (Stock-in-Hand) 1000.00 · avg 100.0000

{"stock": {"movement": "out", "party": "Acme Traders",
  "items": [{"item": "Widgets", "qty": "3", "rate": "150.00"}]}}
// → Dr Acme Traders 450.00 · Cr Sales 450.00 (the commercial leg)
//   Dr Cost of Goods Sold 300.00 · Cr Widgets 300.00 (at avg cost)
//   computed: gross_margin 150.00

{"stock": {"movement": "transfer",
  "items": [{"item": "Widgets", "qty": "4", "to_ledger": "Widgets (Store B)"}]}}
// → Dr Widgets (Store B) / Cr Widgets — value moves, quantities don't

{"stock": {"movement": "recount",
  "items": [{"item": "Widgets", "qty": "9"}]}}
// → Cr Widgets 100.00 / Dr Stock Adjustment 100.00 (count delta at avg)
```

Valuation is a **moving weighted average** with closed arithmetic:
`value` only moves by posted 2-dp line amounts, so issuing the last unit
costs exactly the remaining value — positions empty to `0.00`, and the
transformer's computed annotations always agree with a later re-fold
(`inventory.position(resolver, item)` runs the same fold for bridges).
Reversals net to zero on stock: the fold skips an undone original, so
reversing a goods-out restores the stock just as the mirrored entries
restored the ledgers.

Guard rails (all 422): insufficient stock (`policy:allow_negative_stock`
overrides partial overdrafts; an empty position never issues), transfers
that would go negative, no-op recounts, mode conflicts (`stock` +
`gst`/`invoice`/`receipt`/`payroll`, vetoed by all packs), FX lines, future
dates (policy-overridable), and item names that would silently reuse an
unrelated existing ledger. `MOVEMENTS` (after-commit seam) records every
stock document — the warehouse-bridge hook point.

## Worked pack: payroll (`ledgerforge/contrib/payroll.py`)

The third pack — the second in a row with **zero schema change**
(ADR-012). Salaries are documents like invoices and stock: the *run*
accrues, the *payout* settles, and the derived state (year-to-date,
payable) is a deterministic fold over posted documents, never stored.
Plugin API 0.4 grew the one fact the fold needs
(`Resolver.fy_start_month`).

```json
{"payroll": {"mode": "run", "period": "2026-10",
  "employees": [{"employee": "Asha Verma", "components": [
    {"name": "Basic Salary", "amount": "50000.00", "kind": "earning"},
    {"name": "HRA", "amount": "20000.00", "kind": "earning"},
    {"name": "PF Payable", "amount": "1800.00", "kind": "withheld"},
    {"name": "Employer PF", "amount": "1800.00", "kind": "employer",
     "liability": "PF Payable"}
  ]}]}}
// → Dr Basic Salary 50000 · Dr HRA 20000 (Indirect Expenses)
//   Dr Employer PF 1800 · Cr PF Payable 1800+1800 (Duties & Taxes)
//   Cr Asha Verma 68200 (Provisions — the net-pay payable)
//   computed: per-employee gross/withheld/employer/net + fiscal-year YTD

{"payroll": {"mode": "payout", "employee": "Asha Verma",
  "amount": "68200.00", "bank": "HDFC Bank"}}
// → Dr Asha Verma 68200 · Cr HDFC Bank 68200
//   computed: payable_after 0.00
```

Components are pay heads with a `kind` — the TallyPrime taxonomy in
miniature: `earning` (Dr expense), `withheld` (Cr liability, reduces
net), `employer` (Dr expense + Cr a required `liability`; never touches
net). **YTD windows by the book's fiscal year** (a September payslip
never shows October); **payable is all-time** (accrued net minus
payouts — a balance never expires). One run per employee per period,
serialized by advisory locks; reversals net to zero on both views;
`payroll.ytd(resolver, employee)` and `payroll.payable(resolver,
employee)` replay the folds for bridges.

Guard rails (all 422): malformed periods (`YYYY-MM`), duplicate
employees/components, bad kinds, non-positive amounts, employer without
a liability, withheld exceeding gross, periods that have not started,
future dates, mode conflicts (vetoed by all packs), FX lines, and
employee names colliding with unrelated ledgers. `RUNS` (after-commit
seam) records every payroll document — the payslip/PF-bridge hook point.
