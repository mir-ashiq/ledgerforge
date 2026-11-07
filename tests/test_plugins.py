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


def test_gst_odd_amount_splits_exactly(client, fresh_db):
    """Rate 5 on 333.33: tax 16.67 → CGST 8.34 + SGST 8.33; the split
    always sums to the total tax exactly (remainder arithmetic). The v0.13
    GST pack whitelists rates to the statutory slabs — a non-slab rate
    (15, 20, …) is a fat-finger veto, tested in test_gst_plugin.py."""
    create_ledger(client, "Odd Traders", "Sundry Debtors")
    create_ledger(client, "Odd Sales", "Sales Accounts")
    r = post_voucher(
        client,
        meta={"gst": {"rate": "5"}},
        entries=[
            {"ledger_name": "Odd Traders", "dc": "D", "amount": "333.33"},
            {"ledger_name": "Odd Sales", "dc": "C", "amount": "333.33"},
        ],
    )
    assert r.status_code == 201, r.text
    lines = {(e["ledger_name"], e["dc"]): Decimal(e["amount"]) for e in r.json()["entries"]}
    assert lines[("Odd Traders", "D")] == Decimal("350.00")
    assert lines[("CGST Payable", "C")] == Decimal("8.34")  # half-up of 8.335
    assert lines[("SGST Payable", "C")] == Decimal("8.33")  # the remainder
    assert lines[("CGST Payable", "C")] + lines[("SGST Payable", "C")] == Decimal("16.67")

    # a non-slab rate is refused loudly (the compliance pack's whitelist)
    r = post_voucher(
        client,
        meta={"gst": {"rate": "15"}},
        entries=[
            {"ledger_name": "Odd Traders", "dc": "D", "amount": "333.33"},
            {"ledger_name": "Odd Sales", "dc": "C", "amount": "333.33"},
        ],
    )
    assert r.status_code == 422
    assert "not a GST slab" in r.json()["detail"]


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
    from ledgerforge.contrib import gst

    create_ledger(client, "Event Sales", "Sales Accounts")
    gst.POSTED_EVENTS.clear()
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "42.00"},
            {"ledger_name": "Event Sales", "dc": "C", "amount": "42.00"},
        ],
    )
    assert r.status_code == 201, r.text
    assert gst.POSTED_EVENTS[-1]["voucher_no"] == r.json()["voucher_no"]


def test_master_created_event_fires_for_ledger(client, fresh_db):
    """The documented-but-never-fired taxonomy kept: creating a ledger
    emits master.created with the master's identity and placement."""
    events: list[dict] = []
    client.app.state.plugin_manager.registry.on("master.created", lambda e: events.append(e.data))

    body = create_ledger(client, "Event Customer", "Sundry Debtors")

    assert len(events) == 1
    assert events[0] == {
        "kind": "ledger",
        "book_id": events[0]["book_id"],
        "id": body["id"],
        "name": "Event Customer",
        "code": body["code"],
        "group_id": body["group_id"],
        "group_name": "Sundry Debtors",
    }
    assert isinstance(events[0]["book_id"], int)  # the selected book, present and real


def test_master_created_event_fires_for_group(client, fresh_db):
    events: list[dict] = []
    client.app.state.plugin_manager.registry.on("master.created", lambda e: events.append(e.data))

    r = client.post(
        "/api/v1/groups", json={"name": "Branch Debtors", "parent_name": "Sundry Debtors"}
    )
    assert r.status_code == 201, r.text
    body = r.json()

    assert len(events) == 1
    assert events[0]["kind"] == "group"
    assert events[0]["name"] == "Branch Debtors"
    assert events[0]["parent_id"] is not None
    assert events[0]["nature"] == body["nature"]
    assert isinstance(events[0]["book_id"], int)


def test_master_created_event_failure_is_isolated(client, fresh_db):
    """A broken listener never breaks the master's creation — the same
    guarantee voucher.posted handlers get."""

    def boom(_event):  # noqa: ANN001
        raise RuntimeError("listener exploded")

    registry = client.app.state.plugin_manager.registry
    registry.on("master.created", boom)
    r = client.post(
        "/api/v1/ledgers", json={"name": "Survivor Ledger", "group_name": "Sundry Debtors"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "Survivor Ledger"


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


def test_api_compat_is_a_floor():
    """api_compat declares the minor a plugin was BUILT against, not a pin
    (ADR-011): 0.2- through 0.5-era plugins keep loading on this 0.6 build
    — additions are backwards compatible — while anything from a newer
    minor refuses."""
    from ledgerforge.plugins import PluginManager, PluginManifest

    def _plugin(name: str, api_compat: str):
        module = types.ModuleType(f"{name}_plugin")
        module.PLUGIN = PluginManifest(name=name, version="0.0.1", api_compat=api_compat)
        module.register = lambda registry: None  # noqa: E731
        return module

    mgr = PluginManager()
    mgr.load_module(_plugin("old-floor", "0.2"))  # the invoicing pack's era
    mgr.load_module(_plugin("fold-era", "0.3"))  # the inventory pack's era
    mgr.load_module(_plugin("ytd-era", "0.4"))  # the payroll pack's era
    mgr.load_module(_plugin("print-era", "0.5"))  # the print-bridge era
    mgr.load_module(_plugin("current", "0.6"))  # the pack-reports era
    assert [m.name for m in mgr.loaded] == [
        "old-floor",
        "fold-era",
        "ytd-era",
        "print-era",
        "current",
    ]

    try:
        mgr.load_module(_plugin("from-the-future", "0.9"))
        raise AssertionError("should have refused to load")
    except RuntimeError as exc:
        assert "plugin API" in str(exc)


def test_api_compat_refuses_newer_minor_on_older_build(monkeypatch):
    from ledgerforge import plugins as runtime

    monkeypatch.setattr(runtime, "PLUGIN_API_VERSION", "0.3")
    module = types.ModuleType("needs_new_api")
    module.PLUGIN = runtime.PluginManifest(name="needs-new-api", version="0.0.1", api_compat="0.4")
    module.register = lambda registry: None  # noqa: E731
    try:
        runtime.PluginManager().load_module(module)
        raise AssertionError("should have refused to load")
    except RuntimeError as exc:
        assert "plugin API" in str(exc)


def test_api_compat_rejects_garbage():
    from ledgerforge.plugins import _api_compatible

    for bad in ("", "0", "x.y", "abc", None, 3):
        assert _api_compatible(bad) is False, bad


def test_resolver_prior_documents_provenance(client, fresh_db, db_session):
    """The document-store read (API 0.3, ADR-011): prior_documents returns
    the stored payloads oldest-first, and a reversal rides along with its
    ``reversal_of`` set so a fold can net the pair — everything book-scoped."""
    from sqlalchemy import select

    from ledgerforge.models import Book
    from ledgerforge.plugins import Resolver

    posted = post_voucher(
        client,
        type_="PUR",
        meta={
            "stock": {
                "movement": "in",
                "party": "Omega Supplies",
                "items": [{"item": "Widgets", "qty": "3", "rate": "100.00"}],
            }
        },
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "0.01"},
            {"ledger_name": "Cash", "dc": "C", "amount": "0.01"},
        ],
    )
    assert posted.status_code == 201, posted.text
    original_id = posted.json()["id"]

    reversal = client.post(f"/api/v1/vouchers/{original_id}/reverse")
    assert reversal.status_code == 201, reversal.text
    reversal_id = reversal.json()["id"]

    book = db_session.scalar(select(Book).where(Book.slug == "default"))
    resolver = Resolver(db_session, book)
    docs = resolver.prior_documents("stock")
    by_id = {d.voucher_id: d for d in docs}
    assert original_id in by_id and reversal_id in by_id
    assert by_id[original_id].reversal_of is None
    assert by_id[original_id].meta["stock"]["movement"] == "in"
    assert by_id[reversal_id].reversal_of == original_id
    assert by_id[reversal_id].meta == {}  # reversals are new documents (ADR-010)
    assert [d.voucher_id for d in docs] == sorted(by_id)  # oldest first

    # key-scoped: a voucher carrying a different meta key is invisible
    other = post_voucher(
        client,
        meta={"gst": {"rate": "18"}},
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "100.00"},
            {"ledger_name": "Cash", "dc": "C", "amount": "100.00"},
        ],
    )
    assert other.status_code == 201, other.text
    assert all(d.voucher_id != other.json()["id"] for d in resolver.prior_documents("stock"))


def test_resolver_lock_is_namespaced_and_reentrant(client, fresh_db, db_session):
    """resolver.lock takes a transaction-scoped advisory lock keyed by the
    book and the plugin's name (API 0.3). Within one transaction the same
    name is reentrant; the namespacing is proven at the API level by the
    inventory pack's concurrency test."""
    from sqlalchemy import select

    from ledgerforge.models import Book
    from ledgerforge.plugins import Resolver

    book = db_session.scalar(select(Book).where(Book.slug == "default"))
    resolver = Resolver(db_session, book)
    resolver.lock("stock:Widgets")
    resolver.lock("stock:Widgets")  # pg advisory xact locks are reentrant
    resolver.lock("anything:else")
    db_session.rollback()  # xact-scoped: released, nothing persists


def test_entry_point_discovery():
    """The GST pack is discoverable via the ledgerforge.plugins entry-point
    group (this is how pip-installed packs load) — the v0.13 release that
    grew example_gst into a real localization plugin swapped the entry-point
    (ADR-017); the example stays importable as the plugin guide's teaching
    module, but only the gst pack owns meta.gst in a running server."""
    from importlib.metadata import entry_points

    from ledgerforge.plugins import PluginManager

    names = {ep.name for ep in entry_points(group="ledgerforge.plugins")}
    assert "gst" in names
    assert "example_gst" not in names

    mgr = PluginManager()
    mgr.discover()
    assert any(m.name == "gst" for m in mgr.loaded)
    assert not any(m.name == "example-gst" for m in mgr.loaded)
