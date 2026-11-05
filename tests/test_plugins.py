"""The plugin runtime: GST transformer, policy validator, after-commit events,
API-compatibility guard, and entry-point discovery."""

from __future__ import annotations

import types
from datetime import date, timedelta
from decimal import Decimal

from conftest import create_ledger, post_voucher


def test_gst_transformer_splits_tax(client, fresh_db):
    """meta.gst turns a 1000 sale into 1180 party debit + CGST/SGST 90 each —
    the exact shape TallyPrime produces, with exact decimal math."""
    create_ledger(client, "City Traders", "Sundry Debtors")
    create_ledger(client, "GST Sales", "Sales Accounts")

    r = post_voucher(
        client,
        meta={"gst": {"rate": "18"}},
        entries=[
            {"ledger_name": "City Traders", "dc": "D", "amount": "1000.00"},
            {"ledger_name": "GST Sales", "dc": "C", "amount": "1000.00"},
        ],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    lines = {(e["ledger_name"], e["dc"]): Decimal(e["amount"]) for e in body["entries"]}
    assert lines[("City Traders", "D")] == Decimal("1180.00")
    assert lines[("GST Sales", "C")] == Decimal("1000.00")
    assert lines[("CGST Payable", "C")] == Decimal("90.00")
    assert lines[("SGST Payable", "C")] == Decimal("90.00")

    # tax ledgers auto-created under Duties & Taxes by the plugin
    ledgers = {row["name"]: row for row in client.get("/api/v1/ledgers").json()}
    assert ledgers["CGST Payable"]["group_name"] == "Duties & Taxes"
    assert ledgers["SGST Payable"]["group_name"] == "Duties & Taxes"


def test_gst_odd_rate_splits_exactly(client, fresh_db):
    """Rate 15 on 333.33: tax 50.00 → CGST 25.00 + SGST 25.00; the split
    always sums to the total tax exactly (remainder arithmetic)."""
    create_ledger(client, "Odd Traders", "Sundry Debtors")
    create_ledger(client, "Odd Sales", "Sales Accounts")
    r = post_voucher(
        client,
        meta={"gst": {"rate": "15"}},
        entries=[
            {"ledger_name": "Odd Traders", "dc": "D", "amount": "333.33"},
            {"ledger_name": "Odd Sales", "dc": "C", "amount": "333.33"},
        ],
    )
    assert r.status_code == 201, r.text
    lines = {(e["ledger_name"], e["dc"]): Decimal(e["amount"]) for e in r.json()["entries"]}
    assert lines[("Odd Traders", "D")] == Decimal("383.33")
    assert lines[("CGST Payable", "C")] + lines[("SGST Payable", "C")] == Decimal("50.00")


def test_weekend_policy_validator(client, fresh_db):
    create_ledger(client, "Policy Sales", "Sales Accounts")
    last_sunday = date.today() - timedelta(days=date.today().weekday() + 1)

    r = post_voucher(
        client,
        date_=last_sunday.isoformat(),
        meta={"policy:no_weekends": True},
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "10.00"},
            {"ledger_name": "Policy Sales", "dc": "C", "amount": "10.00"},
        ],
    )
    assert r.status_code == 422
    assert "weekend" in r.json()["detail"].lower()

    # without the policy flag the same Sunday posting is fine
    r = post_voucher(
        client,
        date_=last_sunday.isoformat(),
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "10.00"},
            {"ledger_name": "Policy Sales", "dc": "C", "amount": "10.00"},
        ],
    )
    assert r.status_code == 201, r.text


def test_posted_event_recorded(client, fresh_db):
    from ledgerforge.contrib import example_gst

    create_ledger(client, "Event Sales", "Sales Accounts")
    example_gst.POSTED_EVENTS.clear()
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "42.00"},
            {"ledger_name": "Event Sales", "dc": "C", "amount": "42.00"},
        ],
    )
    assert r.status_code == 201, r.text
    assert example_gst.POSTED_EVENTS[-1]["voucher_no"] == r.json()["voucher_no"]


def test_api_compatibility_guard():
    """A plugin targeting an incompatible plugin API is refused loudly."""
    from ledgerforge.plugins import PluginManager, PluginManifest

    incompatible = types.ModuleType("incompatible_plugin")
    incompatible.PLUGIN = PluginManifest(name="bad", version="0.0.1", api_compat="9.9")
    incompatible.register = lambda registry: None  # noqa: E731
    mgr = PluginManager()
    try:
        mgr.load_module(incompatible)
        raise AssertionError("should have refused to load")
    except RuntimeError as exc:
        assert "plugin API" in str(exc)


def test_entry_point_discovery():
    """The example plugin is discoverable via the ledgerforge.plugins
    entry-point group (this is how pip-installed packs load)."""
    from importlib.metadata import entry_points

    from ledgerforge.plugins import PluginManager

    names = {ep.name for ep in entry_points(group="ledgerforge.plugins")}
    assert "example_gst" in names

    mgr = PluginManager()
    mgr.discover()
    assert any(m.name == "example-gst" for m in mgr.loaded)
