"""Fresh database for the browser E2E (frontend/web/e2e, CI job ``e2e``).

Creates ``lf_e2e`` from scratch: the frozen SQL files, the seeded default
book (ADR-009), and the one ledger the keyboard flow needs that the
system chart does not carry — ``E2E Sales`` under ``Sales Accounts`` —
so the voucher screen's type-ahead sees exactly two candidates ("Cash",
"E2E Sales") and the test stays deterministic.

Usage (sandbox / local, embedded postgres via the pgserver package)::

    python scripts/e2e_setup_db.py

Usage (CI, service container)::

    python scripts/e2e_setup_db.py --admin-url postgresql://postgres:postgres@localhost:5432/postgres
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import urllib.parse

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

E2E_DBNAME = "lf_e2e"
E2E_LEDGER = "E2E Sales"
E2E_LEDGER_GROUP = "Sales Accounts"


def _psycopg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _rebuild(admin_dsn: str) -> str:
    """Drop + recreate the E2E database; returns its DSN."""
    import psycopg

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {E2E_DBNAME} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {E2E_DBNAME}")
    parts = urllib.parse.urlsplit(admin_dsn)
    return urllib.parse.urlunsplit(parts._replace(path=f"/{E2E_DBNAME}"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--admin-url",
        default=None,
        help="postgres superuser DSN (default: LF_TEST_ADMIN_URL, else an "
        "embedded pgserver at /tmp/lf-e2e-pgdata)",
    )
    args = parser.parse_args()

    admin_url = args.admin_url
    if not admin_url:
        import os

        admin_url = os.environ.get("LF_TEST_ADMIN_URL")
    if not admin_url:
        import pgserver

        admin_url = pgserver.get_server("/tmp/lf-e2e-pgdata").get_uri()
    admin_url = _psycopg_url(admin_url)

    dsn = _rebuild(admin_url)

    from ledgerforge.bootstrap import apply_sql_files

    apply_sql_files(dsn, repo_root=REPO_ROOT)  # psycopg-style DSN

    # The one non-system ledger the flow types against.
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from ledgerforge.models import Book, Group, Ledger
    from ledgerforge.seeding import DEFAULT_BOOK_SLUG

    sqlalchemy_url = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(sqlalchemy_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            book = session.scalar(select(Book).where(Book.slug == DEFAULT_BOOK_SLUG))
            group = session.scalar(
                select(Group).where(Group.book_id == book.id, Group.name == E2E_LEDGER_GROUP)
            )
            session.add(Ledger(book_id=book.id, name=E2E_LEDGER, group_id=group.id))
            session.commit()
            print(f"book {book.id} ({book.slug}): ledger '{E2E_LEDGER}' under '{E2E_LEDGER_GROUP}'")
    finally:
        engine.dispose()

    print(f"E2E database ready: {E2E_DBNAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
