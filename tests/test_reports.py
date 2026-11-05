"""Reporting invariants on a controlled scenario (fresh DB, posted once
per module by the `scenario` fixture):

scenario: sale 1000 (Cash/Local Sales), purchase 400 (Purchases A/c/Cash),
rent 100 (Rent/Cash) — gross profit 600, net profit 500, cash 500 Dr,
Trial Balance 1500/1500/0, Balance Sheet balances via the profit plug."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import create_ledger, post_voucher

AS_OF = date.today().isoformat()
YEAR_FROM = f"{date.today().year}-01-01"
YEAR_TO = f"{date.today().year}-12-31"


@pytest.fixture(scope="module")
def scenario(client_module, fresh_db):
    create_ledger(client_module, "Local Sales", "Sales Accounts")
    create_ledger(client_module, "Purchases A/c", "Purchase Accounts")
    create_ledger(client_module, "Rent", "Indirect Expenses")
    for entries in (
        [  # sale
            {"ledger_name": "Cash", "dc": "D", "amount": "1000.00"},
            {"ledger_name": "Local Sales", "dc": "C", "amount": "1000.00"},
        ],
        [  # purchase
            {"ledger_name": "Purchases A/c", "dc": "D", "amount": "400.00"},
            {"ledger_name": "Cash", "dc": "C", "amount": "400.00"},
        ],
        [  # rent paid
            {"ledger_name": "Rent", "dc": "D", "amount": "100.00"},
            {"ledger_name": "Cash", "dc": "C", "amount": "100.00"},
        ],
    ):
        r = post_voucher(client_module, entries=entries)
        assert r.status_code == 201, r.text
    return {}


def test_trial_balance_exact_zero_difference(client, scenario):
    tb = client.get(f"/api/v1/reports/trial-balance?as_of={AS_OF}").json()
    assert Decimal(tb["totals"]["dr"]) == Decimal("1500.00")
    assert Decimal(tb["totals"]["cr"]) == Decimal("1500.00")
    assert Decimal(tb["totals"]["difference"]) == 0

    by_ledger = {row["ledger_name"]: row for row in tb["ledgers"]}
    cash = by_ledger["Cash"]
    assert Decimal(cash["dr"]) == Decimal("1000.00")
    assert Decimal(cash["cr"]) == Decimal("500.00")
    assert cash["dc"] == "D" and Decimal(cash["balance"]) == Decimal("500.00")

    rollup = {g["group_name"]: Decimal(g["dr"]) for g in tb["groups"]}
    assert rollup["Current Assets"] == Decimal("1000.00")
    assert rollup["Purchase Accounts"] == Decimal("400.00")


def test_profit_and_loss_gross_and_net(client, scenario):
    pl = client.get(f"/api/v1/reports/profit-loss?from={YEAR_FROM}&to={YEAR_TO}").json()
    assert Decimal(pl["trading"]["gross_profit"]) == Decimal("600.00")
    assert Decimal(pl["profit_and_loss"]["net_profit"]) == Decimal("500.00")

    direct_income = {g["group_name"]: Decimal(g["amount"]) for g in pl["trading"]["direct_incomes"]}
    assert direct_income["Sales Accounts"] == Decimal("1000.00")
    direct_expense = {
        g["group_name"]: Decimal(g["amount"]) for g in pl["trading"]["direct_expenses"]
    }
    assert direct_expense["Purchase Accounts"] == Decimal("400.00")
    indirect = {
        g["group_name"]: Decimal(g["amount"]) for g in pl["profit_and_loss"]["indirect_expenses"]
    }
    assert indirect["Indirect Expenses"] == Decimal("100.00")


def test_balance_sheet_balances_via_profit_plug(client, scenario):
    bs = client.get(f"/api/v1/reports/balance-sheet?as_of={AS_OF}").json()
    assert Decimal(bs["assets"]["total"]) == Decimal("500.00")  # net cash
    assert Decimal(bs["liabilities"]["total"]) == Decimal("0")
    assert Decimal(bs["equity"]["net_profit_to_date"]) == Decimal("500.00")
    assert Decimal(bs["totals"]["assets"]) == Decimal(bs["totals"]["liabilities_and_equity"])
    assert Decimal(bs["totals"]["difference"]) == 0

    assets_by_group = {g["group_name"]: Decimal(g["amount"]) for g in bs["assets"]["groups"]}
    assert assets_by_group["Cash-in-Hand"] == Decimal("500.00")
    assert assets_by_group["Current Assets"] == Decimal("500.00")


def test_reports_as_of_date_respected(client, scenario):
    """A voucher posted 30 days ago is invisible at a 31-days-ago cutoff —
    and at its own date it appears alongside only the Cash ledger."""
    create_ledger(client, "Dated Sales", "Sales Accounts")
    cutoff = (date.today() - timedelta(days=31)).isoformat()
    posted_on = (date.today() - timedelta(days=30)).isoformat()

    r = post_voucher(
        client,
        date_=posted_on,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "10.00"},
            {"ledger_name": "Dated Sales", "dc": "C", "amount": "10.00"},
        ],
    )
    assert r.status_code == 201, r.text

    tb_before = client.get(f"/api/v1/reports/trial-balance?as_of={cutoff}").json()
    assert tb_before["ledgers"] == []  # the scenario is dated today — nothing yet

    tb_at = client.get(f"/api/v1/reports/trial-balance?as_of={posted_on}").json()
    assert {row["ledger_name"] for row in tb_at["ledgers"]} == {"Cash", "Dated Sales"}
