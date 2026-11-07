"""LedgerForge — a community-driven, keyboard-first, double-entry accounting engine.

Inspired by TallyPrime. The three financial laws (docs/analysis.md §7):
1. Total debits must equal total credits for every transaction, exactly.
2. Financial history is append-only; corrections happen via reversal vouchers.
3. All money is arbitrary-precision decimal — floating point is banned.
"""

__version__ = "0.16.0"

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
PLUGIN_API_VERSION = "0.7"
