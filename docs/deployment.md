# Deploying LedgerForge in production

The repository ships a development posture by default (`docker-compose.yml`,
`LF_AUTH_MODE=disabled`, anonymous admin). This runbook is the production
counterpart: what to change, what to watch, and how to prove the books come
back. The hardened stack lives in
[`docker-compose.prod.yml`](../docker-compose.prod.yml); the operational
scripts in [`ops/`](../ops/).

## 1. Topology

```
            ┌────────────┐       ┌───────────────┐
  users ──▶ │  web (nginx) │ ──▶ │ api ×N        │   stateless workers;
            │  :8080       │     │ uvicorn :8000 │   all locks live in
            └────────────┘       └──────┬────────┘   PostgreSQL
                                        ▼
                                 ┌────────────┐     ┌────────────┐
                                 │ db :5432   │ ◀── │ db-backup  │
                                 │ (unpublished)│    │ pg_dump loop│
                                 └────────────┘     └────────────┘
```

- **web** serves the built UI and proxies `/api` to the engine — the browser
  origin is the only origin (no CORS surface unless you configure one).
- **api** is stateless: vouchers post through PostgreSQL transactions,
  period locks are advisory locks *in the database* (ADR-008), and plugin
  event tiers run after commit. Scale it with
  `docker compose -f docker-compose.prod.yml up -d --scale api=3`.
- **db** is never published to the host; only the compose network reaches
  it. The application connects as a role with SELECT + INSERT-only rights
  on financial history (see `db/schema.sql`) — keep it that way.
- **db-backup** runs `ops/backup.sh` on a loop (default daily, 14-day
  retention) into the `lf-backups` volume.

TLS terminates in front of `web` (any reverse proxy or load balancer);
bearer tokens must never cross a network in clear text.

## 2. First boot

```bash
cp .env.example .env 2>/dev/null || true   # or write the variables below
#   LF_DB_USER, LF_DB_PASSWORD            — database credentials
#   LF_OIDC_ISSUER, LF_OIDC_AUDIENCE      — your identity provider
docker compose -f docker-compose.prod.yml up -d
```

The api container bootstraps on start: `db/schema.sql` + `functions.sql`
(idempotent), the seeded default book, and an Alembic stamp to the current
head (ADR-006). On an *existing* database prefer explicit migrations:

```bash
LF_DATABASE_URL=… alembic upgrade head     # the transactional path
```

## 3. Authentication — the guard

`LF_AUTH_MODE=disabled` is a development default and stays one; production
sets **`LF_REQUIRE_AUTH=true`** (the prod compose file does), which turns a
missing OIDC configuration into a **boot failure** instead of a silent
anonymous-admin instance (v0.16, `SECURITY.md`).

Configure your IdP (Keycloak, Auth0, …) so tokens carry:

- `roles` (or Keycloak's `realm_access.roles`): `reader` < `bookkeeper` <
  `admin` — every route enforces its minimum role;
- the `books` claim for multi-book deployments: `["*"]` or a list of book
  ids (ADR-009).

`GET /health` reports `auth_mode`; monitoring should alert on anything
other than `"oidc"`.

## 4. Multi-worker tuning

| Knob | Guidance |
|---|---|
| api replicas | start at 2 behind the nginx DNS round-robin; the engine is stateless |
| SQLAlchemy pool | default 5 connections per worker process — budget `replicas × pool` against PostgreSQL `max_connections` (raise it or add pgbouncer for large fleets) |
| shm on db | the prod file sets `shm_size: 256mb`; raise with workload |
| partitions | `ledgerforge scale-out status` — when entries pass millions, `scale-out partition` converts the table yearly (maintenance window, ADR-018) |
| balance cache | `ledgerforge scale-out refresh-cache` after period closes; `?fast=true` reports then read the cache |

The event tier (webhooks, the IRN bridge) runs per api process; a plugin
that must be a singleton should advisory-lock its work, exactly like
`rpt.refresh_balance_cache` does.

## 5. Backups and restore drills

Financial history is append-only, so a logical dump is a complete
point-in-time copy. `ops/backup.sh` (pg_dump custom format + sha256
sidecar, retention-pruned) runs from cron or the compose sidecar; both
databases and volumes are covered by the same script.

```bash
LF_DATABASE_URL=postgres://… ops/backup.sh          # one dump now
LF_BACKUP_DIR=/backups LF_BACKUP_INTERVAL_SECONDS=86400 ops/backup.sh   # loop
```

**A backup that was never restored is a hope.** Run the drill — it
verifies the checksum, restores into a scratch database, and asserts the
books' integrity, including Law 1 (total debits == total credits, exactly):

```bash
LF_ADMIN_URL=postgres://… ops/restore.sh backups/ledgerforge-20261107T092321Z.dump
```

Output ends with `The drill passed` plus the real cut-over runbook (stop
API → final backup → restore → verify `/health` → spot-check the daybook
and a zero trial-balance difference). Schedule the drill quarterly; the
real cut-over is the same procedure in a maintenance window.

For point-in-time recovery beyond dump granularity, run PostgreSQL with
WAL archiving (`archive_mode`, `archive_command`) — the dump schedule and
the WAL stream are complementary, not alternatives.

## 6. Monitoring

- **`GET /health`** — `{"status", "version", "auth_mode"}` plus a live
  `SELECT 1`. Alert on: non-200, `auth_mode != "oidc"`, and version drift
  across replicas after a rollout.
- **`GET /api/v1/ops/scale-out`** (admin) — entries shape, partition
  count, balance-cache state per book (ADR-018). A growing
  `cache_rows`/`horizon` gap means refreshes are not keeping up.
- **Logs** — uvicorn access logs plus the `ledgerforge.*` loggers; plugin
  boot failures are logged, never fatal, and every refused drop-in names
  the reason (`GET /api/v1/plugins`, admin).
- **PostgreSQL** — connections against `max_connections`, `pg_stat_activity`
  lock waits (period locks are advisory and visible in
  `pg_locks` with `locktype = 'advisory'`).

There is deliberately no metrics endpoint yet: poll the two JSON surfaces
above until a real requirements-driven case lands (ADR-020).

## 7. Upgrades

1. Read the release notes; every release ships an ADR for behavior changes.
2. Take a backup (`ops/backup.sh`) — the rollback of last resort is a
   restore, drilled in §5.
3. `alembic upgrade head` (migrations are transactional; ADR-006), or let
   a single-boot api container bootstrap-stamp on an already-current
   database.
4. Roll api replicas one at a time (`--no-deps --scale api=2 up -d api`
   patterns); verify `/health` version between waves.
5. After a financial-year close, refresh the balance cache
   (`ledgerforge scale-out refresh-cache`).

## 8. Where the depth lives

| Topic | Document |
|---|---|
| Security checklist, threat model | [`SECURITY.md`](../SECURITY.md) |
| Schema, migrations, the drift guard | [`docs/database-design.md`](database-design.md), ADR-006 |
| Period locking (audit windows) | ADR-008 |
| Report scale-out (partitions, cache) | ADR-018, `ledgerforge scale-out --help` |
| Plugins: trust, install, rotation | [`docs/plugin-guide.md`](plugin-guide.md), ADR-016, [`community/plugins/README.md`](../community/plugins/README.md) |
| The product decisions, numbered | [`docs/adr/`](adr/) |
