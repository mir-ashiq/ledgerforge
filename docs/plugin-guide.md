# Plugin Guide

**Runtime:** `ledgerforge.plugins.PluginManager` · **Worked examples:** `ledgerforge/contrib/example_gst.py` (tax transform) and `ledgerforge/contrib/invoicing.py` (a complete document pack) · **Architecture:** ADR-003, `docs/analysis.md` §5.

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

`Resolver` (inside-transaction helper): `resolve.ledger(name=…)` and `resolve.ensure_ledger(name, group_name)` let tax packs create/pay into the right ledgers (e.g. `CGST Payable` under *Duties & Taxes*) atomically with the posting.

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
