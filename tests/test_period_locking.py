"""Period locking and the financial-year close (issue #5, ADR-008): the
opt-in posting gate, the open/locked/closed state machine, and the P&L sweep
that zeroes the year into Profit & Loss A/c without polluting either year's
report.

The module shares one fresh book, so every test owns its own fiscal-year
range (periods must never overlap — the database enforces it) and its own
ledger names."""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import create_ledger, create_period, post_voucher

# (label, first-year range, next-year range) — disjoint across tests
RANGES = {
    "open": ("FY 2026-27", "2026-04-01", "2027-03-31"),
    "lock": ("Lock FY 2023-24", "2023-04-01", "2024-03-31"),
    "close_req": ("CloseReq FY 2024-25", "2024-04-01", "2025-03-31"),
    "close_req_next": ("CloseReq FY 2025-26", "2025-04-01", "2026-03-31"),
    "full": ("Full FY 2021-22", "2021-04-01", "2022-03-31"),
    "full_next": ("Full FY 2022-23", "2022-04-01", "2023-03-31"),
    "next_open": ("NextOpen FY 2019-20", "2019-04-01", "2020-03-31"),
    "next_open_next": ("NextOpen FY 2020-21", "2020-04-01", "2021-03-31"),
    "retry": ("Retry FY 2017-18", "2017-04-01", "2018-03-31"),
    "retry_next": ("Retry FY 2018-19", "2018-04-01", "2019-03-31"),
    "swept": ("Swept FY 2015-16", "2015-04-01", "2016-03-31"),
    "swept_next": ("Swept FY 2016-17", "2016-04-01", "2017-03-31"),
    "roles": ("Roles FY 2013-14", "2013-04-01", "2014-03-31"),
    "audit": ("Audit FY 2011-12", "2011-04-01", "2012-03-31"),
    "audit_next": ("Audit FY 2012-13", "2012-04-01", "2013-03-31"),
}


def _period(client, key: str) -> dict:
    name, start, end = RANGES[key]
    return create_period(client, name, start, end)


def _transfer(client, ledger: str, amount: str, date_: str, type_: str = "CN") -> dict:
    """Cash <-> ledger contra; asset-only, so no income/expense balances are
    created (FY-close sweeps are cumulative as-of and must stay predictable)."""
    r = post_voucher(
        client,
        type_=type_,
        date_=date_,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": amount},
            {"ledger_name": ledger, "dc": "C", "amount": amount},
        ],
    )
    assert r.status_code == 201, r.text
    return r.json()


def _sale(client, ledger: str, amount: str, date_: str) -> dict:
    r = post_voucher(
        client,
        type_="SAL",
        date_=date_,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": amount},
            {"ledger_name": ledger, "dc": "C", "amount": amount},
        ],
    )
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ the gate
def test_no_periods_every_date_posts(client, fresh_db):
    """The feature is opt-in: with no periods defined, any date posts."""
    create_ledger(client, "Legacy Bank", "Bank Accounts")
    assert _transfer(client, "Legacy Bank", "10.00", "2019-01-01")["voucher_no"]


def test_open_period_accepts_and_gate_covers_dates(client, fresh_db):
    create_ledger(client, "Gated Sales", "Sales Accounts")
    _period(client, "open")
    assert _sale(client, "Gated Sales", "100.00", "2026-11-06")["voucher_no"]

    # once periods exist, a date NO period covers is refused — past dates
    # outside every range, exactly the typo class the gate exists for
    for bad_date in ("2026-03-31", "2015-01-01"):
        r = post_voucher(
            client,
            date_=bad_date,
            entries=[
                {"ledger_name": "Cash", "dc": "D", "amount": "1.00"},
                {"ledger_name": "Gated Sales", "dc": "C", "amount": "1.00"},
            ],
        )
        assert r.status_code == 422
        assert "No period covers" in r.json()["detail"]


def test_locked_and_unlocked_cycle(client, fresh_db):
    create_ledger(client, "Lock Bank", "Bank Accounts")
    period = _period(client, "lock")
    r = client.post(f"/api/v1/periods/{period['id']}/lock")
    assert r.status_code == 200
    assert r.json()["status"] == "locked"
    assert r.json()["locked_at"] is not None

    blocked = post_voucher(
        client,
        date_="2023-11-06",
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "5.00"},
            {"ledger_name": "Lock Bank", "dc": "C", "amount": "5.00"},
        ],
    )
    assert blocked.status_code == 409
    assert "locked" in blocked.json()["detail"]

    unlocked = client.post(f"/api/v1/periods/{period['id']}/unlock")
    assert unlocked.json()["status"] == "open"
    assert unlocked.json()["locked_at"] is None
    assert _transfer(client, "Lock Bank", "5.00", "2023-11-06")["voucher_no"]


def test_period_shapes_are_validated(client, fresh_db):
    assert (
        client.post(
            "/api/v1/periods",
            json={"name": "backwards", "starts_on": "2027-03-31", "ends_on": "2026-04-01"},
        ).status_code
        == 422
    )
    # the overlap probe needs an existing period to overlap with
    if not any(p["name"] == RANGES["open"][0] for p in client.get("/api/v1/periods").json()):
        _period(client, "open")
    overlapping = client.post(
        "/api/v1/periods",
        json={"name": "overlap", "starts_on": "2027-01-01", "ends_on": "2027-06-30"},
    )
    assert overlapping.status_code == 422
    assert "overlap" in overlapping.json()["detail"].lower()
    duplicate = client.post(
        "/api/v1/periods",
        json={
            "name": RANGES["open"][0],
            "starts_on": "2030-01-01",
            "ends_on": "2030-12-31",
        },
    )
    assert duplicate.status_code == 422
    assert "already exists" in duplicate.json()["detail"]


# ------------------------------------------------------------- the FY close
def test_close_requires_lock_then_next_period(client, fresh_db):
    period = _period(client, "close_req")
    r = client.post(f"/api/v1/periods/{period['id']}/close")
    assert r.status_code == 409  # open -> closed is refused; lock first
    assert "lock" in r.json()["detail"]

    client.post(f"/api/v1/periods/{period['id']}/lock")
    r = client.post(f"/api/v1/periods/{period['id']}/close")
    assert r.status_code == 422  # no next period for the sweep date
    assert "2025-04-01" in r.json()["detail"]

    _period(client, "close_req_next")
    client.post(f"/api/v1/periods/{period['id']}/unlock")  # back to open
    client.post(f"/api/v1/periods/{period['id']}/lock")  # and forward again
    r = client.post(f"/api/v1/periods/{period['id']}/close")
    assert r.status_code == 200  # nothing to sweep, but closes cleanly
    assert r.json()["sweep_voucher"] is None
    assert r.json()["period"]["status"] == "closed"
    assert r.json()["period"]["closed_by"] == "anonymous"


def test_full_close_flow_sweeps_the_year(client, fresh_db):
    create_ledger(client, "Year Sales", "Sales Accounts")
    create_ledger(client, "Year Rent", "Indirect Expenses")
    fy1 = _period(client, "full")
    _period(client, "full_next")

    _sale(client, "Year Sales", "10000.00", "2021-06-01")
    rent = post_voucher(
        client,
        type_="PYT",
        date_="2021-07-01",
        entries=[
            {"ledger_name": "Year Rent", "dc": "D", "amount": "4000.00"},
            {"ledger_name": "Cash", "dc": "C", "amount": "4000.00"},
        ],
    )
    assert rent.status_code == 201, rent.text

    # preview: exactly what close will post
    preview = client.get(f"/api/v1/periods/{fy1['id']}/close-preview").json()
    lines = {(ln["ledger_name"], ln["dc"]): ln["amount"] for ln in preview["lines"]}
    assert lines[("Year Sales", "D")] == "10000.00000000"
    assert lines[("Year Rent", "C")] == "4000.00000000"
    assert lines[("Profit & Loss A/c", "C")] == "6000.00000000"
    assert preview["net_profit"] == "6000.00000000"
    assert Decimal(preview["totals"]["difference"]) == 0

    assert client.post(f"/api/v1/periods/{fy1['id']}/lock").status_code == 200
    closed = client.post(f"/api/v1/periods/{fy1['id']}/close")
    assert closed.status_code == 200, closed.text
    body = closed.json()
    sweep = body["sweep_voucher"]
    assert body["sweep_posted"] is True
    assert sweep["voucher_type"] == "FYC"
    assert sweep["voucher_date"] == "2022-04-01"  # first day of the NEXT period
    assert sweep["narration"].startswith("Financial year close")
    debits = sum((Decimal(e["amount"]) for e in sweep["entries"] if e["dc"] == "D"), Decimal(0))
    credits = sum((Decimal(e["amount"]) for e in sweep["entries"] if e["dc"] == "C"), Decimal(0))
    assert debits == credits  # Law 1 on the sweep, exactly

    # closed is final: no posting, no unlock, no re-close
    assert (
        post_voucher(
            client,
            date_="2022-01-15",
            entries=[
                {"ledger_name": "Cash", "dc": "D", "amount": "1.00"},
                {"ledger_name": "Year Sales", "dc": "C", "amount": "1.00"},
            ],
        ).status_code
        == 409
    )
    assert client.post(f"/api/v1/periods/{fy1['id']}/unlock").status_code == 409
    assert client.post(f"/api/v1/periods/{fy1['id']}/close").status_code == 409
    assert body["period"]["closed_at"] is not None
    assert body["period"]["closed_by"] == "anonymous"

    # the closed year's own P&L is intact (FYC sweeps are excluded) ...
    pl1 = client.get("/api/v1/reports/profit-loss?from=2021-04-01&to=2022-03-31").json()
    assert pl1["profit_and_loss"]["net_profit"] == "6000.00000000"
    # ... and the next year's P&L is not polluted by the transfer lines
    pl2 = client.get("/api/v1/reports/profit-loss?from=2022-04-01&to=2023-03-31").json()
    assert Decimal(pl2["profit_and_loss"]["net_profit"]) == 0

    # income/expense ledgers are zeroed as of the sweep date
    tb = client.get("/api/v1/reports/trial-balance?as_of=2022-04-01").json()
    balances = {ln["ledger_name"]: ln["balance"] for ln in tb["ledgers"]}
    assert Decimal(balances["Year Sales"]) == 0
    assert Decimal(balances["Year Rent"]) == 0
    pl_ac = next(ln for ln in tb["ledgers"] if ln["ledger_name"] == "Profit & Loss A/c")
    assert pl_ac["balance"] == "6000.00000000"

    # the balance sheet still balances (assets == liabilities + equity + profit)
    bs = client.get("/api/v1/reports/balance-sheet?as_of=2022-04-01").json()
    assert Decimal(bs["totals"]["difference"]) == 0


def test_next_period_must_be_open_for_the_sweep(client, fresh_db):
    create_ledger(client, "Sweep Sales", "Sales Accounts")
    fy1 = _period(client, "next_open")
    fy2 = _period(client, "next_open_next")
    _sale(client, "Sweep Sales", "500.00", "2019-11-06")
    client.post(f"/api/v1/periods/{fy1['id']}/lock")
    client.post(f"/api/v1/periods/{fy2['id']}/lock")
    r = client.post(f"/api/v1/periods/{fy1['id']}/close")
    assert r.status_code == 409
    assert "sweep" in r.json()["detail"]
    client.post(f"/api/v1/periods/{fy2['id']}/unlock")
    r = client.post(f"/api/v1/periods/{fy1['id']}/close")
    assert r.status_code == 200
    assert r.json()["sweep_posted"] is True


def test_close_retry_after_sweep_is_safe(client, fresh_db):
    """A retry after the sweep posted but before the status flip replays the
    sweep (same deterministic client_token) instead of double-sweeping."""
    create_ledger(client, "Retry Sales", "Sales Accounts")
    fy1 = _period(client, "retry")
    _period(client, "retry_next")
    _sale(client, "Retry Sales", "300.00", "2017-11-06")

    # simulate the crash window: post the sweep by hand with the same
    # deterministic token the close action derives, then close normally
    from ledgerforge.engine.periods import _sweep_token

    token = _sweep_token(fy1["id"])
    sweep_by_hand = post_voucher(
        client,
        type_="FYC",
        date_="2018-04-01",
        token=token,
        narration="Financial year close — Retry FY 2017-18 (hand-posted probe)",
        entries=[
            {"ledger_name": "Retry Sales", "dc": "D", "amount": "300.00"},
            {"ledger_name": "Profit & Loss A/c", "dc": "C", "amount": "300.00"},
        ],
    )
    assert sweep_by_hand.status_code == 201

    client.post(f"/api/v1/periods/{fy1['id']}/lock")
    closed = client.post(f"/api/v1/periods/{fy1['id']}/close")
    assert closed.status_code == 200
    assert closed.json()["sweep_posted"] is False  # replayed, not duplicated
    fyc_vouchers = [
        v
        for v in client.get("/api/v1/vouchers?type=FYC&limit=100").json()
        if v["client_token"] == str(token)
    ]
    assert len(fyc_vouchers) == 1


def test_unlock_refused_once_swept(client, fresh_db):
    """A locked period with a (crash-window) sweep can never be reopened:
    the sweep is a real, immutable voucher; a reopen would leave it stale."""
    create_ledger(client, "Swept Sales", "Sales Accounts")
    fy1 = _period(client, "swept")
    _period(client, "swept_next")
    _sale(client, "Swept Sales", "100.00", "2015-11-06")

    from ledgerforge.engine.periods import _sweep_token

    client.post(f"/api/v1/periods/{fy1['id']}/lock")
    sweep = post_voucher(  # what close would have posted, before the crash
        client,
        type_="FYC",
        date_="2016-04-01",
        token=_sweep_token(fy1["id"]),
        narration="Financial year close — Swept FY 2015-16",
        entries=[
            {"ledger_name": "Swept Sales", "dc": "D", "amount": "100.00"},
            {"ledger_name": "Profit & Loss A/c", "dc": "C", "amount": "100.00"},
        ],
    )
    assert sweep.status_code == 201

    r = client.post(f"/api/v1/periods/{fy1['id']}/unlock")
    assert r.status_code == 409
    assert "sweep" in r.json()["detail"].lower()

    # the period can still be closed (the sweep replays via its token)
    closed = client.post(f"/api/v1/periods/{fy1['id']}/close")
    assert closed.status_code == 200
    assert closed.json()["sweep_posted"] is False  # replayed, not re-posted


# ------------------------------------------------------------ roles & audit
@pytest.fixture()
def roles_client(monkeypatch, fresh_db, sqlalchemy_url):
    """An OIDC-mode app wired to test_auth's mock JWKS (no real network)."""
    from fastapi.testclient import TestClient
    from test_auth import AUDIENCE, ISSUER, JWKS_URL, mock_client

    from ledgerforge import db
    from ledgerforge.auth import OIDCTokenVerifier
    from ledgerforge.main import create_app
    from ledgerforge.settings import get_settings

    for key in (
        "LF_AUTH_MODE",
        "LF_OIDC_ISSUER",
        "LF_OIDC_AUDIENCE",
        "LF_OIDC_JWKS_URI",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LF_AUTH_MODE", "oidc")
    monkeypatch.setenv("LF_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("LF_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("LF_OIDC_JWKS_URI", JWKS_URL)
    get_settings.cache_clear()
    db.reset_engine()
    app = create_app()
    app.state.token_verifier = OIDCTokenVerifier(
        issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URL, http_client=mock_client()
    )
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()
    db.reset_engine()


def test_period_mutations_are_admin_only(roles_client, fresh_db):
    """A reader can read periods; creating, locking and closing are 403."""
    from test_auth import bearer, make_token

    reader = bearer(make_token(roles=["reader"]))
    admin = bearer(make_token(roles=["admin"]))

    assert roles_client.get("/api/v1/periods", headers=reader).status_code == 200

    r = roles_client.post(
        "/api/v1/periods",
        json={
            "name": RANGES["roles"][0],
            "starts_on": RANGES["roles"][1],
            "ends_on": RANGES["roles"][2],
        },
        headers=reader,
    )
    assert r.status_code == 403
    assert "admin" in r.json()["detail"]

    created = roles_client.post(
        "/api/v1/periods",
        json={
            "name": RANGES["roles"][0],
            "starts_on": RANGES["roles"][1],
            "ends_on": RANGES["roles"][2],
        },
        headers=admin,
    )
    assert created.status_code == 201
    pid = created.json()["id"]

    assert roles_client.post(f"/api/v1/periods/{pid}/lock", headers=reader).status_code == 403
    assert roles_client.post(f"/api/v1/periods/{pid}/lock", headers=admin).status_code == 200
    assert roles_client.post(f"/api/v1/periods/{pid}/close", headers=reader).status_code == 403
    # admin's close passes the role gate and reaches the engine (it fails only
    # for lack of a next period — proof the role check let it through)
    closing = roles_client.post(f"/api/v1/periods/{pid}/close", headers=admin)
    assert closing.status_code == 422
    assert "2014-04-01" in closing.json()["detail"]


def test_close_is_audited_with_actor(client, db_session, fresh_db):
    create_ledger(client, "Audit Sales", "Sales Accounts")
    fy1 = _period(client, "audit")
    _period(client, "audit_next")
    _sale(client, "Audit Sales", "250.00", "2011-11-06")
    client.post(f"/api/v1/periods/{fy1['id']}/lock")
    assert client.post(f"/api/v1/periods/{fy1['id']}/close").status_code == 200

    from sqlalchemy import text

    rows = db_session.execute(
        text("SELECT operation, actor FROM audit_log WHERE table_name = 'periods' ORDER BY id")
    ).all()
    assert [r[0] for r in rows].count("INSERT") >= 1
    updates = [r for r in rows if r[0] == "UPDATE"]
    assert updates, "lock and close must be journaled"
    # every mutating action records its principal (dev mode = 'anonymous';
    # the OIDC roles test earlier in this module locks as 'user-1')
    assert all(r[1] for r in updates)
    assert {r[1] for r in updates} <= {"anonymous", "user-1"}
    period = client.get(f"/api/v1/periods/{fy1['id']}").json()
    assert period["closed_by"] == "anonymous"
