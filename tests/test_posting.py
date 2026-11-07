"""Posting engine behavior: happy path, idempotency, numbering, reversal,
opening balances. Uses a fresh database (module fixture) for deterministic
numbering."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from uuid import uuid4

from conftest import create_ledger, post_voucher

VOUCHER_NO_RE = re.compile(r"^SAL-\d{4}-\d{2}-\d{5}$")


def test_post_and_read_back(client, fresh_db):
    create_ledger(client, "Readable Sales", "Sales Accounts")
    token = uuid4()
    r = post_voucher(
        client,
        token=token,
        narration="first sale",
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "1250.50"},
            {"ledger_name": "Readable Sales", "dc": "C", "amount": "1250.50"},
        ],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert VOUCHER_NO_RE.match(body["voucher_no"]), body["voucher_no"]
    assert body["narration"] == "first sale"
    assert body["replayed"] is False
    assert body["client_token"] == str(token)
    assert {e["dc"] for e in body["entries"]} == {"D", "C"}
    assert all(isinstance(e["amount"], str) for e in body["entries"])

    fetched = client.get(f"/api/v1/vouchers/{body['id']}").json()
    assert fetched["voucher_no"] == body["voucher_no"]
    assert len(fetched["entries"]) == 2


def test_voucher_amount_is_engine_computed(client, fresh_db):
    """The daybook's amount column (ADR-015): every VoucherOut carries the
    debit-side total, quantized to cents by the engine — the client never
    sums (Law 3). Storage keeps 8 decimals; the field shows cents."""
    create_ledger(client, "Amount Sales", "Sales Accounts")
    r = post_voucher(
        client,
        narration="split sale",
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "1250.50"},
            {"ledger_name": "Amount Sales", "dc": "C", "amount": "1000.00"},
            {"ledger_name": "Amount Sales", "dc": "C", "amount": "250.50"},
        ],
    )
    assert r.status_code == 201, r.text
    assert r.json()["amount"] == "1250.50"

    listed = client.get("/api/v1/vouchers?limit=10").json()
    assert [v["amount"] for v in listed if v["id"] == r.json()["id"]] == ["1250.50"]


def test_idempotent_replay_returns_original(client, fresh_db):
    create_ledger(client, "Replay Sales", "Sales Accounts")
    token = uuid4()
    entries = [
        {"ledger_name": "Cash", "dc": "D", "amount": "99.99"},
        {"ledger_name": "Replay Sales", "dc": "C", "amount": "99.99"},
    ]
    before = client.get("/api/v1/vouchers?limit=500").json()
    first = post_voucher(client, token=token, entries=entries)
    assert first.status_code == 201
    second = post_voucher(client, token=token, entries=entries)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["id"] == first.json()["id"]

    after = client.get("/api/v1/vouchers?limit=500").json()
    assert len(after) == len(before) + 1  # no duplicate from the replay
    assert sum(1 for v in after if v["client_token"] == str(token)) == 1


def test_gapless_sequential_numbering(client, fresh_db):
    create_ledger(client, "Seq Sales", "Sales Accounts")
    nos = []
    for i in range(5):
        amount = f"{10 + i}.00"
        r = post_voucher(
            client,
            entries=[
                {"ledger_name": "Cash", "dc": "D", "amount": amount},
                {"ledger_name": "Seq Sales", "dc": "C", "amount": amount},
            ],
        )
        assert r.status_code == 201, r.text
        nos.append(r.json()["voucher_no"])
    assert len(set(nos)) == 5
    assert nos == sorted(nos)  # monotonic


def test_reversal_nets_to_zero_and_original_is_intact(client, fresh_db):
    create_ledger(client, "Reversal Sales", "Sales Accounts")
    tb_before = client.get("/api/v1/reports/trial-balance").json()
    cash_before = next(
        (row for row in tb_before["ledgers"] if row["ledger_name"] == "Cash"),
        {"dr": "0.00", "cr": "0.00"},
    )

    original = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "400.00"},
            {"ledger_name": "Reversal Sales", "dc": "C", "amount": "400.00"},
        ],
    ).json()

    r = client.post(f"/api/v1/vouchers/{original['id']}/reverse", json={})
    assert r.status_code == 201, r.text
    reversal = r.json()
    assert reversal["reversal_of"] == original["id"]

    # original untouched
    original_after = client.get(f"/api/v1/vouchers/{original['id']}").json()
    assert original_after["entries"] == original["entries"]

    # mirror: debits and credits swapped
    original_map = {(e["ledger_id"], e["dc"]): e["amount"] for e in original["entries"]}
    for e in reversal["entries"]:
        mirrored_dc = "C" if e["dc"] == "D" else "D"
        assert original_map[(e["ledger_id"], mirrored_dc)] == e["amount"]

    # the net effect on Cash of the pair is exactly zero
    tb = client.get("/api/v1/reports/trial-balance").json()
    assert Decimal(tb["totals"]["difference"]) == 0
    cash = next(row for row in tb["ledgers"] if row["ledger_name"] == "Cash")
    assert Decimal(cash["dr"]) == Decimal(cash_before["dr"]) + Decimal("400.00")
    assert Decimal(cash["cr"]) == Decimal(cash_before["cr"]) + Decimal("400.00")

    # reversing twice is refused
    r2 = client.post(f"/api/v1/vouchers/{original['id']}/reverse", json={})
    assert r2.status_code == 409


def test_opening_balances_post_as_balanced_journal(client, fresh_db):
    create_ledger(client, "Owner Capital", "Capital Account")
    r = client.post(
        "/api/v1/opening-balances",
        json={
            "voucher_date": str(date.today()),
            "entries": [
                {"ledger_name": "Cash", "dc": "D", "amount": "50000.00"},
                {"ledger_name": "Owner Capital", "dc": "C", "amount": "50000.00"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["voucher_type"] == "OPEN"
    tb = client.get("/api/v1/reports/trial-balance").json()
    assert Decimal(tb["totals"]["difference"]) == 0


def test_opening_balances_must_balance(client, fresh_db):
    r = client.post(
        "/api/v1/opening-balances",
        json={
            "voucher_date": str(date.today()),
            "entries": [
                {"ledger_name": "Cash", "dc": "D", "amount": "100.00"},
                {"ledger_name": "Profit & Loss A/c", "dc": "C", "amount": "90.00"},
            ],
        },
    )
    assert r.status_code == 422
    assert "Unbalanced" in r.json()["detail"]
