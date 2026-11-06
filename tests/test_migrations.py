"""ADR-006 in action: the Alembic baseline vs the SQL bootstrap path.

The drift guard: a database created by ``alembic upgrade head`` must be
structurally identical to one created by ``python -m ledgerforge.bootstrap``
(schema.sql + functions.sql + seed + stamp). The comparison is a catalog
fingerprint — schemas, extensions, tables/columns, indexes, constraints,
triggers, functions and the ledgerforge_app grants — so it needs no external
tools and runs anywhere the suite runs (pgserver or CI PostgreSQL).
"""

from __future__ import annotations

import difflib

import psycopg
import pytest
from alembic import command
from conftest import REPO_ROOT, _rebuild

from ledgerforge import migrations as lf_migrations

APP_ROLE = "ledgerforge_app"

# ----------------------------------------------------------------- queries
#: The fingerprint describes objects *we* own. Anything installed by an
#: extension (pg_depend deptype='e' — the same membership check pg_dump
#: uses, e.g. pg_trgm's ~31 functions in `public`) is the server's, not
#: ours: symmetric in the equality tests, invisible in the downgrade test.
_NOT_EXTENSION = """
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.classid = {classid} AND d.objid = {objid} AND d.deptype = 'e'
          )
"""


def _member_queries() -> tuple[tuple[str, str], ...]:
    ne = _NOT_EXTENSION
    return (
        (
            "schemas",
            "SELECT 'schema ' || nspname FROM pg_namespace"
            " WHERE nspname NOT LIKE 'pg%' AND nspname <> 'information_schema'"
            " ORDER BY nspname",
        ),
        ("extensions", "SELECT 'extension ' || extname FROM pg_extension ORDER BY extname"),
        (
            "columns",
            """
            SELECT format('%s.%s | %s | notnull=%s | default=%s | identity=%s | generated=%s',
                          c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
                          a.attnotnull,
                          COALESCE(pg_get_expr(ad.adbin, ad.adrelid), '-'),
                          COALESCE(NULLIF(a.attidentity, ''), '-'),
                          COALESCE(NULLIF(a.attgenerated, ''), '-'))
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a
              ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            LEFT JOIN pg_attrdef ad ON ad.adrelid = c.oid AND ad.adnum = a.attnum
            WHERE c.relkind IN ('r', 'S')
              AND n.nspname NOT LIKE 'pg%' AND n.nspname <> 'information_schema'"""
            + ne.format(classid="'pg_class'::regclass", objid="c.oid")
            + """
            ORDER BY c.relname, a.attnum
            """,
        ),
        (
            "indexes",
            """
            SELECT format('index %s %s', i.relname, pg_get_indexdef(i.oid))
            FROM pg_class i
            JOIN pg_namespace n ON n.oid = i.relnamespace
            WHERE i.relkind = 'i'
              AND n.nspname NOT LIKE 'pg%' AND n.nspname <> 'information_schema'"""
            + ne.format(classid="'pg_class'::regclass", objid="i.oid")
            + """
            ORDER BY i.relname
            """,
        ),
        (
            "constraints",
            """
            SELECT format('constraint %s %s %s', co.conrelid::regclass::text, co.conname,
                          pg_get_constraintdef(co.oid))
            FROM pg_constraint co
            WHERE co.connamespace NOT IN ('pg_catalog'::regnamespace,
                                          'information_schema'::regnamespace)"""
            + ne.format(classid="'pg_constraint'::regclass", objid="co.oid")
            + """
            ORDER BY 1
            """,
        ),
        (
            "triggers",
            """
            SELECT pg_get_triggerdef(t.oid)
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal
              AND n.nspname NOT LIKE 'pg%' AND n.nspname <> 'information_schema'"""
            + ne.format(classid="'pg_trigger'::regclass", objid="t.oid")
            + """
            ORDER BY t.tgname, c.relname
            """,
        ),
        (
            "functions",
            """
            SELECT pg_get_functiondef(p.oid)
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname NOT LIKE 'pg%' AND n.nspname <> 'information_schema'"""
            + ne.format(classid="'pg_proc'::regclass", objid="p.oid")
            + """
            ORDER BY p.oid::regprocedure::text
            """,
        ),
        (
            "role",
            f"SELECT 'role {APP_ROLE}' WHERE EXISTS"
            f" (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}')",
        ),
        (
            "grants",
            f"""
            SELECT 'table grant ' || table_schema || '.' || table_name || ' ' || privilege_type
            FROM information_schema.role_table_grants WHERE grantee = '{APP_ROLE}'
            UNION ALL
            SELECT 'routine grant ' || routine_schema || '.' || routine_name
                   || ' ' || privilege_type
            FROM information_schema.role_routine_grants WHERE grantee = '{APP_ROLE}'
            UNION ALL
            SELECT 'usage grant ' || object_type || ' ' || object_schema || '.'
                   || object_name || ' ' || privilege_type
            FROM information_schema.usage_privileges WHERE grantee = '{APP_ROLE}'
            ORDER BY 1
            """,
        ),
    )


_FINGERPRINT_QUERIES = _member_queries()


def fingerprint(dsn: str) -> list[str]:
    """A deterministic, sorted structural manifest of the database."""
    lines: list[str] = []
    with psycopg.connect(dsn) as conn:
        for _, query in _FINGERPRINT_QUERIES:
            for (row,) in conn.execute(query).fetchall():
                lines.append(" ".join(str(row).split()))
    return sorted(lines)


def alembic_version(dsn: str) -> str | None:
    """The stamped revision, or None when the bookkeeping table is absent."""
    try:
        with psycopg.connect(dsn) as conn:
            rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    except psycopg.errors.UndefinedTable:
        return None
    return rows[0][0] if rows else None


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def scratch_dbs(admin_dsn: str):
    """Factory for per-test scratch databases (dropped on teardown)."""
    created: list[str] = []

    def _make(name: str) -> str:
        dbname = f"lf_mig_{name}"
        dsn = _rebuild(admin_dsn, dbname)
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {dbname}")
        created.append(dbname)
        return dsn

    yield _make
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        for dbname in created:
            conn.execute(f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)")


# ------------------------------------------------------------------- helpers
def _alembic_cfg(dsn: str):
    cfg = lf_migrations.make_alembic_config()
    cfg.attributes["url"] = lf_migrations.normalize_url(dsn)
    return cfg


def _upgrade(dsn: str) -> None:
    command.upgrade(_alembic_cfg(dsn), "head")


def _downgrade(dsn: str, revision: str) -> None:
    command.downgrade(_alembic_cfg(dsn), revision)


def _sql_bootstrap(dsn: str) -> None:
    from ledgerforge import bootstrap

    bootstrap.apply_sql_files(dsn, REPO_ROOT, echo=False)


# --------------------------------------------------------------------- tests
def test_baseline_matches_bootstrap_sql(scratch_dbs):
    """The drift guard: `alembic upgrade head` == schema.sql + functions.sql."""
    alembic_dsn = scratch_dbs("alembic")
    sql_dsn = scratch_dbs("sql")
    _upgrade(alembic_dsn)
    _sql_bootstrap(sql_dsn)

    left = fingerprint(alembic_dsn)
    right = fingerprint(sql_dsn)
    if left != right:
        diff = "\n".join(
            difflib.unified_diff(
                left, right, "alembic upgrade head", "bootstrap SQL files", lineterm=""
            )
        )
        pytest.fail(
            "alembic upgrade head drifted from db/schema.sql + db/functions.sql.\n"
            "The baseline (0001) is frozen: ship the change as a NEW revision "
            "that also updates the SQL files in the same commit (ADR-006).\n\n" + diff
        )
    assert alembic_version(alembic_dsn) == alembic_version(sql_dsn) == lf_migrations.head_revision()


def test_bootstrap_stamps_head(scratch_dbs):
    """The bootstrap stamps alembic_version so both paths converge."""
    dsn = scratch_dbs("stamp")
    _sql_bootstrap(dsn)
    assert alembic_version(dsn) == lf_migrations.head_revision()

    # An upgrade on a stamped database is a no-op, not a re-run.
    _upgrade(dsn)
    assert alembic_version(dsn) == lf_migrations.head_revision()
    with psycopg.connect(dsn) as conn:
        (entries,) = conn.execute("SELECT count(*) FROM alembic_version").fetchone()
    assert entries == 1


def test_alembic_then_bootstrap_is_stable(scratch_dbs):
    """An alembic-migrated database survives a bootstrap re-run unchanged."""
    dsn = scratch_dbs("reboot")
    _upgrade(dsn)
    _sql_bootstrap(dsn)

    fresh = scratch_dbs("reboot_fresh")
    _sql_bootstrap(fresh)
    assert fingerprint(dsn) == fingerprint(fresh)


def test_head_revision_is_single_and_parseable():
    """The helper agrees with Alembic's own graph walk."""
    from alembic.script import ScriptDirectory

    head = lf_migrations.head_revision()
    assert head == "0004_multi_book"
    assert lf_migrations.revisions() == {
        "0001_baseline": None,
        "0002_multi_currency": "0001_baseline",
        "0003_period_locking": "0002_multi_currency",
        "0004_multi_book": "0003_period_locking",
    }
    scripts = ScriptDirectory(str(lf_migrations.migrations_dir()))
    assert scripts.get_current_head() == head


def test_stepwise_upgrade_and_partial_downgrade(scratch_dbs):
    """Each revision is independently reversible: stop at 0002 (currencies
    in, periods out), continue to head, step back one (periods dropped, FX
    columns survive), and land at head again."""
    dsn = scratch_dbs("stepwise")

    command.upgrade(_alembic_cfg(dsn), "0002_multi_currency")  # base -> 0002
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM currencies").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_name = 'voucher_entries'"
                " AND column_name IN ('currency', 'amount_fx', 'fx_rate')"
            ).fetchone()[0]
            == 3
        )
        assert conn.execute("SELECT to_regclass('periods')").fetchone()[0] is None

    _upgrade(dsn)
    _downgrade(dsn, "0002_multi_currency")
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT to_regclass('periods')").fetchone()[0] is None
        assert conn.execute("SELECT to_regclass('currencies')").fetchone()[0] is not None
        # the FYC-aware profit_and_loss was 0003's; stepping back restores the
        # baseline version (no voucher_types join inside)
        (has_join,) = conn.execute(
            "SELECT pg_get_functiondef('rpt.profit_and_loss(DATE, DATE)'::regprocedure)"
            " LIKE '%voucher_types%'"
        ).fetchone()
        assert has_join is False

    _upgrade(dsn)
    with psycopg.connect(dsn) as conn:
        # at head (0004) the P&L is book-scoped: (BIGINT, DATE, DATE)
        (has_join,) = conn.execute(
            "SELECT pg_get_functiondef('rpt.profit_and_loss(BIGINT, DATE, DATE)'::regprocedure)"
            " LIKE '%voucher_types%'"
        ).fetchone()
        assert has_join is True
        assert conn.execute("SELECT to_regclass('periods')").fetchone()[0] is not None


def test_downgrade_baseline_drops_all(scratch_dbs):
    """Downgrading to base removes every LedgerForge object (ADR-006 notes:
    pg_trgm and, best-effort, the app role may legitimately remain)."""
    dsn = scratch_dbs("downgrade")
    _upgrade(dsn)
    _downgrade(dsn, "base")

    leftovers = [
        line
        for line in fingerprint(dsn)
        if not line.startswith(
            (
                "extension ",
                "schema public",
                # alembic's emptied bookkeeping table is not ours to drop
                "alembic_version.",
                "index alembic_version",
                "constraint alembic_version ",
                "table grant public.alembic_version",
                # the role itself may survive on a shared cluster (see below);
                # only its grants in this database must be gone
                "role ledgerforge_app",
            )
        )
    ]
    assert leftovers == [], f"downgrade left objects behind: {leftovers}"

    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT to_regclass('rpt.trial_balance')").fetchone()[0] is None
        (tables,) = conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
            " AND tablename <> 'alembic_version'"
        ).fetchone()
        assert tables == 0
        # The role is dropped when nothing else references it; in a shared
        # test cluster other databases keep it alive — but never with grants
        # inside the downgraded one.
        (grants,) = conn.execute(
            "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = %s",
            (APP_ROLE,),
        ).fetchone()
        assert grants == 0
