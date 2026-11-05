# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x (main) | security fixes only |

## Reporting a vulnerability

**Do not open a public issue for security problems.**

Email **imsrenpsycho@gmail.com** with details and reproduction steps. You will receive an acknowledgement within 7 days. Please avoid public disclosure until a fix is released; we will credit reporters in the release notes unless you prefer otherwise.

## Threat model notes

- The engine is **append-only by construction**: triggers and database privileges block `UPDATE`/`DELETE` on financial history (see [docs/analysis.md §7](docs/analysis.md)). Treat any bypass of these guards as a critical report.
- Decimal handling is exact end-to-end; any report where the Trial Balance `difference` is not exactly `0` is a critical bug.
- **Authentication is not yet implemented** — do not expose a LedgerForge instance to untrusted networks before AuthN/AuthZ lands (see the roadmap).
- Plugins run in-process with full engine privileges: install plugins only from sources you trust (see [docs/plugin-guide.md](docs/plugin-guide.md)).
