"""The analyst surface (ADR-030, Phase 9): the ``views`` schema's seven
read-only projections, the ``ledgerforge_readonly`` role, and the analyst
seat (``POST /api/v1/ops/analyst-access``).

The promises under test:

- **The views answer and stay exact.** Every view selects; amounts are
  NUMERIC end to end (Law 3 through ODBC); ``ledger_balances`` agrees
  with the engine's own trial balance.
- **The contract is views-only.** ``ledgerforge_readonly`` holds SELECT
  on the views and nothing on the base tables.
- **The seat mints and rotates.** ``POST`` mints (or rotates) the
  ``lf_analyst`` LOGIN with a one-time password, granted the readonly
  role; ``DELETE`` drops it. A second mint rotates, never duplicates.
- The connection parameters ride the response — honest about what the
  deployment is.
"""

from __future__ import annotations

import psycopg
import pytest
from conftest import post_voucher

pytestmark = pytest.mark.usefixtures("fresh_db")


@pytest.fixture(scope="module")
def dx(client_module, fresh_db):
    client = client_module
    client.post("/api/v1/ledgers", json={"name": "Analyst Buyer", "group_name": "Sundry Debtors"})
    client.post("/api/v1/ledgers", json={"name": "Analyst Sales", "group_name": "Sales Accounts"})
    post_voucher(
        client,
        type_="SAL",
        date_="2026-04-05",
        entries=[
            {"ledger_name": "Analyst Buyer", "dc": "D", "amount": "450.00"},
            {"ledger_name": "Analyst Sales", "dc": "C", "amount": "450.00"},
        ],
    )
    return client


@pytest.fixture(scope="module")
def dsn(psycopg_dsn):
    return psycopg_dsn


def _view_rows(dsn: str, view: str) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(f"SELECT * FROM views.{view}").fetchall()


# -------------------------------------------------------------------- the views
def test_every_view_answers(dsn, dx):
    for view in (
        "books",
        "groups",
        "ledgers",
        "vouchers",
        "voucher_lines",
        "ledger_monthly",
        "ledger_balances",
    ):
        rows = _view_rows(dsn, view)
        assert isinstance(rows, list), view


def test_voucher_lines_carry_names_not_ids(dsn, dx):
    """The analyst's contract is flat: ledger, group, voucher number, cost
    centre — joinable without knowing the engine's ids."""
    rows = _view_rows(dsn, "voucher_lines")
    line = next(row for row in rows if row[_col(dsn, "voucher_lines", "ledger")] == "Analyst Buyer")
    group_at = _col(dsn, "voucher_lines", "group")
    dc_at = _col(dsn, "voucher_lines", "dc")
    assert line[group_at] == "Sundry Debtors"
    assert line[dc_at] == "D"


def _col(dsn: str, view: str, name: str) -> int:
    with psycopg.connect(dsn) as conn:
        cols = [c.name for c in conn.execute(f"SELECT * FROM views.{view} LIMIT 0").description]
    return cols.index(name)


def test_ledger_balances_agree_with_the_engine(dsn, dx):
    """The view's closing figures equal the trial balance's — one fold,
    two surfaces (the ADR-023 law, restated for SQL)."""
    tb = dx.get("/api/v1/reports/trial-balance").json()
    engine = {row["ledger_name"]: (row["dr"], row["cr"]) for row in tb["ledgers"]}
    rows = _view_rows(dsn, "ledger_balances")
    ledger_at = _col(dsn, "ledger_balances", "ledger")
    dr_at = _col(dsn, "ledger_balances", "closing_debit")
    cr_at = _col(dsn, "ledger_balances", "closing_credit")
    view = {row[ledger_at]: (str(row[dr_at]), str(row[cr_at])) for row in rows}
    for ledger, (dr, cr) in engine.items():
        if float(dr) == 0 and float(cr) == 0:
            continue  # the engine shows zero rows too; the view omits nothing
        assert ledger in view, f"{ledger} missing from the view"
        assert float(view[ledger][0]) == float(dr)
        assert float(view[ledger][1]) == float(cr)


def test_amounts_stay_numeric_end_to_end(dsn, dx):
    """NUMERIC, never float — the ODBC driver receives exact decimals."""
    with psycopg.connect(dsn) as conn:
        col = conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='views' AND table_name='voucher_lines' AND column_name='amount'"
        ).fetchone()
    assert col[0] == "numeric"


# --------------------------------------------------------------------- grants
def test_readonly_role_sees_views_not_tables(dsn, dx):
    with psycopg.connect(dsn) as conn:
        granted = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.role_table_grants "
            "WHERE grantee='ledgerforge_readonly' ORDER BY 1, 2"
        ).fetchall()
    schemas = {row[0] for row in granted}
    assert schemas == {"views"}  # and nothing in public — the base tables stay the engine's
    names = {row[1] for row in granted}
    assert {"vouchers", "voucher_lines", "ledger_balances"} <= names


# ------------------------------------------------------------------ the seat
def test_analyst_seat_mints_rotates_and_drops(dx, dsn):
    # a previous run may have left the seat behind — start honest
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP ROLE IF EXISTS lf_analyst")
    r = dx.post("/api/v1/ops/analyst-access")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "minted"
    assert body["role"] == "lf_analyst"
    assert "password" in body and body["password"]
    assert "connection" in body and "database" in body["connection"]
    assert "views" in body["sees"]

    with psycopg.connect(dsn) as conn:
        member = conn.execute(
            "SELECT r.rolname FROM pg_auth_members m "
            "JOIN pg_roles r ON r.oid = m.roleid "
            "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname='lf_analyst'"
        ).fetchall()
    assert [row[0] for row in member] == ["ledgerforge_readonly"]

    # the second mint rotates the password, never duplicates the seat
    rotated = dx.post("/api/v1/ops/analyst-access")
    assert rotated.status_code == 200
    assert rotated.json()["action"] == "rotated"
    assert rotated.json()["password"] != body["password"]

    dropped = dx.delete("/api/v1/ops/analyst-access")
    assert dropped.status_code == 200
    assert dropped.json()["action"] == "dropped"
    with psycopg.connect(dsn) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname='lf_analyst'").fetchone()
    assert exists is None

    # deleting an absent seat is a calm no-op
    assert dx.delete("/api/v1/ops/analyst-access").json()["action"] == "absent"
