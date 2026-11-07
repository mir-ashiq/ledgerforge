#!/bin/sh
# LedgerForge restore drill (v0.16, docs/deployment.md §backups).
#
# A backup that was never restored is a hope, not a backup. This script
# performs the drill: verify the checksum, restore the dump into a SCRATCH
# database (never over the live one), run sanity checks — including the
# Law-1 aggregate (total debits == total credits, exactly) — and print the
# cut-over runbook. The drill database is dropped afterwards unless
# --keep is given.
#
# Usage:
#   ops/restore.sh <dumpfile> [--keep]
#
# Env:
#   LF_ADMIN_URL   superuser DSN for creating the scratch database,
#                  e.g. postgres://postgres:...@localhost:5432/postgres
#                  (default: LF_DATABASE_URL's maintenance database)
set -eu

DUMP="${1:?usage: restore.sh <dumpfile> [--keep]}"
KEEP="${2:-}"
[ -f "$DUMP" ] || { echo "error: no such dump: $DUMP" >&2; exit 1; }

ADMIN_URL="${LF_ADMIN_URL:-${LF_DATABASE_URL:?need LF_ADMIN_URL or LF_DATABASE_URL}}"
ADMIN_URL=$(printf '%s' "$ADMIN_URL" | sed 's#^postgresql+psycopg://#postgresql://#')
SCRATCH_DB="${LF_RESTORE_SCRATCH_DB:-lf_restore_drill}"

# 1. checksum
if [ -f "$DUMP.sha256" ] && command -v sha256sum >/dev/null 2>&1; then
  echo "[restore] verifying checksum"
  (cd "$(dirname "$DUMP")" && sha256sum -c "$(basename "$DUMP").sha256")
else
  echo "[restore] warning: no checksum sidecar — restoring unverified bytes" >&2
fi

# 2. fresh scratch database
psql "$ADMIN_URL" --set=ON_ERROR_STOP=1 --quiet <<SQL
DROP DATABASE IF EXISTS $SCRATCH_DB WITH (FORCE);
CREATE DATABASE $SCRATCH_DB;
SQL

# scratch URL = admin URL with its path swapped (query string preserved)
BASE_URL=$(printf '%s' "$ADMIN_URL" | sed 's#\?.*##')
QUERY_PART=$(printf '%s' "$ADMIN_URL" | sed 's#^[^?]*##')
SCRATCH_URL="${BASE_URL%/*}/$SCRATCH_DB$QUERY_PART"

# 3. restore
echo "[restore] pg_restore -> $SCRATCH_DB"
pg_restore --dbname="$SCRATCH_URL" --no-owner --no-privileges --jobs=4 "$DUMP"

# 4. sanity checks — the drill's acceptance criteria
echo "[restore] sanity checks:"
psql "$SCRATCH_URL" --set=ON_ERROR_STOP=1 --quiet <<SQL
SELECT 'books      ' AS check, count(*)::text AS value FROM books
UNION ALL SELECT 'vouchers   ', count(*)::text FROM vouchers
UNION ALL SELECT 'entries    ', count(*)::text FROM voucher_entries
UNION ALL SELECT 'law1     ', CASE WHEN coalesce(sum(CASE WHEN dc = 'D' THEN amount ELSE -amount END), 0) = 0
  THEN 'debits == credits (exact)' ELSE 'VIOLATION' END
  FROM voucher_entries;
SQL

LAW1=$(psql "$SCRATCH_URL" --tuples-only --quiet --no-align \
  -c "SELECT CASE WHEN coalesce(sum(CASE WHEN dc = 'D' THEN amount ELSE -amount END), 0) = 0 THEN 'ok' ELSE 'FAIL' END FROM voucher_entries")
[ "$LAW1" = "ok" ] || {
  echo "error: Law 1 violated in the restored copy — dump is corrupt or partial" >&2
  exit 1
}
echo "[restore] Law 1 holds: total debits == total credits, exactly"

# 5. drop the scratch copy (or keep it for inspection)
if [ "$KEEP" = "--keep" ]; then
  echo "[restore] kept $SCRATCH_DB for inspection (drop it with: DROP DATABASE $SCRATCH_DB)"
else
  psql "$ADMIN_URL" --set=ON_ERROR_STOP=1 --quiet -c "DROP DATABASE $SCRATCH_DB WITH (FORCE)"
  echo "[restore] scratch database dropped"
fi

cat <<'RUNBOOK'

The drill passed. Real cut-over (a maintenance window, in this order):

  1. stop the API (docker compose stop api) — no new vouchers during cutover
  2. take one final backup            ops/backup.sh
  3. drop + recreate the live DB, restore into it
       pg_restore --dbname=<live-url> --no-owner --no-privileges <final.dump>
  4. restart the API and verify       curl -fsS <api>/health
  5. spot-check the daybook (Alt+B) and the trial balance difference == 0

RUNBOOK
