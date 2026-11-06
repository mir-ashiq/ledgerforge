"""Law 1: total debits must equal total credits exactly — enforced by the
application AND by a deferred constraint trigger at commit time."""

from __future__ import annotations

import psycopg
import pytest
from conftest import create_ledger, post_voucher


def test_off_by_a_hundredth_of_a_cent_rejected(client):
    create_ledger(client, "Precision Sales", "Sales Accounts")
    before = client.get("/api/v1/vouchers?limit=500").json()
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "100.00000001"},
            {"ledger_name": "Precision Sales", "dc": "C", "amount": "100.00000000"},
        ],
    )
    assert r.status_code == 422
    assert "Unbalanced" in r.json()["detail"]
    after = client.get("/api/v1/vouchers?limit=500").json()
    assert len(after) == len(before)  # nothing was written


def test_single_entry_rejected(client):
    r = post_voucher(
        client,
        entries=[{"ledger_name": "Cash", "dc": "D", "amount": "10.00"}],
    )
    assert r.status_code == 422
    assert "at least 2" in r.json()["detail"]


def test_missing_ledger_rejected(client):
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "10.00"},
            {"ledger_name": "No Such Ledger", "dc": "C", "amount": "10.00"},
        ],
    )
    assert r.status_code == 404


def test_db_rejects_raw_sql_unbalanced_insert(psycopg_dsn):
    """Bypass the API entirely: the database itself must refuse an unbalanced
    voucher at COMMIT (deferred constraint trigger)."""
    with psycopg.connect(psycopg_dsn) as conn:
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT INTO vouchers (book_id, client_token, voucher_type_id,
                                  voucher_no, sequence, voucher_date)
            SELECT (SELECT id FROM books WHERE slug = 'default'), gen_random_uuid(), id,
                   'JV-DB-TEST-999999', 999999, CURRENT_DATE
            FROM voucher_types WHERE code = 'JV'
            """
        )
        vid = conn.execute("SELECT id FROM vouchers ORDER BY id DESC LIMIT 1").fetchone()[0]
        cash = conn.execute("SELECT id FROM ledgers WHERE name = 'Cash'").fetchone()[0]
        conn.execute(
            "INSERT INTO voucher_entries (voucher_id, line_no, ledger_id, book_id,"
            " dc, amount, voucher_date)"
            " VALUES (%s, 1, %s, (SELECT id FROM books WHERE slug = 'default'),"
            " 'D', 10, CURRENT_DATE), "
            "       (%s, 2, %s, (SELECT id FROM books WHERE slug = 'default'),"
            " 'C', 9.99999999, CURRENT_DATE)",
            (vid, cash, vid, cash),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="Unbalanced"):
            conn.commit()
        conn.rollback()
        # nothing survived
        n = conn.execute("SELECT count(*) FROM vouchers WHERE id = %s", (vid,)).fetchone()[0]
        assert n == 0


def test_db_rejects_single_line_voucher(psycopg_dsn):
    with psycopg.connect(psycopg_dsn) as conn:
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT INTO vouchers (book_id, client_token, voucher_type_id,
                                  voucher_no, sequence, voucher_date)
            SELECT (SELECT id FROM books WHERE slug = 'default'), gen_random_uuid(), id,
                   'JV-DB-TEST-999998', 999998, CURRENT_DATE
            FROM voucher_types WHERE code = 'JV'
            """
        )
        vid = conn.execute("SELECT id FROM vouchers ORDER BY id DESC LIMIT 1").fetchone()[0]
        cash = conn.execute("SELECT id FROM ledgers WHERE name = 'Cash'").fetchone()[0]
        conn.execute(
            "INSERT INTO voucher_entries (voucher_id, line_no, ledger_id, book_id,"
            " dc, amount, voucher_date)"
            " VALUES (%s, 1, %s, (SELECT id FROM books WHERE slug = 'default'),"
            " 'D', 10, CURRENT_DATE)",
            (vid, cash),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="at least 2 entries"):
            conn.commit()
        conn.rollback()
