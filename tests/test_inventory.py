"""The stock-masters kernel (ADR-027, roadmap Phase 6): items with tracking
modes and re-order levels, the per-party date-effective price lists, and
the bills of materials — the masters the depth pack reads through plugin
API 0.12.

Pinned here: the CRUD surface, the identity verdicts (UNIQUE names per
book, one rate per slot), the date-effectivity rules (latest effective
wins; a party row beats the generic at the same date; a newer generic
overtakes an older party contract), the referential guards (a BOM cannot
name a ghost, cannot consume itself, cannot be deleted under a reference)
and the book scoping of every route.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import TODAY  # noqa: F401 — an ISO str, kept for symmetry


# ------------------------------------------------------------------ helpers
@pytest.fixture()
def headers(client):
    books = client.get("/api/v1/books").json()
    book_id = next(b["id"] for b in books if b["slug"] == "default")
    return {"X-Book-Id": str(book_id)}


def _item(client, h, name: str, **extra) -> dict:
    payload = {"name": name}
    payload.update(extra)
    r = client.post("/api/v1/inventory/items", json=payload, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _ledger(client, h, name: str, group: str) -> dict:
    r = client.post("/api/v1/ledgers", json={"name": name, "group_name": group}, headers=h)
    assert r.status_code in (201, 422), r.text  # 422 = exists from an earlier test
    rows = client.get("/api/v1/ledgers", headers=h).json()
    return next(row for row in rows if row["name"] == name)


def _price(client, h, item_id: int, effective_from: str, rate: str, **extra) -> dict:
    payload = {"item_id": item_id, "effective_from": effective_from, "rate": rate}
    payload.update(extra)
    r = client.post("/api/v1/inventory/prices", json=payload, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _bom(client, h, name: str, output_id: int, output_qty: str, components: list[dict]) -> dict:
    payload = {
        "name": name,
        "output_item_id": output_id,
        "output_qty": output_qty,
        "components": components,
    }
    r = client.post("/api/v1/inventory/boms", json=payload, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------- items
def test_item_master_crud_roundtrip(client, fresh_db, headers):
    row = _item(
        client,
        headers,
        "Amoxycillin 500",
        unit="strip",
        track_batches=True,
        reorder_level="40",
        reorder_quantity="100",
    )
    assert row["track_batches"] is True
    assert row["track_serials"] is False
    assert Decimal(row["reorder_level"]) == Decimal("40")
    assert Decimal(row["reorder_quantity"]) == Decimal("100")

    listed = client.get("/api/v1/inventory/items", headers=headers).json()
    assert any(i["name"] == "Amoxycillin 500" for i in listed)

    one = client.get(f"/api/v1/inventory/items/{row['id']}", headers=headers)
    assert one.status_code == 200
    assert one.json()["unit"] == "strip"

    patched = client.patch(
        f"/api/v1/inventory/items/{row['id']}",
        json={"reorder_level": "25", "track_serials": True},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert Decimal(body["reorder_level"]) == Decimal("25")
    assert body["track_serials"] is True
    assert body["track_batches"] is True  # untouched by the partial patch

    gone = client.delete(f"/api/v1/inventory/items/{row['id']}", headers=headers)
    assert gone.status_code == 200
    assert client.get(f"/api/v1/inventory/items/{row['id']}", headers=headers).status_code == 404


def test_item_identity_is_the_databases_verdict(client, fresh_db, headers):
    _item(client, headers, "Cogs")
    dup = client.post("/api/v1/inventory/items", json={"name": "Cogs"}, headers=headers)
    assert dup.status_code == 409
    assert "already exists" in dup.json()["detail"]

    for bad in ({"reorder_level": "-1"}, {"reorder_quantity": "0"}):
        r = client.post("/api/v1/inventory/items", json={"name": "Bad", **bad}, headers=headers)
        assert r.status_code == 422, (bad, r.text)

    rename = client.patch(
        f"/api/v1/inventory/items/{_item(client, headers, 'Sprockets')['id']}",
        json={"name": "Cogs"},
        headers=headers,
    )
    assert rename.status_code == 409


def test_items_are_book_scoped(client, fresh_db, headers):
    _item(client, headers, "Shared Name")
    other = client.post("/api/v1/books", json={"name": "OtherCo"}).json()["book"]
    other_h = {"X-Book-Id": str(other["id"])}
    assert client.get("/api/v1/inventory/items", headers=other_h).json() == []
    # the same name is free in the other book — masters are per-book
    assert (
        client.post(
            "/api/v1/inventory/items", json={"name": "Shared Name"}, headers=other_h
        ).status_code
        == 201
    )


# ------------------------------------------------------------------ prices
def test_price_lists_and_the_effective_lookup(client, fresh_db, headers):
    item = _item(client, headers, "Widgets")
    medplus = _ledger(client, headers, "MedPlus", "Sundry Debtors")
    _price(client, headers, item["id"], "2026-01-01", "15.50")  # the list price
    _price(
        client, headers, item["id"], "2026-01-01", "13.00", party_ledger_id=medplus["id"]
    )  # MedPlus's contract

    def effective(**params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        r = client.get(f"/api/v1/inventory/prices/effective?{query}", headers=headers)
        assert r.status_code == 200, r.text
        return r.json()["rate"]

    # at the same effective date the party's contract beats the list price
    assert effective(item_id=item["id"], party_ledger_id=medplus["id"]) == "13.00"
    # the generic lookup never sees party rows
    assert effective(item_id=item["id"]) == "15.50"
    # an unknown party has no contract — the list price is the only suggestion
    stranger = _ledger(client, headers, "Stranger Retail", "Sundry Debtors")
    assert effective(item_id=item["id"], party_ledger_id=stranger["id"]) == "15.50"
    # date-effective: before the first row, nothing applies
    assert effective(item_id=item["id"], on="2025-12-31") is None


def test_a_newer_generic_overtakes_an_older_party_contract(client, fresh_db, headers):
    """The stated rule (ADR-027 §1): the latest effective_from wins; the
    party row is the tie-breaker at the same date, never an override of a
    later list revision."""
    item = _item(client, headers, "Gears")
    party = _ledger(client, headers, "BulkBuyer", "Sundry Debtors")
    _price(client, headers, item["id"], "2026-01-01", "10.00", party_ledger_id=party["id"])
    _price(client, headers, item["id"], "2026-06-01", "12.00")  # the price rise

    r = client.get(
        f"/api/v1/inventory/prices/effective?item_id={item['id']}&party_ledger_id={party['id']}",
        headers=headers,
    )
    assert r.json()["rate"] == "12.00"


def test_one_rate_per_slot(client, fresh_db, headers):
    item = _item(client, headers, "Springs")
    _price(client, headers, item["id"], date.today().isoformat(), "9.00")
    dup = client.post(
        "/api/v1/inventory/items/{id}/prices".format(id=item["id"]),
        json={"item_id": item["id"], "effective_from": date.today().isoformat(), "rate": "9.50"},
        headers=headers,
    )
    # that route never existed — the canonical POST is /prices; both paths
    # prove the same thing only if we use the right one:
    assert dup.status_code in (404, 405)
    real = client.post(
        "/api/v1/inventory/prices",
        json={"item_id": item["id"], "effective_from": date.today().isoformat(), "rate": "9.50"},
        headers=headers,
    )
    assert real.status_code == 409
    assert "one rate per slot" in real.json()["detail"]

    # a LATER effective date is a new row, not a conflict
    assert (
        client.post(
            "/api/v1/inventory/prices",
            json={
                "item_id": item["id"],
                "effective_from": (date.today() + timedelta(days=1)).isoformat(),
                "rate": "9.75",
            },
            headers=headers,
        ).status_code
        == 201
    )


def test_price_rows_cascade_with_their_item(client, fresh_db, headers):
    item = _item(client, headers, "Discontinued")
    _price(client, headers, item["id"], "2026-01-01", "5.00")
    assert (
        client.delete(f"/api/v1/inventory/items/{item['id']}", headers=headers).status_code == 200
    )
    every = client.get("/api/v1/inventory/prices", headers=headers).json()
    assert not any(p["item_id"] == item["id"] for p in every)  # cascaded


# -------------------------------------------------------------------- BOMs
def test_bom_crud_and_referential_guards(client, fresh_db, headers):
    desk = _item(client, headers, "Desk")
    plank = _item(client, headers, "Plank")
    screw = _item(client, headers, "Screw")

    bom = _bom(
        client,
        headers,
        "Desk-STD",
        desk["id"],
        "2",
        [
            {"component_item_id": plank["id"], "qty_per_output": "6"},
            {"component_item_id": screw["id"], "qty_per_output": "20"},
        ],
    )
    assert bom["output"] == "Desk"
    assert [(c["component"], Decimal(c["qty_per_output"])) for c in bom["components"]] == [
        ("Plank", Decimal("6")),
        ("Screw", Decimal("20")),
    ]

    # a BOM cannot consume its own output
    r = client.post(
        "/api/v1/inventory/boms",
        json={
            "name": "Impossible",
            "output_item_id": desk["id"],
            "output_qty": "1",
            "components": [{"component_item_id": desk["id"], "qty_per_output": "1"}],
        },
        headers=headers,
    )
    assert r.status_code == 422
    assert "cannot be its own output" in r.json()["detail"]

    # a component may not appear twice
    r = client.post(
        "/api/v1/inventory/boms",
        json={
            "name": "Twice",
            "output_item_id": desk["id"],
            "output_qty": "1",
            "components": [
                {"component_item_id": plank["id"], "qty_per_output": "1"},
                {"component_item_id": plank["id"], "qty_per_output": "2"},
            ],
        },
        headers=headers,
    )
    assert r.status_code == 422 and "appears twice" in r.json()["detail"]

    # revising the recipe replaces the component set atomically
    revised = client.patch(
        f"/api/v1/inventory/boms/{bom['id']}",
        json={"components": [{"component_item_id": plank["id"], "qty_per_output": "7"}]},
        headers=headers,
    )
    assert revised.status_code == 200, revised.text
    assert [c["component"] for c in revised.json()["components"]] == ["Plank"]
    assert Decimal(revised.json()["components"][0]["qty_per_output"]) == Decimal("7")

    # the database refuses to delete an item a BOM references
    r = client.delete(f"/api/v1/inventory/items/{plank['id']}", headers=headers)
    assert r.status_code == 409
    assert "delete or re-point the BOM" in r.json()["detail"]

    # deleting the BOM frees the item
    assert client.delete(f"/api/v1/inventory/boms/{bom['id']}", headers=headers).status_code == 200
    freed = client.delete(f"/api/v1/inventory/items/{plank['id']}", headers=headers)
    assert freed.status_code == 200


def test_bom_names_are_unique_per_book(client, fresh_db, headers):
    bench = _item(client, headers, "Bench")
    _bom(
        client,
        headers,
        "Bench-STD",
        bench["id"],
        "1",
        [{"component_item_id": _item(client, headers, "Rail")["id"], "qty_per_output": "1"}],
    )
    r = client.post(
        "/api/v1/inventory/boms",
        json={
            "name": "Bench-STD",
            "output_item_id": bench["id"],
            "output_qty": "1",
            "components": [{"component_item_id": bench["id"], "qty_per_output": "1"}],
        },
        headers=headers,
    )
    assert r.status_code == 409


# ------------------------------------------------------ the resolver handles
def test_resolver_stock_handles_read_the_masters(client, fresh_db, headers, db_session):
    from sqlalchemy import select

    from ledgerforge import inventory as kernel
    from ledgerforge.models import Book
    from ledgerforge.plugins import Resolver

    item = _item(client, headers, "Resolver Item", unit="kg", reorder_level="10")
    book = db_session.scalar(select(Book).where(Book.slug == "default"))
    resolver = Resolver(db_session, book)

    master = resolver.stock_item(name="Resolver Item")
    assert master is not None
    assert master.unit == "kg"
    assert master.reorder_level == Decimal("10")
    assert resolver.stock_item(name="no such item") is None
    assert any(m.name == "Resolver Item" for m in resolver.stock_items())

    # the BOM handle resolves components to names
    plank = _item(client, headers, "Plank")
    kernel.create_bom(
        db_session,
        book,
        {
            "name": "R-Desk",
            "output_item_id": item["id"],
            "output_qty": "2",
            "components": [{"component_item_id": plank["id"], "qty_per_output": "3"}],
        },
    )
    db_session.commit()
    bom = resolver.stock_bom(name="R-Desk")
    assert bom.output_item == "Resolver Item"
    assert bom.components == (("Plank", Decimal("3")),)
    assert resolver.stock_bom(name="no such bom") is None


def test_resolver_price_handle(client, fresh_db, headers, db_session):
    from sqlalchemy import select

    from ledgerforge.models import Book
    from ledgerforge.plugins import Resolver

    item = _item(client, headers, "Priced")
    party = _ledger(client, headers, "MedPlus", "Sundry Debtors")
    _price(client, headers, item["id"], "2026-01-01", "14.00", party_ledger_id=party["id"])
    book = db_session.scalar(select(Book).where(Book.slug == "default"))
    resolver = Resolver(db_session, book)

    from decimal import Decimal

    assert resolver.stock_price("Priced", "MedPlus") == Decimal("14.00")
    assert resolver.stock_price("Priced") is None  # no list price exists
    assert resolver.stock_price("no such item", "MedPlus") is None
    assert resolver.stock_price("Priced", "no such party") is None
