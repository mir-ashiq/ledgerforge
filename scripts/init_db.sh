#!/usr/bin/env sh
# Apply the LedgerForge schema, reporting functions and the default-book
# seed (ledgerforge/seeding.py via bootstrap) to a database.
# Idempotent — safe to re-run. DSN resolution: LF_DATABASE_URL > DATABASE_URL.
set -eu

: "${LF_DATABASE_URL:=${DATABASE_URL:-}}"

if [ -z "${LF_DATABASE_URL}" ]; then
    echo "error: set LF_DATABASE_URL (or DATABASE_URL)" >&2
    echo "  e.g. LF_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/ledgerforge" >&2
    exit 1
fi

python -m ledgerforge.bootstrap "$@"
