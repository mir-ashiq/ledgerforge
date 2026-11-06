"""Deep audit pass: adversarial probes across the three financial laws,
multi-currency (#4) and period locking (#5) — the gaps the regular suites
leave open.

Sections:
  A. Law 1 — a losing year's FY-close sweep must balance exactly like any
     other voucher, and both years' reports must stay correct.
  B. Law 2 — database-level immutability of things that are NOT vouchers:
     FX snapshot columns, period dates/state, currency reference data, and
     the snapshot-vs-rate-master independence.
  C. Law 3 + FX — rounding boundaries, magnitude boundaries, notation,
     multi-currency lines, rate-pair direction, and the advisory nature of
     the fx_rates master.
  D. Period locking — the DB gate on raw writes, boundary-day exclusion,
     gaps between periods, reversals against a locked period, and the
     lock-vs-post race the FOR SHARE gate exists for.
  E. Idempotency — first write wins when a client_token is replayed with a
     different payload.

The module owns a fresh book (module-scoped fresh_db); every test gets its
own private fiscal year(s) because periods must never overlap.
"""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from conftest import create_currency, create_fx_rate, create_ledger, create_period, post_voucher
from sqlalchemy import select, text

from ledgerforge.errors import PeriodStateError

TODAY = date.today()
RATE = "83.5050000000"  # USD -> INR


def _fx(ledger: str, dc: str, amount_fx: str, rate: str = RATE, ccy: str = "USD") -> dict:
    product = Decimal(amount_fx) * Decimal(rate)
    expected = product.quantize(Decimal("0.00000001"))
    return {
        "ledger_name": ledger,
        "dc": dc,
        "amount": format(expected, "f"),
        "currency": ccy,
        "amount_fx": amount_fx,
        "fx_rate": rate,
    }


@pytest.fixture(scope="module", autouse=True)
def _book(client_module, fresh_db):
    """One fresh book: FX masters, an open period covering today (the module's
    general-purpose date range), and nothing else — each test mints its own
    private years."""
    create_currency(client_module, "USD", "US Dollar", symbol="$")
    create_currency(client_module, "EUR", "Euro", symbol="€")
    create_fx_rate(client_module, "USD", "INR", RATE, "2020-01-01", source="audit")
    create_fx_rate(client_module, "EUR", "INR", "98.1000000000", "2020-01-01", source="audit")
    create_period(
        client_module,
        "Audit Current",
        str(TODAY - timedelta(days=365)),
        str(TODAY + timedelta(days=1)),
    )


# ======================================================================
# A. Law 1 — the losing year
# ======================================================================
def test_fy_close_with_net_loss_balances_and_reports(client, fresh_db):
    """Expenses > income: the sweep must still balance exactly, P&L A/c must
    absorb the loss on the debit side, and neither year's P&L may be polluted
    by the transfer (the FYC exclusion)."""
    create_ledger(client, "Audit Loss Sales", "Sales Accounts")
    create_ledger(client, "Audit Loss Rent", "Indirect Expenses")
    fy1 = create_period(client, "Audit Loss FY 2016", "2016-01-01", "2016-12-31")
    create_period(client, "Audit Loss FY 2017", "2017-01-01", "2017-12-31")

    post_voucher(
        client,
        type_="SAL",
        date_="2016-05-01",
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "400.00"},
            {"ledger_name": "Audit Loss Sales", "dc": "C", "amount": "400.00"},
        ],
    )
    post_voucher(
        client,
        type_="PYT",
        date_="2016-06-01",
        entries=[
            {"ledger_name": "Audit Loss Rent", "dc": "D", "amount": "1000.00"},
            {"ledger_name": "Cash", "dc": "C", "amount": "1000.00"},
        ],
    )

    preview = client.get(f"/api/v1/periods/{fy1['id']}/close-preview").json()
    lines = {(ln["ledger_name"], ln["dc"]): ln["amount"] for ln in preview["lines"]}
    assert lines[("Audit Loss Sales", "D")] == "400.00000000"  # zero the income
    assert lines[("Audit Loss Rent", "C")] == "1000.00000000"  # zero the expense
    assert lines[("Profit & Loss A/c", "D")] == "600.00000000"  # the loss, debited
    assert preview["net_profit"] == "-600.00000000"
    assert Decimal(preview["totals"]["difference"]) == 0

    assert client.post(f"/api/v1/periods/{fy1['id']}/lock").status_code == 200
    closed = client.post(f"/api/v1/periods/{fy1['id']}/close")
    assert closed.status_code == 200, closed.text
    sweep = closed.json()["sweep_voucher"]
    assert sweep["voucher_type"] == "FYC"
    assert sweep["voucher_date"] == "2017-01-01"
    dr = sum((Decimal(e["amount"]) for e in sweep["entries"] if e["dc"] == "D"), Decimal(0))
    cr = sum((Decimal(e["amount"]) for e in sweep["entries"] if e["dc"] == "C"), Decimal(0))
    assert dr == cr  # Law 1 on a losing sweep, exactly

    # the loss year reports its loss; the next year is not polluted
    pl1 = client.get("/api/v1/reports/profit-loss?from=2016-01-01&to=2016-12-31").json()
    assert pl1["profit_and_loss"]["net_profit"] == "-600.00000000"
    pl2 = client.get("/api/v1/reports/profit-loss?from=2017-01-01&to=2017-12-31").json()
    assert Decimal(pl2["profit_and_loss"]["net_profit"]) == 0

    # income/expense ledgers zeroed; the loss sits on P&L A/c as a debit
    tb = client.get("/api/v1/reports/trial-balance?as_of=2017-01-01").json()
    balances = {ln["ledger_name"]: (ln["balance"], ln["dc"]) for ln in tb["ledgers"]}
    assert Decimal(balances["Audit Loss Sales"][0]) == 0
    assert Decimal(balances["Audit Loss Rent"][0]) == 0
    assert balances["Profit & Loss A/c"] == ("600.00000000", "D")  # debit balance
    assert Decimal(tb["totals"]["difference"]) == 0

    bs = client.get("/api/v1/reports/balance-sheet?as_of=2017-01-01").json()
    assert Decimal(bs["totals"]["difference"]) == 0


# ======================================================================
# B. Law 2 — immutability beyond vouchers
# ======================================================================
def test_entry_fx_snapshot_columns_are_immutable(client, fresh_db, psycopg_dsn):
    create_ledger(client, "Audit FX Immutable", "Bank Accounts")
    posted = post_voucher(
        client,
        entries=[
            _fx("Audit FX Immutable", "D", "10.00"),
            {"ledger_name": "Cash", "dc": "C", "amount": "835.05000000"},
        ],
    )
    assert posted.status_code == 201, posted.text
    with psycopg.connect(psycopg_dsn) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(
                "UPDATE voucher_entries SET fx_rate = 1 WHERE voucher_id = %s",
                (posted.json()["id"],),
            )
        conn.rollback()


def test_period_dates_and_transitions_are_guarded(client, fresh_db, psycopg_dsn):
    period = create_period(client, "Audit TG 2022", "2022-01-01", "2022-12-31")
    pid = period["id"]

    def sql(statement: str) -> None:
        with psycopg.connect(psycopg_dsn) as conn:
            conn.execute(statement, (pid,))
            conn.commit()

    with pytest.raises(psycopg.errors.RaiseException, match="dates are immutable"):
        sql("UPDATE periods SET starts_on = '2022-02-01' WHERE id = %s")
    with pytest.raises(psycopg.errors.RaiseException, match="Invalid period status transition"):
        sql("UPDATE periods SET status = 'closed' WHERE id = %s")  # open -> closed

    sql("UPDATE periods SET status = 'locked' WHERE id = %s")  # open -> locked is legal
    with pytest.raises(psycopg.errors.RaiseException, match="closed_by"):
        sql("UPDATE periods SET status = 'closed' WHERE id = %s")  # locked, no actor
    sql("UPDATE periods SET status = 'closed', closed_by = 'auditor' WHERE id = %s")
    with pytest.raises(psycopg.errors.RaiseException, match="Invalid period status transition"):
        sql("UPDATE periods SET status = 'open' WHERE id = %s")  # closed is final

    with psycopg.connect(psycopg_dsn) as conn:
        row = conn.execute(
            "SELECT status, closed_by, closed_at, locked_at FROM periods WHERE id = %s",
            (pid,),
        ).fetchone()
    assert row[0] == "closed" and row[1] == "auditor" and row[2] and row[3]


def test_period_deletion_guards(client, fresh_db, psycopg_dsn):
    with_vouchers = create_period(client, "Audit D1 2015", "2015-01-01", "2015-12-31")
    create_ledger(client, "Audit D1 Sales", "Sales Accounts")
    post_voucher(
        client,
        date_="2015-06-01",
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "1.00"},
            {"ledger_name": "Audit D1 Sales", "dc": "C", "amount": "1.00"},
        ],
    )
    locked_empty = create_period(client, "Audit D2 2014", "2014-01-01", "2014-12-31")
    open_empty = create_period(client, "Audit D3 2013", "2013-01-01", "2013-12-31")

    def try_delete(pid: int) -> None:
        with psycopg.connect(psycopg_dsn) as conn:
            conn.execute("DELETE FROM periods WHERE id = %s", (pid,))
            conn.commit()

    with pytest.raises(psycopg.errors.RaiseException, match="contains 1 voucher"):
        try_delete(with_vouchers["id"])
    with psycopg.connect(psycopg_dsn) as conn:  # lock it, then try again
        conn.execute("UPDATE periods SET status = 'locked' WHERE id = %s", (locked_empty["id"],))
        conn.commit()
    with pytest.raises(psycopg.errors.RaiseException, match="Only open periods"):
        try_delete(locked_empty["id"])
    try_delete(open_empty["id"])  # open + empty deletes cleanly
    with psycopg.connect(psycopg_dsn) as conn:
        n = conn.execute(
            "SELECT count(*) FROM periods WHERE id = %s", (open_empty["id"],)
        ).fetchone()[0]
    assert n == 0


def test_used_currency_cannot_be_deleted_unused_can(client, fresh_db, psycopg_dsn):
    create_ledger(client, "Audit CCY Bank", "Bank Accounts")
    posted = post_voucher(
        client,
        entries=[
            _fx("Audit CCY Bank", "D", "5.00"),
            {"ledger_name": "Cash", "dc": "C", "amount": "417.52500000"},
        ],
    )
    assert posted.status_code == 201, posted.text
    create_currency(client, "JPY", "Japanese Yen")  # created, never referenced

    def delete(code: str):
        with psycopg.connect(psycopg_dsn) as conn:
            conn.execute("DELETE FROM currencies WHERE code = %s", (code,))
            conn.commit()

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        delete("USD")  # referenced by entries AND fx_rates
    delete("JPY")  # unreferenced masters remain deletable — by FK design
    codes = {c["code"] for c in client.get("/api/v1/currencies").json()}
    assert "JPY" not in codes and "USD" in codes


def test_rate_correction_never_restates_posted_history(client, fresh_db):
    """PATCH the master rate after posting: the immutable snapshot on the
    entry keeps the rate that was actually applied (Law 2 vs reference data)."""
    create_ledger(client, "Audit Snapshot Bank", "Bank Accounts")
    posted = post_voucher(
        client,
        entries=[
            _fx("Audit Snapshot Bank", "D", "20.00"),
            {"ledger_name": "Cash", "dc": "C", "amount": "1670.10000000"},
        ],
    ).json()
    entry = next(e for e in posted["entries"] if e["ledger_name"] == "Audit Snapshot Bank")
    assert entry["fx_rate"] == RATE

    rate = client.get("/api/v1/fx-rates?from_ccy=USD&to_ccy=INR").json()[0]
    patched = client.patch(f"/api/v1/fx-rates/{rate['id']}", json={"rate": "99.0000000000"})
    assert patched.status_code == 200

    reread = client.get(f"/api/v1/vouchers/{posted['id']}").json()
    entry_after = next(e for e in reread["entries"] if e["ledger_name"] == "Audit Snapshot Bank")
    assert entry_after["fx_rate"] == RATE  # snapshot untouched
    assert entry_after["amount"] == "1670.10000000"


def test_currency_master_changes_are_audited(client, fresh_db, db_session):
    db_session.rollback()
    client.patch("/api/v1/currencies/USD", json={"symbol": "US$"})
    ops = [
        r[0]
        for r in db_session.execute(
            text("SELECT operation FROM audit_log WHERE table_name = 'currencies' ORDER BY id")
        ).all()
    ]
    assert ops.count("INSERT") >= 2  # USD + EUR from module setup
    assert ops[-1] == "UPDATE"  # the patch is journaled


# ======================================================================
# C. Law 3 + multi-currency edges
# ======================================================================
def test_rounding_boundary_half_up_at_the_eighth_decimal(client, fresh_db):
    """0.00000001 x 1.5000000000 = 0.000000015 — half-away-from-zero must
    round UP to 0.00000002, and the engine and the database CHECK must agree."""
    create_ledger(client, "Audit Rounding Bank", "Bank Accounts")
    wrong = post_voucher(
        client,
        entries=[
            {
                **_fx("Audit Rounding Bank", "D", "0.00000001", rate="1.5000000000"),
                "amount": "0.00000001",
            },  # the truncated, not rounded, product
            {"ledger_name": "Cash", "dc": "C", "amount": "0.00000001"},
        ],
    )
    assert wrong.status_code == 422
    assert "0.00000002" in wrong.json()["detail"]  # the expected figure is in the error

    right = post_voucher(
        client,
        entries=[
            _fx("Audit Rounding Bank", "D", "0.00000001", rate="1.5000000000"),
            {"ledger_name": "Cash", "dc": "C", "amount": "0.00000002"},
        ],
    )
    assert right.status_code == 201, right.text
    entry = next(e for e in right.json()["entries"] if e["currency"] == "USD")
    assert entry["amount"] == "0.00000002"


def test_amount_magnitude_and_notation_boundaries(client, fresh_db):
    create_ledger(client, "Audit Boundary Sales", "Sales Accounts")
    # the largest legal amount (just under 10^18 with full 8dp scale)
    big = "999999999999999999.99999999"
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": big},
            {"ledger_name": "Audit Boundary Sales", "dc": "C", "amount": big},
        ],
    )
    assert r.status_code == 201, r.text
    assert Decimal(r.json()["entries"][0]["amount"]) == Decimal(big)

    for bad in ("1000000000000000000", "1e400", "-1e2", "0.000000001", "1.000000000"):
        r = post_voucher(
            client,
            entries=[
                {"ledger_name": "Cash", "dc": "D", "amount": "10.00"},
                {"ledger_name": "Audit Boundary Sales", "dc": "C", "amount": bad},
            ],
        )
        assert r.status_code == 422, f"{bad!r} should be refused: {r.text}"

    # exact scientific notation is an exact decimal — accepted, stored exactly
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "1e2"},
            {"ledger_name": "Audit Boundary Sales", "dc": "C", "amount": "100.00"},
        ],
    )
    assert r.status_code == 201, r.text
    assert Decimal(r.json()["entries"][0]["amount"]) == Decimal("100")


def test_fx_rate_must_be_positive_and_within_scale(client, fresh_db):
    create_ledger(client, "Audit Rate Bank", "Bank Accounts")
    for rate in ("0", "0.0000000000", "-83.5050000000"):
        r = post_voucher(
            client,
            entries=[
                _fx("Audit Rate Bank", "D", "10.00", rate=rate),
                {"ledger_name": "Cash", "dc": "C", "amount": "835.05000000"},
            ],
        )
        assert r.status_code == 422, rate
    # amount_fx obeys the amount rules too: 9 decimals is one too many
    r = post_voucher(
        client,
        entries=[
            {**_fx("Audit Rate Bank", "D", "10.00"), "amount_fx": "10.000000001"},
            {"ledger_name": "Cash", "dc": "C", "amount": "835.05000000"},
        ],
    )
    assert r.status_code == 422


def test_two_foreign_currencies_in_one_voucher(client, fresh_db):
    """USD and EUR lines plus a base contra in a single voucher — all three
    snapshot independently and the voucher balances in base currency."""
    create_ledger(client, "Audit Multi Desk", "Bank Accounts")
    r = post_voucher(
        client,
        entries=[
            _fx("Audit Multi Desk", "D", "100.00", rate=RATE),  # 8350.50 INR
            _fx("Audit Multi Desk", "D", "50.00", rate="98.1000000000", ccy="EUR"),  # 4905.00
            {"ledger_name": "Cash", "dc": "C", "amount": "13255.50000000"},
        ],
    )
    assert r.status_code == 201, r.text
    by_ccy = {e["currency"]: e for e in r.json()["entries"] if e["currency"]}
    assert by_ccy["USD"]["fx_rate"] == RATE
    assert by_ccy["EUR"]["fx_rate"] == "98.1000000000"
    assert by_ccy["USD"]["amount"] == "8350.50000000"
    assert by_ccy["EUR"]["amount"] == "4905.00000000"

    tb = client.get("/api/v1/reports/trial-balance").json()
    assert Decimal(tb["totals"]["difference"]) == 0
    desk = next(ln for ln in tb["ledgers"] if ln["ledger_name"] == "Audit Multi Desk")
    assert Decimal(desk["balance"]) == Decimal("13255.50000000")  # aggregated in base


def test_no_silent_rate_inversion(client, fresh_db):
    """Only USD->INR rows exist; the reverse direction must 404 rather than
    quietly applying 1/rate (inversion re-rounds and lies about what applied)."""
    r = client.get("/api/v1/fx-rates/resolve?from_ccy=INR&to_ccy=USD&on=2026-01-01")
    assert r.status_code == 404
    assert "INR→USD" in r.json()["detail"]
    r = client.get("/api/v1/fx-rates/resolve?from_ccy=EUR&to_ccy=USD&on=2026-01-01")
    assert r.status_code == 404


def test_posted_rate_is_the_truth_not_the_master(client, fresh_db):
    """fx_rates is advisory input for UIs: a consistent trio posts even when
    no master row carries that rate (the snapshot is the applied truth)."""
    create_ledger(client, "Audit Truth Bank", "Bank Accounts")
    r = post_voucher(
        client,
        entries=[
            _fx("Audit Truth Bank", "D", "100.00", rate="80.0000000000"),  # no such master row
            {"ledger_name": "Cash", "dc": "C", "amount": "8000.00000000"},
        ],
    )
    assert r.status_code == 201, r.text


def test_currency_code_normalization(client, fresh_db):
    create_ledger(client, "Audit Lower Bank", "Bank Accounts")
    line = {**_fx("Audit Lower Bank", "D", "1.00"), "currency": "usd"}  # lowercase in
    r = post_voucher(
        client,
        entries=[
            line,
            {"ledger_name": "Cash", "dc": "C", "amount": "83.50500000"},
        ],
    )
    assert r.status_code == 201, r.text
    usd = [e for e in r.json()["entries"] if e["currency"]]
    assert len(usd) == 1 and usd[0]["currency"] == "USD"  # upper-cased on the way in


def test_minor_unit_bounds_enforced(client, fresh_db):
    r = client.post("/api/v1/currencies", json={"code": "CHF", "name": "Franc", "minor_unit": 9})
    assert r.status_code == 422
    r = client.post("/api/v1/currencies", json={"code": "CHF", "name": "Franc", "minor_unit": 8})
    assert r.status_code == 201


# ======================================================================
# D. Period locking edges
# ======================================================================
def test_db_gate_blocks_raw_inserts_locked_open_gap(client, fresh_db, psycopg_dsn):
    gate = create_period(client, "Audit Gate 2030", "2030-01-01", "2030-12-31")
    create_period(client, "Audit Gate2 2031H2", "2031-06-01", "2031-12-31")
    with psycopg.connect(psycopg_dsn) as conn:
        conn.execute("UPDATE periods SET status = 'locked' WHERE id = %s", (gate["id"],))
        conn.commit()

    def raw_insert(voucher_no: str, day: str) -> None:
        with psycopg.connect(psycopg_dsn) as conn:
            conn.execute(
                "INSERT INTO vouchers (client_token, voucher_type_id,"
                " voucher_no, sequence, voucher_date)"
                " VALUES (gen_random_uuid(),"
                " (SELECT id FROM voucher_types WHERE code = 'JV'), %s, 1, %s)",
                (voucher_no, day),
            )
            conn.rollback()  # the gate fires at INSERT; no need to commit

    with pytest.raises(psycopg.errors.RaiseException, match="locked"):
        raw_insert("JV-AUDIT-LOCKED", "2030-07-01")
    with pytest.raises(psycopg.errors.RaiseException, match="No period covers"):
        raw_insert("JV-AUDIT-GAP", "2032-05-05")
    raw_insert("JV-AUDIT-OPEN", "2031-08-01")  # open period: the gate waves it through


def test_adjacent_periods_cannot_share_a_boundary_day(client, fresh_db):
    create_period(client, "Audit SH Mar 2024", "2024-03-01", "2024-03-31")
    shared = client.post(
        "/api/v1/periods",
        json={"name": "Audit SH Apr 2024", "starts_on": "2024-03-31", "ends_on": "2024-04-30"},
    )
    assert shared.status_code == 422  # '[]' ranges: 03-31 already belongs to March
    assert "overlap" in shared.json()["detail"].lower()
    touching = client.post(
        "/api/v1/periods",
        json={"name": "Audit SH Apr 2024b", "starts_on": "2024-04-01", "ends_on": "2024-04-30"},
    )
    assert touching.status_code == 201  # contiguous is fine


def test_single_day_period_and_gap_refusal(client, fresh_db):
    create_ledger(client, "Audit Day Sales", "Sales Accounts")
    create_period(client, "Audit Single Day", "2024-06-10", "2024-06-10")
    ok = post_voucher(
        client,
        date_="2024-06-10",
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "2.00"},
            {"ledger_name": "Audit Day Sales", "dc": "C", "amount": "2.00"},
        ],
    )
    assert ok.status_code == 201, ok.text
    refused = post_voucher(
        client,
        date_="2024-06-11",
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "2.00"},
            {"ledger_name": "Audit Day Sales", "dc": "C", "amount": "2.00"},
        ],
    )
    assert refused.status_code == 422
    assert "No period covers" in refused.json()["detail"]


def test_close_refused_when_a_gap_precedes_the_next_period(client, fresh_db):
    """Periods need not be contiguous; if the sweep date (ends_on + 1 day)
    falls in a gap, close must refuse rather than post into nowhere."""
    gap_fy = create_period(client, "Audit Gap FY 2019", "2019-01-01", "2019-12-31")
    create_period(client, "Audit Gap FY 2020H1", "2020-02-01", "2020-06-30")  # gap: 2020-01
    client.post(f"/api/v1/periods/{gap_fy['id']}/lock")
    r = client.post(f"/api/v1/periods/{gap_fy['id']}/close")
    assert r.status_code == 422
    assert "2020-01-01" in r.json()["detail"]  # the sweep date, named in the error


def test_reversal_of_a_voucher_in_a_locked_period(client, fresh_db):
    """The sanctioned correction path: reversing INTO the locked period is
    refused by the same gate; the default reversal date (today, an open
    period) goes through."""
    create_ledger(client, "Audit Rev Sales", "Sales Accounts")
    rev_fy = create_period(client, "Audit Rev 2023", "2023-01-01", "2023-12-31")
    original = post_voucher(
        client,
        date_="2023-06-15",
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "77.00"},
            {"ledger_name": "Audit Rev Sales", "dc": "C", "amount": "77.00"},
        ],
    ).json()
    client.post(f"/api/v1/periods/{rev_fy['id']}/lock")

    blocked = client.post(
        f"/api/v1/vouchers/{original['id']}/reverse", json={"reversal_date": "2023-06-20"}
    )
    assert blocked.status_code == 409
    assert "locked" in blocked.json()["detail"]

    allowed = client.post(f"/api/v1/vouchers/{original['id']}/reverse", json={})
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["voucher_date"] == str(TODAY)
    tb = client.get("/api/v1/reports/trial-balance").json()
    sales = next(ln for ln in tb["ledgers"] if ln["ledger_name"] == "Audit Rev Sales")
    assert Decimal(sales["balance"]) == 0  # fully reversed, net zero


def test_lock_versus_post_race(db_session, fresh_db, plugin_manager, settings, client):
    """The FOR SHARE serialization (ADR-008): while workers post into a
    period, another connection locks it. Every attempt must end cleanly —
    posted (before the lock) or refused with the engine's PeriodStateError —
    never a raw driver error, and nothing may land after the lock."""
    from ledgerforge import db
    from ledgerforge.engine.periods import lock_period
    from ledgerforge.engine.posting import post_voucher as engine_post
    from ledgerforge.models import Ledger
    from ledgerforge.schemas import VoucherCreate, VoucherLineIn

    create_ledger(client, "Audit Race Sales", "Sales Accounts")
    race = create_period(client, "Audit Race 2018", "2018-01-01", "2018-12-31")
    cash_id = db_session.scalar(select(Ledger.id).where(Ledger.name == "Cash"))
    sales_id = db_session.scalar(select(Ledger.id).where(Ledger.name == "Audit Race Sales"))
    db_session.commit()

    n_workers, per_worker = 6, 6
    factory = db.session_factory()
    posted: list[int] = []
    refused: list[Exception] = []
    unexpected: list[Exception] = []
    lock_result: dict = {}

    def poster(worker_id: int) -> None:
        session = factory()
        try:
            for _ in range(per_worker):
                payload = VoucherCreate(
                    voucher_type="JV",
                    voucher_date=date(2018, 7, 1),
                    client_token=uuid4(),
                    entries=[
                        VoucherLineIn(ledger_id=cash_id, dc="D", amount=Decimal("1.00")),
                        VoucherLineIn(ledger_id=sales_id, dc="C", amount=Decimal("1.00")),
                    ],
                )
                try:
                    voucher, _ = engine_post(session, payload, plugin_manager, settings)
                    posted.append(voucher.id)
                except PeriodStateError:
                    refused.append(Exception("locked"))  # the clean refusal
                except Exception as exc:  # noqa: BLE001 — the point of the test
                    unexpected.append(exc)
                    session.rollback()
        finally:
            session.close()

    def locker() -> None:
        session = factory()
        try:
            time.sleep(0.12)  # let the posters get going
            lock_period(session, race["id"], actor="audit-race")
            lock_result["status"] = "locked"
        finally:
            session.close()

    threads = [threading.Thread(target=poster, args=(t,)) for t in range(n_workers)]
    threads.append(threading.Thread(target=locker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not unexpected, f"raw driver errors under race: {unexpected!r}"
    assert lock_result.get("status") == "locked"
    total_attempts = n_workers * per_worker
    assert len(posted) + len(refused) == total_attempts  # every attempt resolved cleanly

    landed = db_session.execute(
        text(
            "SELECT count(*), min(posted_at), max(posted_at) FROM vouchers"
            " WHERE voucher_date = '2018-07-01'"
        )
    ).fetchone()
    assert landed[0] == len(posted), "every success must be durable, and nothing else"
    locked_at = db_session.execute(
        text("SELECT locked_at FROM periods WHERE id = :pid"), {"pid": race["id"]}
    ).scalar()
    assert landed[2] <= locked_at, "no voucher may commit after the lock took effect"

    tb = client.get("/api/v1/reports/trial-balance").json()
    assert Decimal(tb["totals"]["difference"]) == 0


# ======================================================================
# E. Idempotency — first write wins
# ======================================================================
def test_replayed_token_with_different_payload_returns_original(client, fresh_db):
    create_ledger(client, "Audit Replay Sales", "Sales Accounts")
    token = uuid4()
    first = post_voucher(
        client,
        token=token,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "50.00"},
            {"ledger_name": "Audit Replay Sales", "dc": "C", "amount": "50.00"},
        ],
    )
    assert first.status_code == 201

    replay = post_voucher(
        client,
        token=token,
        entries=[  # different amounts entirely
            {"ledger_name": "Cash", "dc": "D", "amount": "999.00"},
            {"ledger_name": "Audit Replay Sales", "dc": "C", "amount": "999.00"},
        ],
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["id"] == first.json()["id"]

    tb = client.get("/api/v1/reports/trial-balance").json()
    sales = next(ln for ln in tb["ledgers"] if ln["ledger_name"] == "Audit Replay Sales")
    assert Decimal(sales["balance"]) == Decimal("50.00")  # the replay wrote nothing
