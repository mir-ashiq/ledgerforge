"""Report shapers over ``rpt.*`` SQL functions (db/functions.sql).

The SQL functions do all aggregation; this layer composes responses and —
crucially — asserts the engine's own balance invariants (a Trial Balance
whose difference is not exactly zero, or a Balance Sheet that does not
balance, raises InvariantViolation and is never returned to a client).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..errors import InvariantViolation


def _fmt(d: Decimal) -> str:
    return format(d, "f")


def trial_balance(session: Session, as_of: date) -> dict:
    rows = (
        session.execute(text("SELECT * FROM rpt.trial_balance(:as_of)"), {"as_of": as_of})
        .mappings()
        .all()
    )

    ledgers = [
        {
            "ledger_id": r.out_ledger_id,
            "ledger_name": r.out_ledger_name,
            "group_id": r.out_group_id,
            "group_name": r.out_group_name,
            "nature": r.out_nature,
            "dr": _fmt(r.out_dr),
            "cr": _fmt(r.out_cr),
            "balance": _fmt(r.out_balance),
            "dc": r.out_dc,
        }
        for r in rows
    ]
    total_dr = sum((r.out_dr for r in rows), Decimal(0))
    total_cr = sum((r.out_cr for r in rows), Decimal(0))
    difference = total_dr - total_cr
    if difference != 0:  # the engine proves itself on every report
        raise InvariantViolation(f"Trial balance difference is {difference}, expected exactly 0")
    return {
        "as_of": as_of,
        "ledgers": ledgers,
        "totals": {"dr": _fmt(total_dr), "cr": _fmt(total_cr), "difference": _fmt(difference)},
    }


def group_rollups(session: Session, as_of: date) -> list[dict]:
    rows = (
        session.execute(text("SELECT * FROM rpt.group_rollups(:as_of)"), {"as_of": as_of})
        .mappings()
        .all()
    )
    return [
        {
            "group_id": r.out_group_id,
            "group_name": r.out_group_name,
            "parent_id": r.out_parent_id,
            "nature": r.out_nature,
            "dr": _fmt(r.out_dr),
            "cr": _fmt(r.out_cr),
            "net": _fmt(r.out_net),
        }
        for r in rows
    ]


def profit_loss(session: Session, d_from: date, d_to: date) -> dict:
    rows = (
        session.execute(
            text("SELECT * FROM rpt.profit_and_loss(:d_from, :d_to)"),
            {"d_from": d_from, "d_to": d_to},
        )
        .mappings()
        .all()
    )

    def groups_for(bucket: str) -> list[dict]:
        return [
            {
                "group_id": r.out_group_id,
                "group_name": r.out_group_name,
                "amount": _fmt(r.out_amount),
            }
            for r in rows
            if r.out_bucket == bucket and r.out_group_id is not None
        ]

    def value_of(bucket: str) -> Decimal:
        return next(
            (r.out_amount for r in rows if r.out_bucket == bucket and r.out_group_id is None),
            Decimal(0),
        )

    return {
        "from": d_from,
        "to": d_to,
        "trading": {
            "direct_incomes": groups_for("direct_income"),
            "direct_expenses": groups_for("direct_expense"),
            "gross_profit": _fmt(value_of("gross_profit")),
        },
        "profit_and_loss": {
            "indirect_incomes": groups_for("indirect_income"),
            "indirect_expenses": groups_for("indirect_expense"),
            "net_profit": _fmt(value_of("net_profit")),
        },
    }


def balance_sheet(session: Session, as_of: date) -> dict:
    # Ledger-level classification for authoritative section totals (each
    # ledger counted exactly once, by its own group's nature).
    tb_rows = (
        session.execute(text("SELECT * FROM rpt.trial_balance(:as_of)"), {"as_of": as_of})
        .mappings()
        .all()
    )

    def section(nature: str, debit_positive: bool) -> Decimal:
        total = Decimal(0)
        for r in tb_rows:
            if r.out_nature != nature:
                continue
            total += (r.out_dr - r.out_cr) if debit_positive else (r.out_cr - r.out_dr)
        return total

    assets = section("asset", debit_positive=True)
    liabilities = section("liability", debit_positive=False)
    equity = section("equity", debit_positive=False)
    profit = section("income", debit_positive=False) - section("expense", debit_positive=True)

    if assets != liabilities + equity + profit:
        raise InvariantViolation(
            f"Balance sheet does not balance: assets {_fmt(assets)} != "
            f"liabilities {_fmt(liabilities)} + equity {_fmt(equity)} + profit {_fmt(profit)}"
        )

    # Group display tree from the SQL function (zero lines filtered out).
    group_rows = (
        session.execute(text("SELECT * FROM rpt.balance_sheet(:as_of)"), {"as_of": as_of})
        .mappings()
        .all()
    )
    sections: dict[str, list[dict]] = {"assets": [], "liabilities": [], "equity": []}
    for r in group_rows:
        if r.out_group_id is None or r.out_amount == 0:
            continue
        sections.setdefault(r.out_section, []).append(
            {
                "group_id": r.out_group_id,
                "group_name": r.out_group_name,
                "parent_id": r.out_parent_id,
                "amount": _fmt(r.out_amount),
            }
        )

    return {
        "as_of": as_of,
        "assets": {"groups": sections["assets"], "total": _fmt(assets)},
        "liabilities": {"groups": sections["liabilities"], "total": _fmt(liabilities)},
        "equity": {
            "groups": sections["equity"],
            "net_profit_to_date": _fmt(profit),
            "total": _fmt(equity + profit),
        },
        "totals": {
            "assets": _fmt(assets),
            "liabilities_and_equity": _fmt(liabilities + equity + profit),
            "difference": "0",
        },
    }
