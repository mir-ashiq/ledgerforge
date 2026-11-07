#!/bin/sh
# LedgerForge logical backup (v0.16, docs/deployment.md §backups).
#
# pg_dump in custom format + a sha256 sidecar, pruned to a retention window.
# Financial history is append-only, so a logical dump is a complete,
# point-in-time copy of the books — no incremental bookkeeping needed.
#
# Env:
#   LF_DATABASE_URL            postgres://user:pass@host/db (or the
#                              sqlalchemy form; the +psycopg suffix is
#                              stripped). Required.
#   LF_BACKUP_DIR              output directory (default ./backups)
#   LF_BACKUP_RETENTION_DAYS   prune dumps older than N days (default 14)
#
# Designed to run anywhere pg_dump exists — cron on the host, or the
# db-backup sidecar in docker-compose.prod.yml (loop mode: set
# LF_BACKUP_INTERVAL_SECONDS to loop; unset = run once and exit).
set -eu

: "${LF_DATABASE_URL:?LF_DATABASE_URL must point at the database, e.g. postgres://ledgerforge:...@db:5432/ledgerforge}"

BACKUP_DIR="${LF_BACKUP_DIR:-./backups}"
RETENTION_DAYS="${LF_BACKUP_RETENTION_DAYS:-14}"

# pg_dump wants a libpq URI; accept the sqlalchemy spelling too.
DSN=$(printf '%s' "$LF_DATABASE_URL" | sed 's#^postgresql+psycopg://#postgresql://#')

run_backup() {
  STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  DUMP="$BACKUP_DIR/ledgerforge-$STAMP.dump"
  echo "[backup] $DUMP"
  pg_dump --format=custom --file="$DUMP" --no-password "$DSN"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$DUMP" > "$DUMP.sha256"
    echo "[backup] $(cut -d' ' -f1 "$DUMP.sha256")  ($DUMP.sha256)"
  else
    echo "[backup] warning: sha256sum not found — no checksum written" >&2
  fi
}

mkdir -p "$BACKUP_DIR"
run_backup

# retention prune
echo "[backup] pruning dumps older than $RETENTION_DAYS days"
find "$BACKUP_DIR" -name 'ledgerforge-*.dump' -mtime "+$RETENTION_DAYS" -delete
find "$BACKUP_DIR" -name 'ledgerforge-*.dump.sha256' -mtime "+$RETENTION_DAYS" -delete
echo "[backup] done: $(find "$BACKUP_DIR" -name 'ledgerforge-*.dump' | wc -l | tr -d ' ') dump(s) retained"

# loop mode (the compose sidecar): sleep and repeat
if [ -n "${LF_BACKUP_INTERVAL_SECONDS:-}" ]; then
  echo "[backup] looping every ${LF_BACKUP_INTERVAL_SECONDS}s"
  while :; do
    sleep "$LF_BACKUP_INTERVAL_SECONDS"
    run_backup
    find "$BACKUP_DIR" -name 'ledgerforge-*.dump' -mtime "+$RETENTION_DAYS" -delete
    find "$BACKUP_DIR" -name 'ledgerforge-*.dump.sha256' -mtime "+$RETENTION_DAYS" -delete
  done
fi
