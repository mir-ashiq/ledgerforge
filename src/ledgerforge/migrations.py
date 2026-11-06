"""Alembic glue: locate the migrations tree, resolve the head revision,
and run migrations from any install (``python -m ledgerforge.migrations``).

The database bootstrap (``python -m ledgerforge.bootstrap``) stamps the
``alembic_version`` table to the current head after applying the SQL files,
so a database provisioned via SQL and one provisioned via ``alembic upgrade
head`` are interchangeable (ADR-006, tests/test_migrations.py).
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_REV_RE = re.compile(r"^revision\s*[:=]\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_DOWN_RE = re.compile(r"^down_revision\s*[:=]\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
#: Alembic's version table — stable public contract (see stamp_schema()).
VERSION_TABLE = "alembic_version"


def migrations_dir() -> pathlib.Path:
    """The db/migrations tree: repo layout, or the wheel-shipped copy."""
    repo = REPO_ROOT / "db" / "migrations"
    if repo.is_dir():
        return repo
    shipped = pathlib.Path(__file__).resolve().parent / "sql" / "migrations"
    if shipped.is_dir():
        return shipped
    raise FileNotFoundError("could not locate db/migrations (repo or shipped sql/)")


def revisions() -> dict[str, str | None]:
    """All revision ids mapped to their parent (None = no parent)."""
    versions = migrations_dir() / "versions"
    found: dict[str, str | None] = {}
    for path in sorted(versions.glob("*.py")):
        text = path.read_text()
        rev = _REV_RE.search(text)
        down = _DOWN_RE.search(text)
        if rev and down:
            found[rev.group(1)] = down.group(1)
        elif rev:  # baseline-style file: down_revision = None
            found[rev.group(1)] = None
    if not found:
        raise RuntimeError(f"no Alembic revisions found under {versions}")
    return found


def head_revision() -> str:
    """The single head of the revision graph (what `upgrade head` reaches)."""
    revs = revisions()
    referenced = {parent for parent in revs.values() if parent is not None}
    heads = [rev for rev in revs if rev not in referenced]
    if len(heads) != 1:
        raise RuntimeError(
            f"expected exactly one Alembic head, found {heads!r} — "
            "merge or fix revisions under db/migrations/versions/"
        )
    return heads[0]


def stamp_schema(dsn: str) -> str:
    """Mark the database as being at the current head revision.

    Called by the bootstrap after the SQL files are applied: they *are* the
    flattened head state, so future `alembic upgrade` runs must not re-apply
    old DDL. Idempotent; re-stamps to head on every bootstrap.
    """
    import psycopg

    head = head_revision()
    if len(head) > 32:
        raise RuntimeError(f"revision id {head!r} exceeds alembic's VARCHAR(32)")
    with psycopg.connect(dsn) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {VERSION_TABLE} (
                version_num VARCHAR(32) NOT NULL,
                CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
            )
            """
        )
        conn.execute(f"DELETE FROM {VERSION_TABLE}")
        conn.execute(f"INSERT INTO {VERSION_TABLE} (version_num) VALUES (%s)", (head,))
        # The app role must never hold privileges on the bookkeeping table.
        # On a database that already ran alembic or an earlier bootstrap, the
        # blanket `GRANT ... ON ALL TABLES` in schema.sql will have caught it
        # (the table then pre-exists the grant); mirror the baseline's REVOKE
        # so both provisioning paths converge (tests/test_migrations.py).
        conn.execute(f"REVOKE ALL ON {VERSION_TABLE} FROM ledgerforge_app")
    return head


def normalize_url(url: str) -> str:
    """Accept both postgresql:// and postgresql+psycopg:// spellings."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def resolve_url() -> str:
    url = os.environ.get("LF_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("no database URL: set LF_DATABASE_URL (or DATABASE_URL)")
    return normalize_url(url)


def make_alembic_config() -> object:
    """A programmatic Config pointing at our script tree (no ini needed)."""
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir()))
    return cfg


def main(argv: list[str] | None = None) -> int:
    """Minimal front end: upgrade / downgrade / stamp / current / heads / history."""
    import alembic.command as command

    parser = argparse.ArgumentParser(
        prog="python -m ledgerforge.migrations", description="Run LedgerForge DB migrations"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("heads", help="show the head revision")
    sub.add_parser("current", help="show this database's revision")
    sub.add_parser("history", help="show the revision history")
    p_upgrade = sub.add_parser("upgrade", help="apply migrations (default: head)")
    p_upgrade.add_argument("revision", nargs="?", default="head")
    p_downgrade = sub.add_parser("downgrade", help="revert migrations (e.g. -1, base)")
    p_downgrade.add_argument("revision")
    p_stamp = sub.add_parser("stamp", help="record a revision as applied without running it")
    p_stamp.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args(argv)

    cfg = make_alembic_config()
    cfg.attributes["url"] = resolve_url()

    if args.cmd == "upgrade":
        command.upgrade(cfg, args.revision)
    elif args.cmd == "downgrade":
        command.downgrade(cfg, args.revision)
    elif args.cmd == "stamp":
        command.stamp(cfg, args.revision)
    elif args.cmd == "current":
        command.current(cfg, verbose=True)
    elif args.cmd == "heads":
        command.heads(cfg, verbose=True)
    elif args.cmd == "history":
        command.history(cfg, verbose=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
