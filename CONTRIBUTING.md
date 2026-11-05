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
make test             # 39 tests against a real PostgreSQL
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
- **Schema changes**: `db/schema.sql` is the v0.x bootstrap; keep it idempotent (`IF NOT EXISTS` / `CREATE OR REPLACE`). Alembic arrives before the first post-1.0 schema change — don't fork the schema informally.
- **Docs count**: architectural changes come with an ADR (`docs/adr/NNN-*.md`: Context / Decision / Consequences).
- **Plugins, not core edits**: if your feature is a tax rule, a dashboard, or a vertical workflow, it belongs in a plugin (see [docs/plugin-guide.md](docs/plugin-guide.md)).

## Reporting bugs

Open an issue with: PostgreSQL version, Python version, the exact API request/response (amounts as strings!), and what you expected. Financial-precision bugs (any case where `difference != 0`) are treated as security-grade priorities.

## Pull requests

Small, focused, single-purpose. Describe *what* and *why*; the diff shows *how*. For anything touching the posting engine, the reporting SQL, or the plugin API contract, expect review against the three laws and the relevant ADRs.

## Licensing

By contributing you agree your contributions are licensed under the repository's [AGPL-3.0](LICENSE).
