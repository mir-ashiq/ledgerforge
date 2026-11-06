# Default local database. Override with `make X DATABASE_URL=...`
DATABASE_URL ?= postgresql+psycopg://postgres:postgres@localhost:5432/ledgerforge

.PHONY: dev test lint fmt init-db bench migrate migrate-stamp migration-new baseline-regen

## Start PostgreSQL (docker compose up -d db) then:
init-db:
        LF_DATABASE_URL=$(DATABASE_URL) python -m ledgerforge.bootstrap

dev:
        LF_DATABASE_URL=$(DATABASE_URL) uvicorn ledgerforge.main:app --reload

dev-ui:
        cd frontend/web && npm run dev   # http://localhost:5173, proxies /api to :8000

test:
        python -m pytest -v

lint:
        ruff check .
        ruff format --check .

fmt:
        ruff format .
        ruff check --fix .

## Migrations (ADR-006). schema.sql stays the source of truth pre-1.0;
## tests/test_migrations.py proves alembic head == the SQL files.
migrate:
        LF_DATABASE_URL=$(DATABASE_URL) alembic upgrade head

## Adopt migrations on a database bootstrapped from the SQL files (no re-run).
migrate-stamp:
        LF_DATABASE_URL=$(DATABASE_URL) alembic stamp head

## Add a migration (post-1.0 policy): make migration-new rev=0002_add_periods m="..."
migration-new:
        LF_DATABASE_URL=$(DATABASE_URL) alembic revision --rev-id $(rev) -m "$(m)"

## Pre-1.0 only: re-embed schema.sql + functions.sql into the baseline revision.
baseline-regen:
        python scripts/regen_baseline.py

## Benchmarks. With a local postgres:  make bench
## Without a running server (embedded via the pgserver package): make bench PGSERVER_DIR=/tmp/lf-bench
bench:
        python scripts/bench_hierarchy.py $(if $(PGSERVER_DIR),--pgserver-dir $(PGSERVER_DIR),--admin-dsn $(BENCH_ADMIN_DSN))
        python scripts/bench_posting.py $(if $(PGSERVER_DIR),--pgserver-dir $(PGSERVER_DIR),--admin-dsn $(BENCH_ADMIN_DSN))
