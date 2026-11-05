# ADR-003: Plugin architecture — in-process hooks inside the transaction, events after it

- **Status:** Accepted (2026-11-06)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

A worldwide community must build tax packs (GST/VAT/sales tax), dashboards, and vertical workflows without touching core. Tax computation must participate in the posting transaction so the double-entry law holds at commit. Full analysis: `docs/analysis.md` §5.

## Decision

- **Two tiers.** In-process plugins (trusted Python via entry-points `ledgerforge.plugins` or `LF_PLUGIN_PATH` directories) run transformers/validators **inside** the posting transaction. Out-of-process integrations subscribe to **domain events** (`voucher.posted`, `master.created`) after commit via webhooks/queues.
- **Versioned surface.** Plugins code against `ledgerforge.plugin_api` (semver) and register exclusively through `PluginRegistry`; manifests declare `api_compat` so incompatible plugins fail loudly at load.
- **Hook taxonomy (v0.1):** `register_transformer(fn)`, `register_validator(fn)`, `on(event, handler)`.
- **Failure policy.** Transformer/validator exceptions abort the whole posting (a plugin may veto, never corrupt). Event-handler exceptions are logged and isolated.
- **UI slots** are manifest-declared; sandboxed slots register commands with the host rather than receiving keyboard scope.

## Consequences

- **(+)** Tax localization packs can extend posting with full Law-1 protection.
- **(+))** Odoo-style drop-in folder experience + pip-installable entry points.
- **(−)** In-process plugins are trusted code — mitigated on the roadmap by a signed plugin index and capability scoping.
- **(−)** Core discipline: every new hook is API surface and needs semver governance.

## Alternatives rejected

- **Everything out-of-process:** cannot run tax math transactionally; latency and consistency failures.
- **Monkey-patching (`_inherit`-style):** silent coupling, plugin-vs-plugin breakage (Odoo's scars).
- **Metadata-only frameworks (DocType-style):** hides the accounting kernel behind indirection at the exact layer where auditability matters most.
