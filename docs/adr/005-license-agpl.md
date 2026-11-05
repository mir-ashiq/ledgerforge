# ADR-005: License — AGPL-3.0 for the core, open boundary for plugins

- **Status:** Accepted (2026-11-06)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

LedgerForge is community-driven and open source, but business management software is a category where hosted/closed forks of open commons are common. The license must protect the commons while keeping a healthy plugin ecosystem possible. Verified precedents (GitHub API, 2026-11-06): ERPNext = GPL-3.0 (~41k stars), Firefly III = AGPL-3.0 (~25k stars), Odoo Community = LGPL-3.0.

## Decision

The core repository is licensed **GNU AGPL-3.0**.

**Rationale:** AGPL-3.0's network clause (§13) requires operators of hosted derivatives to share their modifications with the users they serve — closing the "SaaS loophole" that GPL-3.0 leaves open. For a community accounting engine whose moat *is* the community, that protection matters more than the marginal adoption friction versus permissive licenses.

**Plugin boundary (engineering guidance, not legal advice):** plugins interact with the core exclusively through the versioned, documented `PluginRegistry` API at arm's length, and may carry any license their authors choose — mirroring how Odoo's LGPL-3.0 core enables proprietary modules. Commercial redistributors should seek their own counsel.

## Consequences

- **(+)** Hosted derivatives must contribute back; the commons cannot be enclosed.
- **(+)** Precedent: Firefly III demonstrates a healthy AGPL-3.0 finance community.
- **(−)** Some enterprises exclude AGPL from procurement; for self-hosted community software this is an accepted trade.
- **(−)** GPL-3.0 (ERPNext's choice) would be marginally more familiar in this domain; rejected for the SaaS loophole.
