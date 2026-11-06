# Default local database. Override with `make X DATABASE_URL=...`
DATABASE_URL ?= postgresql+psycopg://postgres:postgres@localhost:5432/ledgerforge

.PHONY: dev test lint fmt init-db bench

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

## Benchmarks. With a local postgres:  make bench
## Without a running server (embedded via the pgserver package): make bench PGSERVER_DIR=/tmp/lf-bench
bench:
        python scripts/bench_hierarchy.py $(if $(PGSERVER_DIR),--pgserver-dir $(PGSERVER_DIR),--admin-dsn $(BENCH_ADMIN_DSN))
        python scripts/bench_posting.py $(if $(PGSERVER_DIR),--pgserver-dir $(PGSERVER_DIR),--admin-dsn $(BENCH_ADMIN_DSN))
