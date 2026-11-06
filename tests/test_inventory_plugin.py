"""The inventory plugin pack (contrib/inventory.py): the second business
document pack, built purely on the runtime — and the first roadmap feature
to land with zero schema change (ADR-011).

The promises under test (ADR-003 + ADR-010 + ADR-011):
- ``meta.stock`` documents derive the accounting: goods-in posts the
  perpetual TallyPrime purchase shape (Dr inventory per item, Cr supplier);
  goods-out posts COGS at the **moving weighted average** and, with a
  party, the full perpetual sale (Dr party / Cr revenue / Dr COGS /
  Cr inventory); transfers move value between godown ledgers at average
  cost; recounts adjust inventory against *Stock Adjustment*.
- Positions are derived, never stored: the fold over posted documents
  always agrees with the transformer's computed annotations, and a
  reversal nets its original out (original + reversal = zero on stock).
- ``value`` only moves by posted 2-dp line amounts: issuing the last unit
  costs exactly the remaining value — positions empty to 0.00 (Law 3).
- Concurrency: same-item movements serialize through transaction-scoped
  advisory locks (resolver.lock, API 0.3) — concurrent outs cannot
  oversell.
- Masters resolve/create through the book-scoped Resolver only; books are
  strangers (same item in two books = two ledgers, two positions).
- Guards (all 422): insufficient stock (policy-overridable, never from an
  empty position), document-mode conflicts, FX lines, future dates, item
  names colliding with unrelated ledgers, zero-value recounts.
"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from conftest import TODAY

from ledgerforge.contrib import inventory


# ------------------------------------------------------------------ helpers
def default_headers(client) -> dict | None:
    """Once a second book exists, requests must name one: pin to the default
    book (the books-are-strangers test creates Zeta mid-module)."""
    books = client.get("/api/v1/books").json()
    if len(books) == 1:
        return None
    default_id = next(b["id"] for b in books if b["slug"] == "default")
    return {"X-Book-Id": str(default_id)}


def post_stock(
    client,
    movement: str,
    items: list[dict],
    *,
    party: str | None = None,
    type_: str = "SAL",
    date_: str | None = None,
    headers: dict | None = None,
    token=None,
    meta_extra: dict | None = None,
    **doc_extra,
):
    """Post a stock document. ``meta_extra`` lands at the TOP meta level
    (policy flags, other packs' keys for conflict probes); ``doc_extra``
    lands inside the stock document (reference, …)."""
    doc: dict = {"movement": movement, "items": items}
    if party is not None:
        doc["party"] = party
    doc.update(doc_extra)
    meta: dict = {"stock": doc}
    if meta_extra:
        meta.update(meta_extra)
    payload = {
        "voucher_type": type_,
        "voucher_date": date_ or TODAY,
        "entries": [  # placeholders — the transformer replaces them
            {"ledger_name": "Cash", "dc": "D", "amount": "0.01"},
            {"ledger_name": "Cash", "dc": "C", "amount": "0.01"},
        ],
        "meta": meta,
    }
    if token is not None:
        payload["client_token"] = str(token)
    return client.post("/api/v1/vouchers", json=payload, headers=headers or default_headers(client))


def goods_in(client, item: str, qty: str, rate: str, **kw):
    return post_stock(
        client,
        "in",
        [{"item": item, "qty": qty, "rate": rate}],
        party=kw.pop("party", "Omega Supplies"),
        type_="PUR",
        **kw,
    )


def lines_by_ledger(body: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in body["entries"]:
        out.setdefault(e["ledger_name"], []).append(e)
    return out


def ledger_groups(client, headers: dict | None = None) -> dict[str, str]:
    return {
        row["name"]: row["group_name"]
        for row in client.get("/api/v1/ledgers", headers=headers or default_headers(client)).json()
    }


def computed_of(body: dict, item: str) -> dict:
    return next(it for it in body["meta"]["stock"]["computed"]["items"] if it["item"] == item)


def _resolver(db_session):
    from sqlalchemy import select

    from ledgerforge.models import Book
    from ledgerforge.plugins import Resolver

    book = db_session.scalar(select(Book).where(Book.slug == "default"))
    return Resolver(db_session, book)


# --------------------------------------------------------------- goods-in
def test_goods_in_builds_tally_shape(client, fresh_db):
    """Two items in one purchase: Cr supplier for the total, one Dr per item
    on its Stock-in-Hand ledger — and the document round-trips (ADR-010)."""
    inventory.MOVEMENTS.clear()
    r = post_stock(
        client,
        "in",
        [
            {"item": "Widgets", "qty": "10", "rate": "100.00"},
            {"item": "Sprockets", "qty": "4", "rate": "50.00"},
        ],
        party="Omega Supplies",
        type_="PUR",
        reference="PO-17",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    lines = lines_by_ledger(body)

    supplier = lines["Omega Supplies"][0]
    assert supplier["dc"] == "C"
    assert Decimal(supplier["amount"]) == Decimal("1200.00")
    assert supplier["narration"] == "goods from Omega Supplies; PO-17"

    assert Decimal(lines["Widgets"][0]["amount"]) == Decimal("1000.00")
    assert lines["Widgets"][0]["narration"] == "Widgets × 10 @ 100.00"
    assert Decimal(lines["Sprockets"][0]["amount"]) == Decimal("200.00")

    computed = body["meta"]["stock"]["computed"]
    assert computed["total"] == "1200.00"
    assert computed_of(body, "Widgets") == {
        "item": "Widgets",
        "line_amount": "1000.00",
        "qty_after": "10",
        "avg_cost_after": "100.0000",
    }
    assert computed_of(body, "Sprockets")["avg_cost_after"] == "50.0000"

    got = client.get(f"/api/v1/vouchers/{body['id']}").json()
    assert got["meta"]["stock"]["computed"] == computed  # persistence, not echo

    groups = ledger_groups(client)
    assert groups["Omega Supplies"] == "Sundry Creditors"
    assert groups["Widgets"] == "Stock-in-Hand"
    assert groups["Sprockets"] == "Stock-in-Hand"

    assert len(inventory.MOVEMENTS) == 1
    record = inventory.MOVEMENTS[0]
    assert record["kind"] == "in"
    assert record["voucher_no"] == body["voucher_no"]
    assert record["party"] == "Omega Supplies"
    assert record["items"] == [{"item": "Widgets", "qty": "10"}, {"item": "Sprockets", "qty": "4"}]


def test_goods_in_validations(client, fresh_db):
    cases = [
        {"movement": "in", "items": []},  # empty items
        {"movement": "sideways", "items": [{"item": "X", "qty": "1", "rate": "1"}]},
        {"movement": "in", "items": [{"item": "X", "qty": "0", "rate": "1"}]},
        {"movement": "in", "items": [{"item": "X", "qty": "1", "rate": "-1"}]},
        {"movement": "in", "items": [{"item": "", "qty": "1", "rate": "1"}]},
        {"movement": "in", "items": ["not-an-object"]},
        {"movement": "in", "items": [{"item": "X", "qty": "1", "rate": "1"}]},  # no party
    ]
    for case in cases:
        doc = dict(case)
        movement, items = doc.pop("movement"), doc["items"]
        has_party = "party" in doc or not items or not isinstance(items[0], dict)
        r = post_stock(
            client,
            movement,
            items,
            party="Omega Supplies" if has_party else None,
            type_="PUR",
        )
        assert r.status_code == 422, (case, r.text)


# ------------------------------------------------------- weighted average
def test_moving_weighted_average_costs(client, fresh_db):
    """1@100 then 2@101: avg 100.6667; issuing 1 costs 100.67, issuing the
    last 2 costs exactly the remaining 201.33 — the position empties to
    0.00 with no rounding residue (Law 3 discipline on derived state)."""
    r1 = goods_in(client, "Gears", "1", "100.00")
    assert r1.status_code == 201, r1.text
    r2 = goods_in(client, "Gears", "2", "101.00")
    assert r2.status_code == 201, r2.text
    assert computed_of(r2.json(), "Gears")["avg_cost_after"] == "100.6667"

    out1 = post_stock(client, "out", [{"item": "Gears", "qty": "1"}])
    assert out1.status_code == 201, out1.text
    c1 = computed_of(out1.json(), "Gears")
    assert c1["cost"] == "100.67"
    assert c1["qty_after"] == "2"
    assert c1["avg_cost_after"] == "100.6650"

    out2 = post_stock(client, "out", [{"item": "Gears", "qty": "2"}])
    assert out2.status_code == 201, out2.text
    c2 = computed_of(out2.json(), "Gears")
    assert c2["cost"] == "201.33"
    assert c2["qty_after"] == "0"
    assert c2["avg_cost_after"] is None  # empty position has no average

    assert out2.json()["meta"]["stock"]["computed"]["total_cost"] == "201.33"

    # the ledger side reconciles: COGS == total goods-in value, inventory 0
    cogs = sum(
        (
            Decimal(e["amount"])
            for e in out1.json()["entries"] + out2.json()["entries"]
            if e["ledger_name"] == "Cost of Goods Sold"
        ),
        Decimal(0),
    )
    assert cogs == Decimal("302.00")

    gone = post_stock(client, "out", [{"item": "Gears", "qty": "1"}])
    assert gone.status_code == 422
    assert "no stock" in gone.json()["detail"]


def test_goods_out_with_party_is_perpetual_sale(client, fresh_db):
    """Dr party @ sale value, Cr revenue per item, plus the stock leg
    Dr COGS / Cr inventory at average cost — margin annotated."""
    assert goods_in(client, "Chains", "10", "100.00").status_code == 201
    r = post_stock(
        client,
        "out",
        [{"item": "Chains", "qty": "3", "rate": "150.00"}],
        party="Acme Traders",
        reference="SO-42",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    lines = lines_by_ledger(body)

    assert Decimal(lines["Acme Traders"][0]["amount"]) == Decimal("450.00")
    assert lines["Acme Traders"][0]["dc"] == "D"
    assert lines["Acme Traders"][0]["narration"] == "sale to Acme Traders; SO-42"
    assert Decimal(lines["Sales"][0]["amount"]) == Decimal("450.00")
    assert lines["Sales"][0]["narration"] == "Chains × 3 @ 150.00"
    assert Decimal(lines["Cost of Goods Sold"][0]["amount"]) == Decimal("300.00")
    assert Decimal(lines["Chains"][0]["amount"]) == Decimal("300.00")
    assert lines["Chains"][0]["dc"] == "C"

    computed = body["meta"]["stock"]["computed"]
    assert computed["total_cost"] == "300.00"
    assert computed["revenue_total"] == "450.00"
    assert computed["gross_margin"] == "150.00"

    groups = ledger_groups(client)
    assert groups["Acme Traders"] == "Sundry Debtors"
    assert groups["Sales"] == "Sales Accounts"
    assert groups["Cost of Goods Sold"] == "Direct Expenses"


def test_goods_out_without_party_is_consumption(client, fresh_db):
    """No party → no revenue leg: Dr COGS / Cr inventory only."""
    assert goods_in(client, "Rivets", "10", "100.00").status_code == 201
    r = post_stock(client, "out", [{"item": "Rivets", "qty": "2"}])
    assert r.status_code == 201, r.text
    body = r.json()
    lines = lines_by_ledger(body)
    assert set(lines) == {"Cost of Goods Sold", "Rivets"}
    computed = body["meta"]["stock"]["computed"]
    assert "revenue_total" not in computed and "gross_margin" not in computed
    assert computed_of(body, "Rivets")["qty_after"] == "8"


def test_out_rate_required_with_party(client, fresh_db):
    assert goods_in(client, "Anvils", "1", "100.00").status_code == 201
    r = post_stock(client, "out", [{"item": "Anvils", "qty": "1"}], party="Acme Traders")
    assert r.status_code == 422
    assert "selling rate" in r.json()["detail"]


# --------------------------------------------------------- stock guards
def test_insufficient_stock_guards(client, fresh_db):
    assert goods_in(client, "Nuts", "3", "100.00").status_code == 201
    r = post_stock(client, "out", [{"item": "Nuts", "qty": "5"}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "insufficient stock" in detail and "have 3, need 5" in detail

    allowed = post_stock(
        client,
        "out",
        [{"item": "Nuts", "qty": "5"}],
        meta_extra={"policy:allow_negative_stock": True},
    )
    assert allowed.status_code == 201, allowed.text
    c = computed_of(allowed.json(), "Nuts")
    assert c["cost"] == "500.00"
    assert c["qty_after"] == "-2"

    # an empty position never issues, override or not — there is no cost basis
    for extra in ({}, {"policy:allow_negative_stock": True}):
        r = post_stock(client, "out", [{"item": "Ghosts", "qty": "1"}], meta_extra=extra)
        assert r.status_code == 422, extra
        assert "no stock" in r.json()["detail"]


# -------------------------------------------------------------- transfer
def test_transfer_moves_value_between_godowns(client, fresh_db):
    assert goods_in(client, "Belts", "10", "100.00").status_code == 201
    r = post_stock(
        client,
        "transfer",
        [{"item": "Belts", "qty": "4", "to_ledger": "Belts (Store B)"}],
        type_="JV",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    lines = lines_by_ledger(body)
    assert Decimal(lines["Belts (Store B)"][0]["amount"]) == Decimal("400.00")
    assert lines["Belts (Store B)"][0]["dc"] == "D"
    assert Decimal(lines["Belts"][0]["amount"]) == Decimal("400.00")
    assert lines["Belts"][0]["dc"] == "C"
    assert body["meta"]["stock"]["computed"]["total"] == "400.00"
    assert ledger_groups(client)["Belts (Store B)"] == "Stock-in-Hand"

    # quantities are unchanged: the full 10 can still be issued (at 1000.00)
    out = post_stock(client, "out", [{"item": "Belts", "qty": "10"}])
    assert out.status_code == 201, out.text
    assert computed_of(out.json(), "Belts")["cost"] == "1000.00"


def test_transfer_guards(client, fresh_db):
    assert goods_in(client, "Hoses", "10", "100.00").status_code == 201
    r = post_stock(client, "transfer", [{"item": "Hoses", "qty": "4"}], type_="JV")
    assert r.status_code == 422
    assert "to_ledger is required" in r.json()["detail"]

    r = post_stock(
        client, "transfer", [{"item": "Hoses", "qty": "4", "to_ledger": "Hoses"}], type_="JV"
    )
    assert r.status_code == 422
    assert "must differ" in r.json()["detail"]

    r = post_stock(
        client, "transfer", [{"item": "Hoses", "qty": "11", "to_ledger": "Store B"}], type_="JV"
    )
    assert r.status_code == 422
    assert "transfers cannot go negative" in r.json()["detail"]


# -------------------------------------------------------------- recount
def test_recount_adjusts_both_directions(client, fresh_db):
    assert goods_in(client, "Cams", "10", "100.00").status_code == 201
    assert goods_in(client, "Pins", "4", "50.00").status_code == 201

    r = post_stock(
        client,
        "recount",
        [{"item": "Cams", "qty": "9"}, {"item": "Pins", "qty": "5"}],
        type_="JV",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    lines = lines_by_ledger(body)
    assert Decimal(lines["Cams"][0]["amount"]) == Decimal("100.00")
    assert lines["Cams"][0]["dc"] == "C"  # shrinkage: credit inventory
    assert Decimal(lines["Pins"][0]["amount"]) == Decimal("50.00")
    assert lines["Pins"][0]["dc"] == "D"  # found stock: debit inventory
    assert Decimal(lines["Stock Adjustment"][0]["amount"]) == Decimal("100.00")
    assert Decimal(lines["Stock Adjustment"][1]["amount"]) == Decimal("50.00")

    computed = body["meta"]["stock"]["computed"]
    assert computed_of(body, "Cams") == {
        "item": "Cams",
        "delta_qty": "-1",
        "value_delta": "-100.00",
        "qty_after": "9",
    }
    assert computed_of(body, "Pins")["value_delta"] == "50.00"
    assert computed["total_adjustment"] == "-50.00"
    assert ledger_groups(client)["Stock Adjustment"] == "Direct Expenses"

    # the recounted position values at the same average
    out = post_stock(client, "out", [{"item": "Cams", "qty": "9"}])
    assert out.status_code == 201, out.text
    assert computed_of(out.json(), "Cams")["cost"] == "900.00"


def test_recount_from_zero_requires_rate(client, fresh_db):
    r = post_stock(client, "recount", [{"item": "Gizmos", "qty": "2"}], type_="JV")
    assert r.status_code == 422
    assert "explicit rate" in r.json()["detail"]

    r = post_stock(client, "recount", [{"item": "Gizmos", "qty": "2", "rate": "40.00"}], type_="JV")
    assert r.status_code == 201, r.text
    assert computed_of(r.json(), "Gizmos")["value_delta"] == "80.00"

    out = post_stock(client, "out", [{"item": "Gizmos", "qty": "2"}])
    assert out.status_code == 201, out.text
    assert computed_of(out.json(), "Gizmos")["cost"] == "80.00"


def test_recount_noop_and_negative_rejected(client, fresh_db):
    assert goods_in(client, "Keys", "3", "100.00").status_code == 201
    r = post_stock(client, "recount", [{"item": "Keys", "qty": "3"}], type_="JV")
    assert r.status_code == 422
    assert "changes nothing" in r.json()["detail"]

    r = post_stock(client, "recount", [{"item": "Keys", "qty": "-2"}], type_="JV")
    assert r.status_code == 422
    assert ">= 0" in r.json()["detail"]


# ------------------------------------------------------------- reversals
def test_reversal_nets_out_movements(client, fresh_db):
    """Reversing a goods-out restores the stock (the fold skips the undone
    original); reversing a goods-in removes it — document pairs net to
    zero on stock exactly like their mirrored entries do on the ledgers."""
    first_in = goods_in(client, "Locks", "3", "100.00")
    assert first_in.status_code == 201, first_in.text

    out = post_stock(client, "out", [{"item": "Locks", "qty": "1"}])
    assert out.status_code == 201, out.text
    assert computed_of(out.json(), "Locks")["cost"] == "100.00"

    rev = client.post(
        f"/api/v1/vouchers/{out.json()['id']}/reverse", headers=default_headers(client)
    )
    assert rev.status_code == 201, rev.text
    assert rev.json()["meta"] == {}  # reversals are new documents (ADR-010)

    # position restored to 3 @ 300: the full lot can be issued in one go
    out_all = post_stock(client, "out", [{"item": "Locks", "qty": "3"}])
    assert out_all.status_code == 201, out_all.text
    assert computed_of(out_all.json(), "Locks")["cost"] == "300.00"

    # now reverse the original goods-in: the position is gone
    rev_in = client.post(
        f"/api/v1/vouchers/{first_in.json()['id']}/reverse", headers=default_headers(client)
    )
    assert rev_in.status_code == 201, rev_in.text
    assert post_stock(client, "out", [{"item": "Locks", "qty": "1"}]).status_code == 422


# ------------------------------------------------------- idempotent replay
def test_idempotent_replay_does_not_move_twice(client, fresh_db):
    inventory.MOVEMENTS.clear()
    token = uuid4()
    first = goods_in(client, "Valves", "2", "100.00", token=token)
    assert first.status_code == 201, first.text
    ledgers_after_first = client.get("/api/v1/ledgers", headers=default_headers(client)).json()

    second = goods_in(client, "Valves", "2", "100.00", token=token)
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["id"] == first.json()["id"]
    assert len(inventory.MOVEMENTS) == 1
    assert client.get("/api/v1/ledgers", headers=default_headers(client)).json() == (
        ledgers_after_first
    )

    # and the position reflects ONE goods-in, not two
    out = post_stock(client, "out", [{"item": "Valves", "qty": "2"}])
    assert computed_of(out.json(), "Valves")["cost"] == "200.00"


# ---------------------------------------------------------- book isolation
def test_books_are_strangers(client, fresh_db):
    """Same item, two companies: two inventory ledgers, two independent
    weighted averages — the pack inherits ADR-009 isolation."""
    r = client.post("/api/v1/books", json={"name": "Zeta Ventures", "base_currency": "INR"})
    assert r.status_code == 201, r.text
    zeta_id = r.json()["book"]["id"]
    zeta = {"X-Book-Id": str(zeta_id)}
    books = client.get("/api/v1/books").json()
    default_id = next(b["id"] for b in books if b["slug"] == "default")
    default = {"X-Book-Id": str(default_id)}

    assert goods_in(client, "Cogs", "1", "100.00", headers=default).status_code == 201
    assert goods_in(client, "Cogs", "1", "200.00", headers=zeta).status_code == 201

    second_default = goods_in(client, "Cogs", "1", "102.00", headers=default)
    second_zeta = goods_in(client, "Cogs", "1", "202.00", headers=zeta)
    assert second_default.status_code == 201, second_default.text
    assert second_zeta.status_code == 201, second_zeta.text

    assert computed_of(second_default.json(), "Cogs")["avg_cost_after"] == "101.0000"
    assert computed_of(second_zeta.json(), "Cogs")["avg_cost_after"] == "201.0000"

    default_ledgers = {
        row["name"]: row["id"] for row in client.get("/api/v1/ledgers", headers=default).json()
    }
    zeta_ledgers = {
        row["name"]: row["id"] for row in client.get("/api/v1/ledgers", headers=zeta).json()
    }
    assert default_ledgers["Cogs"] != zeta_ledgers["Cogs"]


# ------------------------------------------------------------ conflicts
def test_document_mode_conflicts(client, fresh_db):
    for other in (
        {"gst": {"rate": "18"}},
        {
            "invoice": {
                "customer": "Acme",
                "items": [{"description": "X", "qty": "1", "rate": "1", "tax_rate": "0"}],
            }
        },
        {"receipt": {"customer": "Acme", "amount": "1.00"}},
    ):
        r = post_stock(
            client,
            "in",
            [{"item": "Washers", "qty": "1", "rate": "100.00"}],
            party="Omega Supplies",
            type_="PUR",
            meta_extra=other,
        )
        assert r.status_code == 422, (other, r.text)
        assert "mutually exclusive" in r.json()["detail"]


def test_fx_lines_rejected_with_stock(client, fresh_db):
    payload = {
        "voucher_type": "JV",
        "voucher_date": TODAY,
        "entries": [
            {
                "ledger_name": "Cash",
                "dc": "D",
                "amount": "83.50",
                "currency": "USD",
                "amount_fx": "1.00",
                "fx_rate": "83.5000000000",
            },
            {"ledger_name": "Cash", "dc": "C", "amount": "83.50"},
        ],
        "meta": {
            "stock": {
                "movement": "recount",
                "items": [{"item": "Gizmos", "qty": "1", "rate": "83.50"}],
            }
        },
    }
    r = client.post("/api/v1/vouchers", json=payload, headers=default_headers(client))
    assert r.status_code == 422
    assert "foreign-currency" in r.json()["detail"]


def test_future_stock_date_blocked_unless_overridden(client, fresh_db):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = goods_in(client, "Nails", "1", "100.00", date_=tomorrow)
    assert r.status_code == 422
    assert "future" in r.json()["detail"]

    r = goods_in(
        client,
        "Nails",
        "1",
        "100.00",
        date_=tomorrow,
        meta_extra={"policy:allow_future_document_dates": True},
    )
    assert r.status_code == 201, r.text


# ------------------------------------------------------- master collision
def test_item_ledger_collision_guard(client, fresh_db):
    """An item may not silently reuse an existing unrelated ledger: the
    seeded Cash ledger is not a stock account."""
    r = goods_in(client, "Cash", "1", "100.00")
    assert r.status_code == 422
    assert "collides with the existing ledger" in r.json()["detail"]

    r = post_stock(
        client,
        "in",
        [{"item": "Cash", "qty": "1", "rate": "100.00", "inventory_ledger": "Cash Stock"}],
        party="Omega Supplies",
        type_="PUR",
    )
    assert r.status_code == 201, r.text
    assert ledger_groups(client)["Cash Stock"] == "Stock-in-Hand"


# ---------------------------------------------------- derived positions
def test_position_helper_agrees_with_postings(client, fresh_db, db_session):
    """The fold is the single source of truth: after a mixed sequence the
    position helper replays the documents and agrees with the last posted
    annotation — deterministic derivation, never stored state."""
    assert goods_in(client, "Spacers", "2", "100.00").status_code == 201
    assert goods_in(client, "Spacers", "2", "110.00").status_code == 201
    assert post_stock(client, "out", [{"item": "Spacers", "qty": "3"}]).status_code == 201
    r = post_stock(client, "recount", [{"item": "Spacers", "qty": "2"}], type_="JV")
    assert r.status_code == 201, r.text
    assert computed_of(r.json(), "Spacers")["qty_after"] == "2"

    pos = inventory.position(_resolver(db_session), "Spacers")
    assert pos.qty == Decimal(2)
    assert pos.value == Decimal("210.00")
    assert pos.avg_cost == Decimal("105.0000")

    # an unknown item has an empty position
    assert inventory.position(_resolver(db_session), "Unknown").qty == 0


# ------------------------------------------------------------ concurrency
def test_concurrent_same_item_outs_serialize(client, fresh_db, db_session, settings):
    """2 units on the shelf, 3 concurrent outs: exactly two post. The
    advisory lock (resolver.lock, API 0.3) serializes same-item movements,
    so the insufficient-stock guard is airtight under races."""
    from sqlalchemy import select

    from ledgerforge import db
    from ledgerforge.contrib import example_gst, invoicing
    from ledgerforge.engine.posting import post_voucher as engine_post
    from ledgerforge.models import Book
    from ledgerforge.plugins import PluginManager
    from ledgerforge.schemas import VoucherCreate, VoucherLineIn

    assert goods_in(client, "Bolts", "2", "10.00").status_code == 201

    manager = PluginManager()
    for module in (example_gst, invoicing, inventory):
        manager.load_module(module)

    book = db_session.scalar(select(Book).where(Book.slug == "default"))
    db_session.commit()

    session_factory = db.session_factory()
    results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(3)

    def worker() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            payload = VoucherCreate(
                voucher_type="SAL",
                voucher_date=date.today(),
                client_token=uuid4(),
                entries=[
                    VoucherLineIn(ledger_name="Bolts", dc="D", amount="1.00"),
                    VoucherLineIn(ledger_name="Bolts", dc="C", amount="1.00"),
                ],
                meta={"stock": {"movement": "out", "items": [{"item": "Bolts", "qty": "1"}]}},
            )
            voucher, _created = engine_post(session, payload, manager, settings, book)
            results.append(voucher.voucher_no)
        except Exception as exc:  # noqa: BLE001 — recorded and asserted below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    assert len(results) == 2, (results, [str(e) for e in errors])
    assert len(errors) == 1
    assert "no stock" in str(errors[0])

    pos = inventory.position(_resolver(db_session), "Bolts")
    assert pos.qty == 0 and pos.value == 0
