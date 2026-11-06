"""Database bootstrap: apply schema, functions and seed SQL files.

Used by the CLI (``python -m ledgerforge.bootstrap``), Docker entrypoint,
and the test suite. Idempotent — safe to re-run. After applying the SQL
files, stamps the Alembic version table to the current head (ADR-006) so
SQL-bootstrapped and alembic-migrated databases converge.
"""

from __future__ import annotations

import argparse
import pathlib

import psycopg

SQL_FILES = ("schema.sql", "functions.sql", "seed/chart_of_accounts.sql")

# db/ lives two levels above this file: src/ledgerforge/ -> src -> repo root
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def sql_file_paths(repo_root: pathlib.Path | None = None) -> list[pathlib.Path]:
    root = repo_root or REPO_ROOT
    db_dir = root / "db"
    if not db_dir.is_dir():  # installed package fallback: sql shipped next to module
        db_dir = pathlib.Path(__file__).resolve().parent / "sql"
        if not db_dir.is_dir():
            raise FileNotFoundError("Could not locate the db/ SQL directory")
    return [db_dir / rel for rel in SQL_FILES]


def apply_sql_files(dsn: str, repo_root: pathlib.Path | None = None, echo: bool = True) -> None:
    """Apply schema/functions/seed to ``dsn``. psycopg executes multi-statement
    scripts (including trigger functions with $$ bodies) in one call."""
    files = sql_file_paths(repo_root)
    with psycopg.connect(dsn) as conn:
        for path in files:
            if echo:
                print(f"applying {path.name} ...")
            conn.execute(path.read_text())
    _stamp_alembic_head(dsn, echo=echo)
    if echo:
        print("database bootstrap complete")


def _stamp_alembic_head(dsn: str, echo: bool = True) -> None:
    """Record the schema as being at the current Alembic head (ADR-006).

    The SQL files are the flattened head state, so a database bootstrapped
    here and one migrated with ``alembic upgrade head`` are interchangeable;
    later ``alembic upgrade`` runs must not re-apply old DDL. Skipped with a
    notice when the migrations tree is unavailable (e.g. trimmed installs).
    """
    try:
        from ledgerforge.migrations import stamp_schema

        head = stamp_schema(dsn)
    except (FileNotFoundError, ImportError, RuntimeError) as exc:
        if echo:
            print(f"alembic stamp skipped ({exc})")
        return
    if echo:
        print(f"alembic stamp: {head}")


def drop_schema(dsn: str, echo: bool = True) -> None:
    """Drop the LedgerForge schema objects entirely (destructive!)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS rpt CASCADE")
        # tables must drop in FK-safe order; triggers block nothing at DROP time
        conn.execute(
            """
            DROP TABLE IF EXISTS voucher_entries, vouchers, ledgers, groups,
                group_closure, voucher_types, doc_counters, audit_log, settings,
                fx_rates, currencies, periods
            CASCADE
            """
        )
    if echo:
        print("existing LedgerForge objects dropped")


def main() -> None:
    import os

    parser = argparse.ArgumentParser(description="Apply LedgerForge schema/seed to a database")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("LF_DATABASE_URL") or os.environ.get("DATABASE_URL", ""),
        help="libpq DSN or postgresql:// URL (default: LF_DATABASE_URL / DATABASE_URL)",
    )
    parser.add_argument("--drop", action="store_true", help="drop existing objects first")
    args = parser.parse_args()
    if not args.dsn:
        parser.error("no DSN given and LF_DATABASE_URL/DATABASE_URL is not set")
    if args.drop:
        drop_schema(args.dsn)
    apply_sql_files(args.dsn)


if __name__ == "__main__":
    main()
