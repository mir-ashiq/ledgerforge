"""Multi-currency behavior (issue #4, ADR-007): currencies & rates masters,
the base-currency posting rules, the FX snapshot on entries, realized-gain
workflow, and the database-level defense of the snapshot invariant."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest
from conftest import create_currency, create_fx_rate, create_ledger, post_voucher
from sqlalchemy import text

FY_RATE = "83.5050000000"  # USD -> INR, NUMERIC(30,10) shape


def _fx_line(ledger_name: str, dc: str, amount_fx: str, rate: str = FY_RATE) -> dict:
    expected = (Decimal(amount_fx) * Decimal(rate)).quantize(
        Decimal("0.00000001"), rounding=ROUND_HALF_UP
    )
    return {
        "ledger_name": ledger_name,
        "dc": dc,
        "amount": format(expected, "f"),
        "currency": "USD",
        "amount_fx": amount_fx,
        "fx_rate": rate,
    }


@pytest.fixture(scope="module", autouse=True)
def _fx_masters(client_module, fresh_db):
    """USD + a reference rate, once for this module's fresh book (fresh_db is
    the dependency that resets the shared test database for this module)."""
    create_currency(client_module, "USD", "US Dollar", symbol="$")
    create_fx_rate(client_module, "USD", "INR", FY_RATE, "2026-01-01", source="test")


# ------------------------------------------------------------------ masters
def test_seeded_base_currency_is_listed(client):
    currencies = {c["code"]: c for c in client.get("/api/v1/currencies").json()}
    assert currencies["INR"]["is_system"] is True
    assert currencies["INR"]["minor_unit"] == 2
    assert currencies["USD"]["name"] == "US Dollar"


def test_currency_duplicate_and_update(client):
    r = client.post("/api/v1/currencies", json={"code": "usd", "name": "Duplicate"})
    assert r.status_code == 422  # normalized to USD -> duplicate
    r = client.patch("/api/v1/currencies/USD", json={"symbol": "US$"})
    assert r.status_code == 200
    assert r.json()["symbol"] == "US$"
    assert client.patch("/api/v1/currencies/EUR", json={"symbol": "€"}).status_code == 404


def test_fx_rate_rules_and_resolution(client):
    create_fx_rate(client, "USD", "INR", "84.1000000000", "2026-11-06", source="rbi")
    # duplicate pair+day -> 422 with the PATCH hint
    dup = client.post(
        "/api/v1/fx-rates",
        json={"from_ccy": "USD", "to_ccy": "INR", "rate": "85", "valid_on": "2026-11-06"},
    )
    assert dup.status_code == 422
    assert "PATCH" in dup.json()["detail"]
    # exact day
    exact = client.get("/api/v1/fx-rates/resolve?from_ccy=USD&to_ccy=INR&on=2026-11-06").json()
    assert exact["rate"] == "84.1000000000"
    # later day falls back to the latest on-or-before row
    later = client.get("/api/v1/fx-rates/resolve?from_ccy=USD&to_ccy=INR&on=2026-12-25").json()
    assert later["rate"] == "84.1000000000"
    # earlier day falls back to the 2026-01-01 row
    earlier = client.get("/api/v1/fx-rates/resolve?from_ccy=USD&to_ccy=INR&on=2026-06-01").json()
    assert earlier["rate"] == FY_RATE
    # no row at all
    missing = client.get("/api/v1/fx-rates/resolve?from_ccy=USD&to_ccy=INR&on=2025-12-31")
    assert missing.status_code == 404
    # correction is an UPDATE, not a new row; provenance survives a PATCH
    # that does not mention it
    rate_id = exact["id"]
    patched = client.patch(f"/api/v1/fx-rates/{rate_id}", json={"rate": "84.2000000000"})
    assert patched.status_code == 200
    assert patched.json()["rate"] == "84.2000000000"
    assert patched.json()["source"] == "rbi"
    corrected = client.patch(
        f"/api/v1/fx-rates/{rate_id}", json={"rate": "84.2500000000", "source": "manual"}
    )
    assert corrected.json()["source"] == "manual"


def test_fx_pairs_must_differ_and_exist(client):
    r = client.post(
        "/api/v1/fx-rates",
        json={"from_ccy": "USD", "to_ccy": "USD", "rate": "1", "valid_on": "2026-11-06"},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/v1/fx-rates",
        json={"from_ccy": "EUR", "to_ccy": "INR", "rate": "90", "valid_on": "2026-11-06"},
    )
    assert r.status_code == 404  # EUR not created (same convention as unknown ledgers)
    assert "does not exist" in r.json()["detail"]


# ------------------------------------------------------------- posting rules
def test_fx_voucher_snapshots_the_trio(client):
    create_ledger(client, "USD Bank", "Bank Accounts")
    r = post_voucher(
        client,
        entries=[
            _fx_line("USD Bank", "D", "100.00"),
            {"ledger_name": "Cash", "dc": "C", "amount": "8350.50000000"},
        ],
    )
    assert r.status_code == 201, r.text
    entry = next(e for e in r.json()["entries"] if e["ledger_name"] == "USD Bank")
    assert entry["currency"] == "USD"
    assert entry["amount_fx"] == "100.00000000"
    assert entry["fx_rate"] == FY_RATE
    assert entry["amount"] == "8350.50000000"
    base_line = next(e for e in r.json()["entries"] if e["ledger_name"] == "Cash")
    assert base_line["currency"] is None
    assert base_line["fx_rate"] is None


def test_fx_amount_must_be_the_exact_product(client):
    create_ledger(client, "USD Bank 2", "Bank Accounts")
    r = post_voucher(
        client,
        entries=[
            {
                "ledger_name": "USD Bank 2",
                "dc": "D",
                "amount": "8350.51",  # off by a fraction of a paisa
                "currency": "USD",
                "amount_fx": "100.00",
                "fx_rate": FY_RATE,
            },
            {"ledger_name": "Cash", "dc": "C", "amount": "8350.51"},
        ],
    )
    assert r.status_code == 422
    assert "8350.50000000" in r.json()["detail"]  # the expected figure is in the error


def test_fx_trio_is_all_or_none(client):
    create_ledger(client, "USD Bank 3", "Bank Accounts")
    r = post_voucher(
        client,
        entries=[
            {
                "ledger_name": "USD Bank 3",
                "dc": "D",
                "amount": "8350.50",
                "currency": "USD",  # amount_fx / fx_rate missing
            },
            {"ledger_name": "Cash", "dc": "C", "amount": "8350.50"},
        ],
    )
    assert r.status_code == 422
    assert "together" in r.json()["detail"][0]["msg"]


def test_base_currency_line_rejects_the_trio(client):
    create_ledger(client, "Local Bank", "Bank Accounts")
    r = post_voucher(
        client,
        entries=[
            {
                "ledger_name": "Local Bank",
                "dc": "D",
                "amount": "100.00",
                "currency": "INR",
                "amount_fx": "100.00",
                "fx_rate": "1.0000000000",
            },
            {"ledger_name": "Cash", "dc": "C", "amount": "100.00"},
        ],
    )
    assert r.status_code == 422
    assert "base currency" in r.json()["detail"]


def test_unknown_currency_and_bad_rate_shapes(client):
    create_ledger(client, "USD Bank 4", "Bank Accounts")
    r = post_voucher(
        client,
        entries=[
            {
                "ledger_name": "USD Bank 4",
                "dc": "D",
                "amount": "8350.50",
                "currency": "CHF",
                "amount_fx": "100.00",
                "fx_rate": FY_RATE,
            },
            {"ledger_name": "Cash", "dc": "C", "amount": "8350.50"},
        ],
    )
    assert r.status_code == 404
    assert "CHF" in r.json()["detail"]

    # Law 3 on the wire: a JSON float rate is rejected outright
    r = post_voucher(
        client,
        entries=[
            {
                "ledger_name": "USD Bank 4",
                "dc": "D",
                "amount": "8350.50",
                "currency": "USD",
                "amount_fx": "100.00",
                "fx_rate": 83.505,
            },
            {"ledger_name": "Cash", "dc": "C", "amount": "8350.50"},
        ],
    )
    assert r.status_code == 422
    assert "string" in r.json()["detail"][0]["msg"] or "float" in r.text.lower()

    # rates carry at most 10 decimals (NUMERIC(30,10))
    r = post_voucher(
        client,
        entries=[
            {
                "ledger_name": "USD Bank 4",
                "dc": "D",
                "amount": "8350.50",
                "currency": "USD",
                "amount_fx": "100.00",
                "fx_rate": "83.50500000001",  # 11 dp
            },
            {"ledger_name": "Cash", "dc": "C", "amount": "8350.50"},
        ],
    )
    assert r.status_code == 422


def test_fx_voucher_must_balance_in_base_currency(client):
    """Law 1 applies to the base amounts — the whole point of the rules."""
    create_ledger(client, "USD Bank 5", "Bank Accounts")
    r = post_voucher(
        client,
        entries=[
            _fx_line("USD Bank 5", "D", "100.00"),  # 8350.50 base
            {"ledger_name": "Cash", "dc": "C", "amount": "8350.00"},  # 0.50 short
        ],
    )
    assert r.status_code == 422
    assert "does not equal total credit" in r.json()["detail"]


def test_realized_gain_workflow(client):
    """Buy USD 100 @ 83.505, settle at 84.00 — the difference lands on the
    seeded Realized Exchange Gain ledger and the P&L sees it."""
    create_ledger(client, "USD Desk", "Bank Accounts")
    buy = post_voucher(
        client,
        narration="buy USD",
        entries=[
            _fx_line("USD Desk", "D", "100.00", rate="83.5050000000"),
            {"ledger_name": "Cash", "dc": "C", "amount": "8350.50000000"},
        ],
    )
    assert buy.status_code == 201, buy.text
    sell = post_voucher(
        client,
        narration="sell USD, realized gain",
        entries=[
            _fx_line("USD Desk", "C", "100.00", rate="84.0000000000"),  # 8400 base
            {"ledger_name": "Cash", "dc": "D", "amount": "8400.00000000"},
            # the desk was carried at 83.505 and leaves at 84.00: book the
            # 49.50 base-currency difference as the realized gain
            {"ledger_name": "USD Desk", "dc": "D", "amount": "49.50000000"},
            {"ledger_name": "Realized Exchange Gain", "dc": "C", "amount": "49.50000000"},
        ],
    )
    assert sell.status_code == 201, sell.text

    pl = client.get("/api/v1/reports/profit-loss?from=2020-01-01&to=2100-01-01").json()
    assert pl["profit_and_loss"]["net_profit"] == "49.50000000"
    tb = client.get("/api/v1/reports/trial-balance").json()
    assert Decimal(tb["totals"]["difference"]) == 0  # still exactly balanced
    usd_desk = next(ln for ln in tb["ledgers"] if ln["ledger_name"] == "USD Desk")
    assert Decimal(usd_desk["balance"]) == 0  # position closed


def test_reversal_carries_the_fx_snapshot(client):
    create_ledger(client, "USD Bank 6", "Bank Accounts")
    original = post_voucher(
        client,
        entries=[
            _fx_line("USD Bank 6", "D", "50.00"),
            {"ledger_name": "Cash", "dc": "C", "amount": "4175.25000000"},
        ],
    ).json()
    r = client.post(f"/api/v1/vouchers/{original['id']}/reverse")
    assert r.status_code == 201, r.text
    reversal = r.json()
    fx_lines = [e for e in reversal["entries"] if e["currency"] == "USD"]
    assert len(fx_lines) == 1
    assert fx_lines[0]["amount_fx"] == "50.00000000"
    assert fx_lines[0]["fx_rate"] == FY_RATE
    assert fx_lines[0]["dc"] == "C"  # mirrored
    # and the ledger position is exactly zero again
    tb = client.get("/api/v1/reports/trial-balance").json()
    desk = next(ln for ln in tb["ledgers"] if ln["ledger_name"] == "USD Bank 6")
    assert Decimal(desk["balance"]) == 0


def test_gst_meta_refused_on_fx_lines(client):
    create_ledger(client, "USD Bank 7", "Bank Accounts")
    r = post_voucher(
        client,
        meta={"gst": {"rate": "18"}},
        entries=[
            _fx_line("USD Bank 7", "D", "100.00"),
            {"ledger_name": "Local Sales", "dc": "C", "amount": "8350.50000000"},
        ],
    )
    assert r.status_code == 422
    assert "foreign-currency" in r.json()["detail"]


# --------------------------------------------------- database-level defense
def test_database_check_rejects_inconsistent_snapshot(db_session):
    """The engine is not the only guardian: chk_entry_fx_consistent holds for
    ANY writer, exactly like the Law 1 deferred trigger."""
    db_session.execute(
        text(
            """
            INSERT INTO voucher_types (code, name, number_prefix, restart_yearly)
            VALUES ('FXC', 'FX check probe', 'FXC', TRUE)
            ON CONFLICT (code) DO NOTHING
            """
        )
    )
    db_session.execute(
        text(
            """
            INSERT INTO vouchers (client_token, voucher_type_id, voucher_no, sequence,
                                  voucher_date)
            VALUES (gen_random_uuid(),
                    (SELECT id FROM voucher_types WHERE code = 'FXC'),
                    'FXC-PROBE-1', 1, CURRENT_DATE)
            """
        )
    )
    with pytest.raises(Exception, match="chk_entry_fx_consistent"):
        db_session.execute(
            text(
                """
                INSERT INTO voucher_entries
                    (voucher_id, line_no, ledger_id, dc, amount, voucher_date,
                     currency, amount_fx, fx_rate)
                VALUES ((SELECT id FROM vouchers WHERE voucher_no = 'FXC-PROBE-1'),
                        1, (SELECT id FROM ledgers WHERE name = 'Cash'), 'D',
                        999.00, CURRENT_DATE, 'USD', 100.00, 83.5050000000)
                """
            )
        )
    db_session.rollback()


def test_rate_rows_are_correctable_but_not_deletable_by_role(db_session):
    """ledgerforge_app holds SELECT/INSERT/UPDATE on fx_rates — never DELETE
    (rates are corrected, not erased); the grants are part of migration 0002."""
    db_session.rollback()
    grants = {
        (row[0], row[1])
        for row in db_session.execute(
            text(
                """
                SELECT table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE grantee = 'ledgerforge_app'
                  AND table_name IN ('currencies', 'fx_rates', 'periods')
                """
            )
        )
    }
    assert ("currencies", "DELETE") in grants
    assert ("currencies", "UPDATE") in grants
    assert ("fx_rates", "UPDATE") in grants
    assert ("fx_rates", "INSERT") in grants
    assert ("fx_rates", "DELETE") not in grants
    assert ("periods", "DELETE") in grants  # guarded by trg_periods_delete_guard
