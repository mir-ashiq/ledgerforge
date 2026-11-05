"""Law 2: financial history is append-only. Triggers block UPDATE/DELETE and
the application role lacks the privileges entirely — defense in depth."""

from __future__ import annotations

import psycopg
import pytest
from conftest import create_ledger, post_voucher


@pytest.fixture(scope="module")
def one_voucher(client_module):
    create_ledger(client_module, "Immutable Sales", "Sales Accounts")
    r = post_voucher(
        client_module,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "500.00"},
            {"ledger_name": "Immutable Sales", "dc": "C", "amount": "500.00"},
        ],
    )
    assert r.status_code == 201, r.text
    return r.json()


def _entry_id(psycopg_dsn, voucher_id: int) -> int:
    with psycopg.connect(psycopg_dsn) as conn:
        return conn.execute(
            "SELECT id FROM voucher_entries WHERE voucher_id = %s ORDER BY line_no LIMIT 1",
            (voucher_id,),
        ).fetchone()[0]


def test_update_voucher_raises(psycopg_dsn, one_voucher):
    vid = one_voucher["id"]
    with psycopg.connect(psycopg_dsn) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("UPDATE vouchers SET narration = 'hack' WHERE id = %s", (vid,))
        conn.rollback()


def test_delete_voucher_raises(psycopg_dsn, one_voucher):
    vid = one_voucher["id"]
    with psycopg.connect(psycopg_dsn) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM vouchers WHERE id = %s", (vid,))
        conn.rollback()


def test_update_entry_raises(psycopg_dsn, one_voucher):
    eid = _entry_id(psycopg_dsn, one_voucher["id"])
    with psycopg.connect(psycopg_dsn) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("UPDATE voucher_entries SET amount = 1 WHERE id = %s", (eid,))
        conn.rollback()


def test_delete_entry_raises(psycopg_dsn, one_voucher):
    eid = _entry_id(psycopg_dsn, one_voucher["id"])
    with psycopg.connect(psycopg_dsn) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM voucher_entries WHERE id = %s", (eid,))
        conn.rollback()


def test_audit_log_is_append_only(psycopg_dsn):
    with psycopg.connect(psycopg_dsn) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM audit_log WHERE true")
        conn.rollback()


def test_app_role_cannot_update_financial_tables(psycopg_dsn, one_voucher):
    """Privilege-level defense: even without the trigger, the application
    role simply lacks UPDATE/DELETE on financial history."""
    with psycopg.connect(psycopg_dsn) as conn:
        vid = one_voucher["id"]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SET ROLE ledgerforge_app")
            conn.execute("UPDATE vouchers SET narration = 'hack' WHERE id = %s", (vid,))
        conn.rollback()  # resets role too
        # read access still works for the app role
        conn.execute("SET ROLE ledgerforge_app")
        n = conn.execute("SELECT count(*) FROM vouchers").fetchone()[0]
        conn.execute("RESET ROLE")
        assert n >= 1


def test_master_changes_are_audited(psycopg_dsn):
    with psycopg.connect(psycopg_dsn) as conn:
        n = conn.execute(
            "SELECT count(*) FROM audit_log WHERE table_name = 'ledgers' AND operation = 'INSERT'"
        ).fetchone()[0]
    assert n >= 1  # the ledgers created by the fixtures are journaled
