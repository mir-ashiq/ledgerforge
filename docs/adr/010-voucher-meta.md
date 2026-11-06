# ADR-010: Voucher meta — the plugin document store

- **Status:** Accepted (2026-11-06)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

`POST /vouchers` has accepted a `meta` dict since the plugin runtime landed (ADR-003) — it is how a draft tells the example GST pack its rate. But `meta` was request-scoped: plugins read it on the way in, the engine persisted the *accounting lines* they produced, and the document itself evaporated at commit. `GET /vouchers/{id}` could show the accounting shadow of a sale, never the sale: no items, no quantities, no rates, no tax configuration, no computed totals. The roadmap (§9) wants invoicing as a plugin; an invoice that forgets its own line items at commit is not an invoice.

The same gap hits every after-commit subscriber: an e-invoicing bridge or BI sync listening on `voucher.posted` received the voucher number and nothing else — not enough to file a statutory invoice or sync a data warehouse.

## Decision

1. **`vouchers.meta JSONB NOT NULL DEFAULT '{}'`** (revision `0005_voucher_meta`). The engine persists the **post-transform** draft meta, so what is stored is what was actually posted: plugin *input* (the invoice items a client sent) plus plugin *output* (the `computed` totals the invoicing pack annotates back).

2. **The kernel is meta-blind.** Core validation never reads it, no `rpt.*` function queries it, no constraint depends on it. This is the deliberate answer to the ERPNext lesson (§8): metadata as *stored document payload* is fine; metadata as *schema indirection for the core ledger* is not. Plugins own their keys; the kernel only guarantees storage, immutability and round-trip.

3. **The API round-trips it.** `GET /vouchers/{id}` returns `meta`; the `voucher.posted` domain event carries it (`event.data["meta"]`) so after-commit subscribers see the complete document. Reversals start from empty meta — a credit note is a new document; `reversal_of` is the provenance link.

4. **Law 2 extends to documents.** `meta` is frozen with the voucher by the existing append-only trigger: a posted invoice's items cannot be quietly edited after the fact, exactly like its amounts.

## Consequences

- **(+)** The invoicing pack (roadmap §9, first e-invoicing-era plugin) is now possible *entirely outside core*: it builds the accounting lines from the document, annotates computed totals, and the voucher **is** the invoice — retrievable, immutable, event-carrying.
- **(+)** Every existing plugin gains persistence for free (the GST config now survives the request), and after-commit bridges receive a complete document instead of a bare number.
- **(+)** One generic primitive instead of N per-feature tables: no `invoices`, no `invoice_lines` in core, no schema governance burden per vertical.
- **(−)** Meta is only as good as the plugin that wrote it: garbage keys persist as garbage. Mitigated by plugin ownership (keys are namespaced by convention, e.g. `invoice.*`, `gst.*`) and by the kernel never interpreting them.
- **(−)** JSONB grows with each voucher (~hundreds of bytes for a typical invoice) — negligible against the entries it replaces; books that need reporting *on* meta keys should promote those to real schema through a migration, not query JSONB in `rpt.*`.

Shipped as revision `0005_voucher_meta` (ADR-006 post-freeze policy); `db/schema.sql` updated in the same change; the drift guard (`tests/test_migrations.py`) proves the alembic and bootstrap paths structurally identical. The first consumer is `ledgerforge/contrib/invoicing.py` — the invoicing plugin pack.
