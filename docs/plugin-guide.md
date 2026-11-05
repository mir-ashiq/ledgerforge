# Plugin Guide

**Runtime:** `ledgerforge.plugins.PluginManager` · **Worked example:** `ledgerforge/contrib/example_gst.py` · **Architecture:** ADR-003, `docs/analysis.md` §5.

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

PLUGIN = PluginManifest(name="my-gst-pack", version="0.1.0", api_compat="0.1")


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
