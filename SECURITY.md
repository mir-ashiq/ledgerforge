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
- **Authentication (v0.3):** `LF_AUTH_MODE=oidc` verifies `Authorization: Bearer` JWTs against the configured OIDC issuer (JWKS, key-cache with rotation refresh) and enforces the `reader` < `bookkeeper` < `admin` role model on every route. The default `LF_AUTH_MODE=disabled` is **development only** — every caller is an anonymous admin and a warning is logged at startup; do not expose such an instance to untrusted networks. See the Authentication section of the README. Per-book permissions land with the multi-book schema.
- Plugins run in-process with full engine privileges: install plugins only from sources you trust (see [docs/plugin-guide.md](docs/plugin-guide.md)).

## Production deployment checklist (v0.16)

The dev defaults are deliberately unsafe for production. Before exposing an
instance beyond localhost:

1. **Set `LF_REQUIRE_AUTH=true`.** The app then refuses to boot unless
   `LF_AUTH_MODE=oidc` is fully configured — a lost OIDC configuration is a
   boot failure, never a silent anonymous-admin instance
   (`docker-compose.prod.yml` ships with the guard on).
2. **Configure real OIDC**: `LF_AUTH_MODE=oidc`, `LF_OIDC_ISSUER`,
   `LF_OIDC_AUDIENCE` (and `LF_OIDC_JWKS_URI` if discovery is blocked).
   Issue tokens carrying `roles` (or Keycloak's `realm_access.roles`) and,
   for multi-book, the `books` claim.
3. **Monitor `/health`** — it reports `status`, `version` and `auth_mode`;
   alert on anything but `{"status": "ok"}` and on `auth_mode != "oidc"`
   (see [docs/deployment.md](docs/deployment.md) for the full runbook:
   backups, restore drills, multi-worker tuning, upgrades).
4. **Restrict the database**: the app role is `ledgerforge_app`
   (SELECT + INSERT-only on financial history); never run the API as the
   PostgreSQL superuser. Do not publish port 5432 beyond the deployment
   network.
5. **Terminate TLS in front** (nginx, the shipped container does this for
   the UI) and keep the engine port private — bearer tokens must never
   cross the network in plain text.
