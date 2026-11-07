# ADR-021: The desktop edition — a Windows application

- **Status:** Accepted (2026-11-07)
- **Deciders:** mir-ashiq
- **Relates to:** ADR-002 (append-only engine — the semantics that rule out
  SQLite), ADR-004 (keyboard-first frontend — reused unchanged), ADR-006
  (migration baseline — the desktop's upgrade path), ADR-016 (plugin trust —
  the drop-in gate, unchanged on the desktop), ADR-020 (the `LF_REQUIRE_AUTH`
  guard — deliberately not armed on the desktop), docs/deployment.md

## Context

The v0.16 close left LedgerForge a complete *server* product: Docker
Compose or a hand-run PostgreSQL + uvicorn + nginx stack, hardened by
ADR-020's guard, backups and monitoring runbook. That is an operator's
deployment. The product brief was always "inspired by TallyPrime" — and
TallyPrime's first impression is *download, double-click, start typing*.
The roadmap being closed, the natural next question was exactly that:
**a Windows application, released on GitHub.**

The constraints any answer had to respect:

1. **The three financial laws are PostgreSQL semantics.** Law 1's second
   line of defense is a `DEFERRABLE INITIALLY DEFERRED` constraint trigger;
   ADR-018 added yearly range partitions + BRIN; reports are `rpt.*`
   functions. An engine port to a "simpler" embedded database is a fork of
   the engine's truth.
2. **The UI is the keyboard contract** (ADR-004) — already browser-delivered,
   already proven at the browser tier by the v0.16 Playwright E2E.
3. **Zero config.** No database install, no service, no ports to open, no
   YAML. First run to first voucher in under a minute.
4. **No new trust surface.** ADR-016's gate and ADR-020's guard both exist
   for reasons; the desktop must not quietly disable the first or
   accidentally trip the second.

## Decision

Ship **LedgerForge Desktop**: the unmodified engine, an embedded
PostgreSQL, and the built web UI in one Windows application — launched by
`ledgerforge-desktop` / `LedgerForge.exe`, released per tag by the
`release-windows` workflow.

1. **Embedded PostgreSQL via pgserver.** The pgserver portable
   distribution ships full PostgreSQL binaries; the launcher
   (`ledgerforge.desktop.boot_database`) `initdb`s a cluster into
   `<data>/pgdata` on first run and adopts it thereafter
   (`cleanup_mode="stop"` stops the server at exit and *keeps* the data; a
   hard kill's stale `postmaster.pid` is recovered on the next boot). Law 1
   keeps its deferrable trigger, ADR-018 its partitions — the desktop is
   not a downgraded engine. SQLite was rejected precisely because of this:
   every PG-specific guarantee would become an `if sqlite else` fork.
2. **The browser is the UI, served same-origin.** `LF_STATIC_DIR` mounts a
   built frontend at `/` on the same uvicorn port as the API — no CORS, no
   dev proxy, no second process. No Electron, no Qt: the keyboard-first
   contract ships exactly as E2E-tested.
3. **Loopback-only and single-user by construction.** The desktop binds
   `127.0.0.1` and runs `LF_AUTH_MODE=disabled` — anonymous-admin over a
   port no other machine can reach. This is a design statement, not an
   oversight: ADR-020's `LF_REQUIRE_AUTH` guard stays armed for network
   deployments, and the desktop never listens beyond the loopback
   interface. (The console window is the app: logs visible, Ctrl+C to
   stop.)
4. **Data and upgrades.** Everything lives in one directory —
   `%LOCALAPPDATA%\LedgerForge` on Windows (`LF_DESKTOP_DATA` overrides;
   XDG/`~/Library` on the other platforms), containing `pgdata/` and
   `plugins/`. First boot: the SQL bootstrap (schema + functions + default
   book + Alembic stamp, ADR-006/009). Later boots on an existing
   database: `alembic upgrade head` — the seed never re-runs over live
   books. A new exe over an old data directory is the upgrade path.
5. **Drop-in plugins keep the trust gate.** The desktop sets
   `LF_PLUGIN_PATH` to `<data>/plugins`; ADR-016's verify-before-execute
   gate is unchanged. Landing a community plugin needs the same signature,
   approval or operator acknowledgement as on a server. (This surfaced a
   real bug: `LF_PLUGIN_PATH` was split on a hard-coded `:`, which tears
   Windows drive-letter paths in two — fixed to `os.pathsep`, regression-
   tested, POSIX behavior identical.)
6. **Packaging and release.** PyInstaller onedir (`desktop/ledgerforge.spec`)
   on a `windows-latest` runner: the SQL tree + Alembic revisions ride
   along at the package-relative fallback path, the built frontend as
   data, the ledgerforge dist metadata so entry-point plugin discovery
   still enumerates, and pgserver's binaries as the database. The
   **shipped exe must pass `LedgerForge.exe --smoke`** — boot the embedded
   database, provision the schema, answer `/health`, serve the web UI —
   before the workflow publishes the zip + SHA-256 checksums as the GitHub
   Release. The .exe's version resource is generated from
   `ledgerforge.__version__` (never hand-edited).

## Consequences

- **Size**: the application folder is dominated by PostgreSQL binaries
  (~100+ MB unpacked; ~50–70 MB zipped). The price of an un-downgraded
  engine, paid once per release.
- **Unsigned binary**: no code-signing certificate exists for the project;
  SmartScreen will warn on first run. Mitigation: published SHA-256
  checksums per release; signing is the obvious follow-up if the community
  funds a certificate.
- **First boot** pays `initdb` (seconds, once); later boots adopt the
  cluster and start quickly.
- **Single user, one book session at a time** — the desktop contract. The
  server deployment remains the answer for teams, and the two share every
  schema and migration.
- **Windows x64 is the shipped artifact**; the launcher itself is
  cross-platform (pgserver ships Linux/macOS binaries too — the test suite
  already exercises the full desktop boot path on Linux), and packaging
  those is future work, as are tray integration, auto-update and
  code signing.
- **AGPL-3.0**: the zip links the exact source tag; the quickstart and
  release notes carry the source offer.
