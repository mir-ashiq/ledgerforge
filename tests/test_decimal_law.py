"""Law 3: exact decimals only. Floats are banned on the wire; string amounts
split exactly where binary floating point would already have failed."""

from __future__ import annotations

from decimal import Decimal

from conftest import create_ledger, post_voucher


def test_float_rejected(client):
    """A JSON float has already lost precision — the API must refuse it."""
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": 0.1},  # float — banned
            {"ledger_name": "Cash", "dc": "C", "amount": "0.1"},
        ],
    )
    assert r.status_code == 422
    assert "string" in str(r.json()["detail"])


def test_scientific_and_garbage_rejected(client):
    for bad in ["abc", "1.2.3", "NaN", "Infinity", "-5.00", "0", ""]:
        r = post_voucher(
            client,
            entries=[
                {"ledger_name": "Cash", "dc": "D", "amount": "10.00"},
                {"ledger_name": "Cash", "dc": "C", "amount": bad},
            ],
        )
        assert r.status_code == 422, f"{bad!r} should be rejected: {r.text}"


def test_too_many_decimals_rejected(client):
    """NUMERIC(26,8) — more than 8dp would be silently rounded by PostgreSQL,
    so the application rejects it first."""
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "1.000000000"},
            {"ledger_name": "Cash", "dc": "C", "amount": "1.000000000"},
        ],
    )
    assert r.status_code == 422
    assert "decimal places" in str(r.json()["detail"])


def test_one_tenth_plus_two_tenths(client):
    """The classic float killer, as exact decimal strings: 0.1 + 0.2 == 0.3."""
    create_ledger(client, "Float Kill Sales", "Sales Accounts")
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "0.3"},
            {"ledger_name": "Float Kill Sales", "dc": "C", "amount": "0.1"},
            {"ledger_name": "Float Kill Sales", "dc": "C", "amount": "0.2"},
        ],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    amounts = {Decimal(e["amount"]) for e in body["entries"]}
    assert {Decimal("0.3"), Decimal("0.1"), Decimal("0.2")} <= amounts
    # amounts serialized as strings, never floats
    assert all(isinstance(e["amount"], str) for e in body["entries"])


def test_sub_cent_precision_exact(client):
    """Eight decimal places round-trip exactly."""
    create_ledger(client, "Micro Sales", "Sales Accounts")
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "0.12345678"},
            {"ledger_name": "Micro Sales", "dc": "C", "amount": "0.12345678"},
        ],
    )
    assert r.status_code == 201, r.text
    assert Decimal(r.json()["entries"][0]["amount"]) == Decimal("0.12345678")
