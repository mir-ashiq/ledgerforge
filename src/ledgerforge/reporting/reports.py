"""Report shapers over ``rpt.*`` SQL functions (db/functions.sql).

The SQL functions do all aggregation; this layer composes responses and —
crucially — asserts the engine's own balance invariants (a Trial Balance
whose difference is not exactly zero, or a Balance Sheet that does not
balance, raises InvariantViolation and is never returned to a client).

Every report is book-scoped (ADR-009, migration 0004): the ``p_book_id``
parameter is mandatory and filters the fact tables.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..errors import InvariantViolation, NotFoundError
from ..models import Ledger


def _fmt(d: Decimal) -> str:
    return format(d, "f")


def trial_balance(session: Session, book_id: int, as_of: date, fast: bool = False) -> dict:
    """Per-ledger balances as of ``as_of``. ``fast`` (ADR-018) sources the
    sums from the balance cache where warm — always exact; a cold or stale
    cache is a full scan, never a wrong number."""
    rows = (
        session.execute(
            text("SELECT * FROM rpt.trial_balance(:book, :as_of, :fast)"),
            {"book": book_id, "as_of": as_of, "fast": fast},
        )
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


def group_rollups(session: Session, book_id: int, as_of: date, fast: bool = False) -> list[dict]:
    rows = (
        session.execute(
            text("SELECT * FROM rpt.group_rollups(:book, :as_of, :fast)"),
            {"book": book_id, "as_of": as_of, "fast": fast},
        )
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


def profit_loss(
    session: Session, book_id: int, d_from: date, d_to: date, fast: bool = False
) -> dict:
    """P&L over [d_from, d_to]; ``fast`` computes it as S(to) − S(from − 1)
    from the balance cache (ADR-018) — arithmetically the same range sums."""
    rows = (
        session.execute(
            text("SELECT * FROM rpt.profit_and_loss(:book, :d_from, :d_to, :fast)"),
            {"book": book_id, "d_from": d_from, "d_to": d_to, "fast": fast},
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


def fy_close(session: Session, book_id: int, as_of: date, pl_ledger_id: int | None = None) -> dict:
    """The financial-year close sweep (issue #5), scoped to ``book_id``:
    for every income/expense ledger of the book with a non-zero closing
    balance as of ``as_of``, the line that zeroes it, plus the Profit & Loss
    A/c balancing line. Amounts stay ``Decimal`` (the close action posts
    them verbatim); the API layer serializes. Like every report, it proves
    its own balance first (Law 1)."""
    if pl_ledger_id is None:
        pl_ledger_id = session.scalar(
            select(Ledger.id).where(Ledger.name == "Profit & Loss A/c", Ledger.book_id == book_id)
        )
        if pl_ledger_id is None:
            raise NotFoundError(
                "Ledger 'Profit & Loss A/c' does not exist in this book — create "
                "the book with its seed (POST /api/v1/books does this) before "
                "closing a year"
            )

    rows = (
        session.execute(
            text("SELECT * FROM rpt.fy_close(:book, :as_of, :pl)"),
            {"book": book_id, "as_of": as_of, "pl": pl_ledger_id},
        )
        .mappings()
        .all()
    )
    lines = [
        {
            "bucket": r.out_bucket,
            "ledger_id": r.out_ledger_id,
            "ledger_name": r.out_ledger_name,
            "dc": r.out_dc,
            "amount": r.out_amount,
        }
        for r in rows
    ]
    total_dr = sum((ln["amount"] for ln in lines if ln["dc"] == "D"), Decimal(0))
    total_cr = sum((ln["amount"] for ln in lines if ln["dc"] == "C"), Decimal(0))
    difference = total_dr - total_cr
    if difference != 0:  # the sweep must balance exactly, like a voucher
        raise InvariantViolation(
            f"FY close sweep does not balance: difference is {difference}, expected exactly 0"
        )
    net = next(
        (
            ln["amount"] * (1 if ln["dc"] == "C" else -1)
            for ln in lines
            if ln["bucket"] == "profit_loss"
        ),
        Decimal(0),
    )
    return {
        "as_of": as_of,
        "lines": lines,
        "net_profit": net,
        "totals": {"dr": total_dr, "cr": total_cr, "difference": difference},
    }


def balance_sheet(session: Session, book_id: int, as_of: date, fast: bool = False) -> dict:
    # Ledger-level classification for authoritative section totals (each
    # ledger counted exactly once, by its own group's nature).
    tb_rows = (
        session.execute(
            text("SELECT * FROM rpt.trial_balance(:book, :as_of, :fast)"),
            {"book": book_id, "as_of": as_of, "fast": fast},
        )
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
        session.execute(
            text("SELECT * FROM rpt.balance_sheet(:book, :as_of, :fast)"),
            {"book": book_id, "as_of": as_of, "fast": fast},
        )
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


# ------------------------------------------------------------- cost centres
def cost_centre_summary(session: Session, book_id: int, d_from: date, d_to: date) -> dict:
    """The centre rollup (ADR-025): every centre's allocation subtree totals
    through the closure — the group_rollups shape — over [d_from, d_to],
    with the Unallocated mass (entries nobody allocated) surfaced so
    allocated + unallocated is always the window's whole entry mass. FYC
    sweeps are excluded (they transfer results, they are not operating
    facts — the profit_and_loss precedent)."""
    rows = (
        session.execute(
            text("SELECT * FROM rpt.cost_centre_summary(:book, :d_from, :d_to)"),
            {"book": book_id, "d_from": d_from, "d_to": d_to},
        )
        .mappings()
        .all()
    )
    centres = [
        {
            "centre_id": r.out_centre_id,
            "centre_name": r.out_centre_name,
            "parent_id": r.out_parent_id,
            "is_root": r.out_is_root,
            "category_id": r.out_category_id,
            "category_name": r.out_category_name,
            "dr": _fmt(r.out_dr),
            "cr": _fmt(r.out_cr),
            "net": _fmt(r.out_net),
        }
        for r in rows
        if r.out_centre_id is not None
    ]
    unallocated = next(
        (
            {"dr": _fmt(r.out_dr), "cr": _fmt(r.out_cr), "net": _fmt(r.out_net)}
            for r in rows
            if r.out_centre_id is None
        ),
        {"dr": "0", "cr": "0", "net": "0"},
    )
    return {
        "from": d_from,
        "to": d_to,
        "centres": centres,
        "unallocated": unallocated,
    }


def cost_centre_breakup(
    session: Session, book_id: int, centre_id: int, d_from: date, d_to: date
) -> dict:
    """Ledger-wise within ONE centre (ADR-025): the centre's own allocations
    (not its subtree — sub-centres have their own break-ups), one row per
    ledger with its nature-signed net, and the total row the shaper proves
    against the ledger rows (the trial-balance discipline: the engine's own
    output is never trusted unproven)."""
    rows = (
        session.execute(
            text("SELECT * FROM rpt.cost_centre_breakup(:book, :centre, :d_from, :d_to)"),
            {"book": book_id, "centre": centre_id, "d_from": d_from, "d_to": d_to},
        )
        .mappings()
        .all()
    )
    ledgers = [
        {
            "ledger_id": r.out_ledger_id,
            "ledger_name": r.out_ledger_name,
            "group_name": r.out_group_name,
            "nature": r.out_nature,
            "dr": _fmt(r.out_dr),
            "cr": _fmt(r.out_cr),
            "net": _fmt(r.out_net),
        }
        for r in rows
        if r.out_bucket == "ledger"
    ]
    total = next(
        (r for r in rows if r.out_bucket == "total"),
        None,
    )
    total_dr = sum((r.out_dr for r in rows if r.out_bucket == "ledger"), Decimal(0))
    total_cr = sum((r.out_cr for r in rows if r.out_bucket == "ledger"), Decimal(0))
    if total is None or total.out_dr != total_dr or total.out_cr != total_cr:
        raise InvariantViolation(
            "Cost centre break-up total does not equal the sum of its ledger rows"
        )
    return {
        "from": d_from,
        "to": d_to,
        "centre_id": centre_id,
        "ledgers": ledgers,
        "totals": {
            "dr": _fmt(total_dr),
            "cr": _fmt(total_cr),
            "net": _fmt(total.out_net),
        },
    }


def cost_category_summary(session: Session, book_id: int, d_from: date, d_to: date) -> dict:
    """The category rollup (ADR-025): allocations through the centre→
    category edge and the category closure — a primary category carries its
    secondaries' centres. The Unallocated row rides along for the same
    conservation story as the centre summary."""
    rows = (
        session.execute(
            text("SELECT * FROM rpt.cost_category_summary(:book, :d_from, :d_to)"),
            {"book": book_id, "d_from": d_from, "d_to": d_to},
        )
        .mappings()
        .all()
    )
    categories = [
        {
            "category_id": r.out_category_id,
            "category_name": r.out_category_name,
            "parent_id": r.out_parent_id,
            "is_root": r.out_is_root,
            "dr": _fmt(r.out_dr),
            "cr": _fmt(r.out_cr),
            "net": _fmt(r.out_net),
        }
        for r in rows
        if r.out_category_id is not None
    ]
    unallocated = next(
        (
            {"dr": _fmt(r.out_dr), "cr": _fmt(r.out_cr), "net": _fmt(r.out_net)}
            for r in rows
            if r.out_category_id is None
        ),
        {"dr": "0", "cr": "0", "net": "0"},
    )
    return {
        "from": d_from,
        "to": d_to,
        "categories": categories,
        "unallocated": unallocated,
    }
