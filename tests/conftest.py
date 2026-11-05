"""Test fixtures: a real PostgreSQL per session (pgserver locally, or
LF_TEST_ADMIN_URL in CI), fresh schema per module where isolation matters,
and small helpers used across the suite."""

from __future__ import annotations

import os
import pathlib
import urllib.parse
from datetime import date

import psycopg
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST_DBNAME = "lf_test"
TODAY = date.today().isoformat()


def _as_psycopg(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _rebuild(db_uri: str, dbname: str) -> str:
    parts = urllib.parse.urlsplit(db_uri)
    return urllib.parse.urlunsplit(parts._replace(path=f"/{dbname}"))


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    env = os.environ.get("LF_TEST_ADMIN_URL")
    if env:
        return _as_psycopg(env)
    import pgserver

    return pgserver.get_server("/tmp/lf-pgdata").get_uri()


@pytest.fixture(scope="session")
def psycopg_dsn(admin_dsn: str) -> str:
    """Fresh test database with schema/functions/seed applied."""
    dsn = _rebuild(admin_dsn, TEST_DBNAME)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DBNAME} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEST_DBNAME}")
    from ledgerforge import bootstrap

    bootstrap.apply_sql_files(dsn, REPO_ROOT, echo=False)
    return dsn


@pytest.fixture(scope="session")
def sqlalchemy_url(psycopg_dsn: str) -> str:
    url = psycopg_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    os.environ["LF_DATABASE_URL"] = url
    return url


@pytest.fixture(scope="module")
def fresh_db(psycopg_dsn: str) -> str:
    """Full schema reset for modules that assert on global state
    (numbering sequences, report totals)."""
    from ledgerforge import bootstrap

    with psycopg.connect(psycopg_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    bootstrap.apply_sql_files(psycopg_dsn, REPO_ROOT, echo=False)
    return psycopg_dsn


@pytest.fixture()
def app(sqlalchemy_url: str):
    from ledgerforge import db
    from ledgerforge.main import create_app
    from ledgerforge.settings import get_settings

    get_settings.cache_clear()
    db.reset_engine()
    return create_app()


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def app_module(sqlalchemy_url):
    from ledgerforge import db
    from ledgerforge.main import create_app
    from ledgerforge.settings import get_settings

    get_settings.cache_clear()
    db.reset_engine()
    return create_app()


@pytest.fixture(scope="module")
def client_module(app_module):
    from fastapi.testclient import TestClient

    with TestClient(app_module) as c:
        yield c


@pytest.fixture()
def db_session(sqlalchemy_url: str):
    from ledgerforge import db

    db.reset_engine()
    session = db.session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def plugin_manager():
    from ledgerforge.contrib import example_gst
    from ledgerforge.plugins import PluginManager

    mgr = PluginManager()
    mgr.load_module(example_gst)
    return mgr


@pytest.fixture()
def settings(sqlalchemy_url: str):
    from ledgerforge.settings import get_settings

    get_settings.cache_clear()
    return get_settings()


# ------------------------------------------------------------- small helpers
def create_ledger(client, name: str, group_name: str) -> dict:
    r = client.post("/api/v1/ledgers", json={"name": name, "group_name": group_name})
    assert r.status_code == 201, r.text
    return r.json()


def post_voucher(
    client,
    *,
    type_: str = "SAL",
    date_: str | None = None,
    entries: list[dict],
    meta: dict | None = None,
    token=None,
    narration: str | None = None,
):
    payload = {
        "voucher_type": type_,
        "voucher_date": date_ or TODAY,
        "entries": entries,
    }
    if meta is not None:
        payload["meta"] = meta
    if token is not None:
        payload["client_token"] = str(token)
    if narration is not None:
        payload["narration"] = narration
    return client.post("/api/v1/vouchers", json=payload)
