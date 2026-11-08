"""LedgerForge — a community-driven, keyboard-first, double-entry accounting engine.

Inspired by TallyPrime. The three financial laws (docs/analysis.md §7):
1. Total debits must equal total credits for every transaction, exactly.
2. Financial history is append-only; corrections happen via reversal vouchers.
3. All money is arbitrary-precision decimal — floating point is banned.
"""

__version__ = "0.27.0"

#: Version of the stable plugin API surface (semver; see docs/plugin-guide.md).
#: 0.2 — DraftLine gained the optional FX snapshot fields
#:       (currency/amount_fx/fx_rate, all-or-none); transformers that rebuild
#:       lines must keep the trio consistent or drop it (issue #4).
#: 0.3 — Resolver gained the document-store read (``prior_documents``) and
#:       transaction-scoped advisory locks (``lock``); ``api_compat`` became
#:       a floor, not an exact pin — older-minor plugins keep loading (ADR-011).
#: 0.4 — Resolver gained the read-only ``fy_start_month`` (the book's
#:       fiscal-year start month), the one fact a year-to-date fold needs
#:       (ADR-012).
#: 0.5 — The print bridge: ``DocumentView``/``DocumentContext`` and
#:       ``PluginRegistry.register_document_view(key, factory)`` — packs own
#:       their documents' print layouts, the kernel stays meta-blind and
#:       renders every unclaimed voucher as the voucher slip (ADR-013).
#: 0.6 — Pack reports: ``PackReport`` and
#:       ``PluginRegistry.register_report(key, factory)`` — packs publish
#:       book-scoped reports (PDF through the shared layout engine, or CSV
#:       for machine consumption) served by ``GET /api/v1/reports/pack/{key}``;
#:       the first live consumer is the statutory bridge (ADR-014).
#: 0.7 — UI slots: ``PluginManifest.slots`` — packs declare sandboxed
#:       dashboard slots (inline HTML served by the host at
#:       ``GET /api/v1/slots/{key}``, or an external URL) that the web UI
#:       renders in a hardened iframe. Slots never receive keyboard scope:
#:       they register commands with the host over postMessage and the host
#:       binds keys to them (M4 + D4, ADR-019).
#: 0.8 — ``Resolver.ledger_entries`` and ``PackReport.data``: the engine-side
#:       rows a ledger statement folds, and the JSON payload a UI consumes —
#:       one fold, every surface (ADR-023).
#: 0.9 — ``Resolver.ensure_voucher_type(code, name, prefix, restart_yearly)``:
#:       book-scoped, idempotent — a pack whose documents deserve their own
#:       numbering series (orders: QT/SO/DN/PO/GRN) creates it inside the
#:       posting transaction; the engine resolves the draft's type after
#:       transformers, so a transformer may rewrite ``voucher_type`` (ADR-024).
#: 0.10 — ``DraftLine.cost_allocations`` (with ``DraftCostAllocation``):
#:       cost-centre splits ride voucher lines through every transformer —
#:       Σallocation = amount is re-proven after each hook, and transformers
#:       that split lines must split the allocations with them (ADR-025).
#: 0.11 — ``PluginRegistry.register_webhook(key, handler)`` (inbound hooks at
#:       ``POST /api/v1/hooks/{key}``, the banking gateway seam) and
#:       ``Resolver.bank_lines(ledger_id)`` (imported statement lines with
#:       their live match state — the banking pack's fold input, the
#:       ``fy_start_month`` precedent of 0.4) (ADR-026).
#: 0.12 — the stock-master reads (ADR-027): ``Resolver.stock_item(name)``,
#:       ``Resolver.stock_items()``, ``Resolver.stock_bom(name)`` (frozen
#:       ``StockItemMaster`` / ``StockBomMaster`` snapshots) and
#:       ``Resolver.stock_price(item, party, on)`` (the date-effective
#:       price-list lookup) — the ledger/group discipline applied to stock
#:       masters, so the inventory pack can enforce tracking modes and
#:       validate recipes without ever touching the session.
#: 0.13 — the portal-download read (ADR-028):
#:       ``Resolver.gst_portal_lines(kind, period, gstin=None)`` (the
#:       latest-snapshot rows of one imported GSTR-2A/2B statement — the
#:       GST pack’s reconciliation fold input, the ``bank_lines``
#:       precedent of 0.11) — so the pack’s ITC comparison reads
#:       stored facts through the versioned API, never the session.
#: 0.14 — the attendance read (ADR-029):
#:       ``Resolver.attendance_lines(period)`` (the live register’s rows —
#:       raw day counts of one work month, the payroll pack’s per-day
#:       fold input, the ``fy_start_month`` precedent of 0.4) — so the
#:       pack computes payable days through the versioned API, never
#:       the session, and the kernel stays meaning-blind.
PLUGIN_API_VERSION = "0.14"
