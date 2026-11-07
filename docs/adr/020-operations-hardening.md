# ADR-020: Operations hardening — the deep pass, closed

- **Status:** Accepted (2026-11-07)
- **Deciders:** mir-ashiq
- **Relates to:** ADR-016 (plugin trust — the rotation addendum), ADR-004
  (keyboard-first frontend — proven at the browser tier), ADR-018 (report
  scale-out — the ops surface this builds on), SECURITY.md, docs/deployment.md

## Context

After v0.15 the planned roadmap was fully landed and a deep pass over the
repository produced a short list of *deliberate* gaps — things the code
never promised but a production deployment eventually needs, each noted
and consciously deferred:

1. **The auth default was a warning, not a guard.** `LF_AUTH_MODE=disabled`
   logs a loud line and serves every caller as anonymous admin; a
   deployment that loses its OIDC configuration boots silently unsafe.
2. **Curator key rotation was manual** (ADR-016's accepted cost): re-sign
   index entries by hand, correctly, under pressure — the exact shape of
   task humans botch.
3. **Frontend coverage stopped at jsdom.** Components were proven; the
   assembled product — vite proxy, live engine, real PostgreSQL — never
   ran in CI, and "keyboard-first" had no test that a keyboard alone
   actually works in a browser.
4. **Deployment hardening was out of scope:** no backup story, no restore
   drill, no multi-worker guidance, nothing to monitor beyond a 200 from
   `/health`.

One item from the same pass dissolved under inspection: the "CI branch
filter typo" (`branches: ain]`) never existed in the repository — every
commit since the first carries `branches: [main]`; the mangled reading was
an artifact of a display pipeline that eats the two bytes `[m`. Verified
by blob SHAs across history before believing either view.

## Decision

Ship v0.16 as four small, independent mechanisms — each closes one gap
without touching the kernel, the schema, or the plugin API (stays 0.7).

### 1. The auth guard: `LF_REQUIRE_AUTH`

A boolean that inverts the failure mode. When true, `create_app()` refuses
to start unless `auth_mode` is `oidc` — a missing IdP configuration becomes
a boot failure with a remediation message, not a running anonymous-admin
instance. Dev default stays `false` (every test, sandbox and CI run boots
unchanged); `docker-compose.prod.yml` ships with it **on** and with
`${LF_OIDC_ISSUER:?}` required-variable syntax, so the guard cannot even
be reached without a real configuration. `/health` additionally reports
`auth_mode` so a monitor can alert on anything but `"oidc"`.

### 2. Rotation is tooled, not manual (ADR-016 addendum)

Two CLI commands, no format change:

- `ledgerforge plugins verify-index` — audits every entry against its
  artifact (sha256, signature, static manifest facts, API floor); the
  pre-flight before *and* after any index change.
- `ledgerforge plugins rotate-curator --new-key … [--old-key …]` —
  re-signs every entry onto the successor key **after verifying each
  existing signature**. Rotation is a re-attestation, never a republish:
  digests cannot move, a failing entry aborts the whole rewrite, and
  `--old-key` refuses a mixed-key index rather than adopting a stranger's
  entry. The runbook lives in `community/plugins/README.md`.

### 3. The browser tier: Playwright in CI

`frontend/web/e2e/keyboard-flow.spec.ts` runs the full keyboard flow in
real Chromium against the live stack — vite's `/api` proxy, the engine,
PostgreSQL — with **zero mouse input**: type-ahead picking, F5/F8 type
switches, the F2 dialog, `Ctrl+Enter` balance fill, `Ctrl+A` accept,
`Alt+B` daybook browse, `Esc` back, `Alt+C` mid-flight ledger creation.
`scripts/e2e_setup_db.py` builds a deterministic `lf_e2e` database (frozen
SQL files + seeded book + exactly one extra ledger); the CI `e2e` job and
`make e2e` run it. The jsdom suite keeps the component depth; this tier
proves the product.

### 4. The operations runbook: `ops/` + `docs/deployment.md`

- `ops/backup.sh` — pg_dump custom format + sha256 sidecar, retention
  pruning, loop mode for a compose sidecar.
- `ops/restore.sh` — the drill: checksum verify, restore into a scratch
  database, sanity counts, and the **Law-1 aggregate** (total debits ==
  total credits, exactly) as the acceptance criterion, then the real
  cut-over steps. A backup that was never restored is a hope, not a
  backup.
- `docker-compose.prod.yml` — the guard on, database unpublished,
  healthchecks and restart policies everywhere, a `db-backup` sidecar, no
  hardcoded credentials.
- `docs/deployment.md` — topology, first boot, OIDC, multi-worker tuning
  (stateless API, lock budgeting, when to partition per ADR-018),
  backup/restore/PITR, monitoring surfaces, upgrade procedure.

## What deliberately did *not* happen

- **No metrics endpoint.** Two JSON surfaces (`/health`,
  `GET /api/v1/ops/scale-out`) answer alerting today; a Prometheus
  exposition is a requirements-driven decision, not a default.
- **No secret management opinions.** The prod compose reads the
  environment; where those values come from (SOPS, Vault, a locked-down
  `.env`) is the operator's platform decision.
- **No schema or plugin API change.** Migration count stays at 0006;
  plugin API stays 0.7 — every pack loads unchanged.

## Consequences

- A production deployment now fails loudly at exactly the moments it used
  to fail silently: missing OIDC (boot), a broken index (rotation/audit),
  an untested product (CI), an unrestorable backup (the drill).
- CI grows a fourth job (`e2e`) that downloads Chromium once and runs one
  spec; the trade — minutes for proof that the keyboard contract survives
  integration — is the same trade every tier in this repository has made.
- Suite totals: backend 297 (288 + 4 guard tests + 5 rotation tests);
  frontend 99 jsdom specs + 1 browser spec. Every one of them green before
  the tag.
