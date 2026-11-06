# Contributing to LedgerForge

Thank you for helping build a community-owned business management engine. Before you start, please read **[docs/analysis.md](docs/analysis.md)** — especially §7, "The Three Financial Laws → Engineering Controls" — and the ADRs in [docs/adr/](docs/adr/). The financial laws are the project's contract:

1. **Debits = Credits, exactly, or the transaction aborts.**
2. **Financial history is append-only.** No `UPDATE`/`DELETE` on vouchers or entries — ever. Corrections are reversal vouchers.
3. **Exact decimals only.** No floats in money paths — not in Python, not in JSON, not in the database (`NUMERIC`, string wire format).

A pull request that bends a law (even "just this once", even "just for migration tooling") will be politely rejected.

## Development setup

```bash
git clone https://github.com/mir-ashiq/ledgerforge && cd ledgerforge
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# any PostgreSQL 16+; docker works:
docker compose up -d db
make init-db          # applies db/schema.sql + functions + Tally-style seed
make test             # 63 tests against a real PostgreSQL
make dev              # API on :8000, docs at /docs
```

Tests need a PostgreSQL they may create/drop scratch databases in:

```bash
export LF_TEST_ADMIN_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres
```

(No server handy? `pip install pgserver` and the suite boots an embedded one automatically.)

## Ground rules

- **Ruff clean**: `make lint` must pass (`ruff check . && ruff format --check .`).
- **Tests for behavior**: every bug fix ships with a test that fails without it. New SQL needs a test that proves the invariant it guards.
- **Conventional commits**: `feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`.
- **Schema changes**: see [ADR-006](docs/adr/006-migration-baseline.md) and the checklist below — the SQL files and the Alembic history must never drift apart (`tests/test_migrations.py` enforces this in CI).
- **Docs count**: architectural changes come with an ADR (`docs/adr/NNN-*.md`: Context / Decision / Consequences).
- **Plugins, not core edits**: if your feature is a tax rule, a dashboard, or a vertical workflow, it belongs in a plugin (see [docs/plugin-guide.md](docs/plugin-guide.md)).

### Changing the schema

Until 1.0, `db/schema.sql` + `db/functions.sql` stay the source of truth (keep
them idempotent) and the Alembic baseline is re-embedded:

```bash
# 1. edit db/schema.sql / db/functions.sql
make baseline-regen     # re-embeds them into db/migrations/versions/0001_baseline.py
make test               # the drift guard must stay green
```

After 1.0 the baseline freezes; a schema change ships as a new revision that
also updates the SQL files in the same PR, so fresh bootstraps and migrated
databases stay identical:

```bash
make migration-new rev=0002_add_periods m="add periods table"
# edit db/migrations/versions/0002_add_periods.py — handwritten raw SQL via op.execute()
# update db/schema.sql + db/functions.sql to the new flattened state
# include the ledgerforge_app GRANTs for any new tables
make test
```

## Reporting bugs

Open an issue with: PostgreSQL version, Python version, the exact API request/response (amounts as strings!), and what you expected. Financial-precision bugs (any case where `difference != 0`) are treated as security-grade priorities.

## Pull requests

Small, focused, single-purpose. Describe *what* and *why*; the diff shows *how*. For anything touching the posting engine, the reporting SQL, or the plugin API contract, expect review against the three laws and the relevant ADRs.

## Licensing

By contributing you agree your contributions are licensed under the repository's [AGPL-3.0](LICENSE).
