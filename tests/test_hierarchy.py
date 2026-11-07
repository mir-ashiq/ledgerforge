"""ADR-001 in action: the adjacency+closure hierarchy — seed integrity,
rollups through the tree, subtree moves, cycle rejection."""

from __future__ import annotations

from decimal import Decimal

import psycopg
from conftest import create_ledger, post_voucher

EXPECTED_GROUPS = {
    "Primary",
    "Capital Account",
    "Loans (Liability)",
    "Current Liabilities",
    "Fixed Assets",
    "Investments",
    "Current Assets",
    "Branch / Divisions",
    "Misc. Expenses (Asset)",
    "Suspense A/c",
    "Sales Accounts",
    "Purchase Accounts",
    "Direct Incomes",
    "Indirect Incomes",
    "Direct Expenses",
    "Indirect Expenses",
    "Reserves & Surplus",
    "Bank OD A/c",
    "Secured Loans",
    "Unsecured Loans",
    "Duties & Taxes",
    "Provisions",
    "Sundry Creditors",
    "Bank Accounts",
    "Cash-in-Hand",
    "Deposits (Asset)",
    "Loans & Advances (Asset)",
    "Stock-in-Hand",
    "Sundry Debtors",
}


def test_predefined_groups_loaded(client):
    # pin the default book once more than one exists (the same discipline as
    # the pack suites — a module earlier in the run may have created one)
    books = client.get("/api/v1/books").json()
    headers = (
        {"X-Book-Id": str(next(b["id"] for b in books if b["slug"] == "default"))}
        if len(books) > 1
        else None
    )
    groups = client.get("/api/v1/groups", headers=headers).json()
    assert {g["name"] for g in groups} >= EXPECTED_GROUPS
    natures = {g["name"]: g["nature"] for g in groups}
    assert natures["Sales Accounts"] == "income"  # P&L side
    assert natures["Sundry Debtors"] == "asset"
    assert natures["Duties & Taxes"] == "liability"
    gross = {g["name"]: g["affects_gross"] for g in groups}
    assert gross["Sales Accounts"] is True
    assert gross["Indirect Expenses"] is False


def test_nested_group_rollup(client, fresh_db):
    """A ledger three levels deep rolls up through every ancestor group."""
    r = client.post(
        "/api/v1/groups", json={"name": "Chennai Branch", "parent_name": "Sundry Debtors"}
    )
    assert r.status_code == 201, r.text
    create_ledger(client, "Chennai Traders", "Chennai Branch")
    create_ledger(client, "Local Sales", "Sales Accounts")
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Chennai Traders", "dc": "D", "amount": "750.00"},
            {"ledger_name": "Local Sales", "dc": "C", "amount": "750.00"},
        ],
    )
    assert r.status_code == 201, r.text
    tb = client.get("/api/v1/reports/trial-balance").json()
    rollup = {g["group_name"]: Decimal(g["dr"]) for g in tb["groups"]}
    assert rollup["Chennai Branch"] == Decimal("750.00")
    assert rollup["Sundry Debtors"] == Decimal("750.00")
    assert rollup["Current Assets"] == Decimal("750.00")
    assert rollup["Primary"] == Decimal("750.00")


def test_subtree_move_updates_rollups(client, fresh_db):
    create_ledger(client, "Movable Sales", "Sales Accounts")
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "100.00"},
            {"ledger_name": "Movable Sales", "dc": "C", "amount": "100.00"},
        ],
    )
    assert r.status_code == 201, r.text

    sales = next(g for g in client.get("/api/v1/groups").json() if g["name"] == "Sales Accounts")
    fixed = next(g for g in client.get("/api/v1/groups").json() if g["name"] == "Fixed Assets")

    r = client.patch(f"/api/v1/groups/{sales['id']}", json={"parent_id": fixed["id"]})
    assert r.status_code == 200, r.text

    tb = client.get("/api/v1/reports/trial-balance").json()
    rollup = {g["group_name"]: Decimal(g["cr"]) for g in tb["groups"]}
    # the entire Sales Accounts subtree (750 from test_nested_group_rollup +
    # this test's 100) now rolls up under Fixed Assets, which previously
    # had a zero balance
    assert rollup["Fixed Assets"] == Decimal("850.00")
    assert rollup["Sales Accounts"] == Decimal("850.00")

    # restore the tree for other suites
    primary = next(g for g in client.get("/api/v1/groups").json() if g["name"] == "Primary")
    r = client.patch(f"/api/v1/groups/{sales['id']}", json={"parent_id": primary["id"]})
    assert r.status_code == 200, r.text


def test_cycle_rejected(client, fresh_db):
    current = next(g for g in client.get("/api/v1/groups").json() if g["name"] == "Current Assets")
    debtors = next(g for g in client.get("/api/v1/groups").json() if g["name"] == "Sundry Debtors")
    r = client.patch(f"/api/v1/groups/{current['id']}", json={"parent_id": debtors["id"]})
    assert r.status_code == 422
    assert "Cycle" in r.json()["detail"]


def test_closure_depths_correct(psycopg_dsn):
    with psycopg.connect(psycopg_dsn) as conn:
        rows = conn.execute(
            """
            SELECT ancestor.name, depth FROM group_closure c
            JOIN groups ancestor ON ancestor.id = c.ancestor_id
            JOIN groups d ON d.id = c.descendant_id
            WHERE d.name = 'Provisions'
            ORDER BY depth
            """
        ).fetchall()
    assert [(name, depth) for name, depth in rows] == [
        ("Provisions", 0),
        ("Current Liabilities", 1),
        ("Primary", 2),
    ]
