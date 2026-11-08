"""The inventory pack's Phase 6 depth (ADR-027): batches, serials, expiry,
godowns, re-order levels and manufacturing — on the same document fold
ADR-011 built.

Pinned here: the depth grammar's guards (batch existence and expiry
consistency, serial identity, godown positions never lying), FEFO's
deterministic allocation, the manufacturing journal's closed cost rollup
(``output value = Σ component costs + Σ charges``, exact), the master
tracking modes' demands, the reports' folds (one truth, three renderings)
and reversal netting across every sub-ledger.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from conftest import TODAY  # noqa: F401 — ISO date str, where a string is wanted


# ------------------------------------------------------------------ helpers
def post_stock(
    client,
    movement: str,
    items,
    *,
    party=None,
    type_="SAL",
    headers=None,
    meta_extra=None,
    date_=None,
    **doc_extra,
):
    doc = {"movement": movement}
    if isinstance(items, list):
        doc["items"] = items
    else:
        doc.update(items)  # manufacture: components/output/charges
    if party is not None:
        doc["party"] = party
    doc.update({k: v for k, v in doc_extra.items() if v is not None})
    meta = {"stock": doc}
    if meta_extra:
        meta.update(meta_extra)
    entries = [
        {"ledger_name": "(stock document)", "dc": "D", "amount": "0.01"}
    ]  # placeholder the transformer replaces
    payload = {
        "voucher_type": type_,
        "voucher_date": date_ or date.today().isoformat(),
        "entries": entries,
        "meta": meta,
    }
    return client.post("/api/v1/vouchers", json=payload, headers=headers)


def goods_in(
    client,
    item,
    qty,
    rate,
    *,
    party="Omega Supplies",
    batch=None,
    expiry=None,
    godown=None,
    serials=None,
    headers=None,
    **extra,
):
    line = {"item": item, "qty": qty, "rate": rate}
    if batch:
        line["batch"] = batch
    if expiry:
        line["expiry"] = expiry
    if godown:
        line["godown"] = godown
    if serials:
        line["serials"] = serials
    return post_stock(client, "in", [line], party=party, type_="PUR", headers=headers, **extra)


def goods_out(
    client,
    item,
    qty,
    *,
    party=None,
    rate=None,
    batch=None,
    godown=None,
    serials=None,
    headers=None,
    policy_negative=False,
    **extra,
):
    line = {"item": item, "qty": qty}
    if rate is not None:
        line["rate"] = rate
    if batch:
        line["batch"] = batch
    if godown:
        line["godown"] = godown
    if serials:
        line["serials"] = serials
    meta_extra = {"policy:allow_negative_stock": True} if policy_negative else None
    return post_stock(
        client, "out", [line], party=party, headers=headers, meta_extra=meta_extra, **extra
    )


def computed_of(body: dict, key: str, item: str) -> dict:
    return next(it for it in body["meta"]["stock"]["computed"][key] if it["item"] == item)


def position_of(client, item: str, headers=None):
    from urllib.parse import quote

    r = client.get(
        f"/api/v1/reports/pack/stock-position?format=json&item={quote(item)}",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    rows = r.json()["items"]
    return rows[0] if rows else None


def make_item_master(
    client,
    name: str,
    *,
    track_batches=False,
    track_serials=False,
    reorder_level=None,
    reorder_quantity=None,
    headers=None,
):
    payload = {"name": name, "track_batches": track_batches, "track_serials": track_serials}
    if reorder_level is not None:
        payload["reorder_level"] = reorder_level
    if reorder_quantity is not None:
        payload["reorder_quantity"] = reorder_quantity
    r = client.post("/api/v1/inventory/items", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ batches
def test_batches_track_in_out_and_expiry(client, fresh_db):
    """A batch is born on its first receipt (expiry fixed), issues name it
    or take FEFO's word, and the position shows the split."""
    r = goods_in(client, "Amox", "10", "12.00", batch="AMX-114", expiry="2027-03-31")
    assert r.status_code == 201, r.text

    r = goods_in(client, "Amox", "10", "12.00", batch="AMX-207", expiry="2026-12-15")
    assert r.status_code == 201, r.text

    pos = position_of(client, "Amox")
    assert pos["qty"] == "20"
    batches = {b["batch"]: b for b in pos["batches"]}
    assert batches["AMX-114"]["expiry"] == "2027-03-31"
    assert batches["AMX-207"]["expiry"] == "2026-12-15"
    assert batches["AMX-114"]["qty"] == "10"

    # explicit batch issue
    r = goods_out(client, "Amox", "4", batch="AMX-114")
    assert r.status_code == 201, r.text
    assert Decimal(computed_of(r.json(), "items", "Amox")["cost"]) == Decimal("48.00")

    # FEFO: no batch named — the earliest expiry (AMX-207) goes first
    r = goods_out(client, "Amox", "12")
    assert r.status_code == 201, r.text
    allocation = computed_of(r.json(), "items", "Amox")["batch_allocation"]
    assert [(a["batch"], a["qty"]) for a in allocation] == [("AMX-207", "10"), ("AMX-114", "2")]

    pos = position_of(client, "Amox")
    assert pos["qty"] == "4"
    assert {b["batch"]: b["qty"] for b in pos["batches"]} == {"AMX-114": "4"}

    # a batch that does not exist is refused
    r = goods_out(client, "Amox", "1", batch="NOPE-1")
    assert r.status_code == 422
    assert "unknown batch" in r.json()["detail"]


def test_batch_expiry_is_fixed_by_the_first_receipt(client, fresh_db):
    goods_in(client, "Serum", "5", "100.00", batch="SR-1", expiry="2026-06-30")
    # a later receipt of the same batch must agree…
    r = goods_in(client, "Serum", "5", "100.00", batch="SR-1", expiry="2026-06-30")
    assert r.status_code == 201, r.text
    # …and cannot redefine it
    r = goods_in(client, "Serum", "5", "100.00", batch="SR-1", expiry="2027-06-30")
    assert r.status_code == 422
    assert "cannot redefine" in r.json()["detail"]
    # expiry rides a batch — a bare expiry is a 422
    r = goods_in(client, "Serum", "5", "100.00", expiry="2027-01-01")
    assert r.status_code == 422
    assert "expiry rides a batch" in r.json()["detail"]


def test_fifo_ties_break_by_batch_name(client, fresh_db):
    """Two batches expiring the same day: FEFO takes the alphabetically
    first — deterministic, the fold replays the same split."""
    goods_in(client, "Nuts", "5", "2.00", batch="B-lot", expiry="2027-01-01")
    goods_in(client, "Nuts", "5", "2.00", batch="A-lot", expiry="2027-01-01")
    r = goods_out(client, "Nuts", "7")
    allocation = computed_of(r.json(), "items", "Nuts")["batch_allocation"]
    assert [(a["batch"], a["qty"]) for a in allocation] == [("A-lot", "5"), ("B-lot", "2")]


# ----------------------------------------------------------------- serials
def test_serials_are_identity(client, fresh_db):
    goods_in(client, "Pumps", "2", "500.00", serials=["P-001", "P-002"])
    pos = position_of(client, "Pumps")
    assert pos["serial_count"] == 2

    # a duplicate serial is a data error, not an override policy
    r = goods_in(client, "Pumps", "1", "500.00", serials=["P-001"])
    assert r.status_code == 422
    assert "already in stock" in r.json()["detail"]

    # one serial per unit — three serials cannot move a qty of 2
    r = goods_out(client, "Pumps", "2", serials=["P-001", "P-002", "P-003"])
    assert r.status_code == 422
    assert "one serial per unit" in r.json()["detail"]

    # issue by serial
    r = goods_out(client, "Pumps", "1", serials=["P-001"])
    assert r.status_code == 201, r.text
    assert position_of(client, "Pumps")["serial_count"] == 1

    # a serial not in stock is refused
    r = goods_out(client, "Pumps", "1", serials=["P-999"])
    assert r.status_code == 422
    assert "not in stock" in r.json()["detail"]

    # the last one goes, and the position empties
    assert goods_out(client, "Pumps", "1", serials=["P-002"]).status_code == 201
    pos = position_of(client, "Pumps")
    assert pos["qty"] == "0" and pos["serial_count"] == 0


def test_serials_live_at_their_godown_and_batch(client, fresh_db):
    goods_in(client, "Valves", "1", "80.00", batch="V-1", godown="Cold Room", serials=["VX-1"])
    # the serial sits in Cold Room — issuing it from the Main location lies
    # (the location guard fires first: the godown holds nothing)
    r = goods_out(client, "Valves", "1", serials=["VX-1"])
    assert r.status_code == 422
    assert "Main location" in r.json()["detail"]
    assert "stock lives in locations" in r.json()["detail"]
    # from its own godown it goes
    r = goods_out(client, "Valves", "1", godown="Cold Room", serials=["VX-1"])
    assert r.status_code == 201, r.text


# ----------------------------------------------------------------- godowns
def test_godowns_are_positions_not_ledgers(client, fresh_db):
    """The ADR-011 limitation closed: quantity lives at locations, and an
    issue cannot take what the godown does not hold — whatever the ledgers
    say."""
    goods_in(client, "Cable", "10", "20.00", godown="Warehouse A")
    goods_in(client, "Cable", "10", "20.00", godown="Warehouse B")

    pos = position_of(client, "Cable")
    assert pos["godowns"] == {"Warehouse A": "10", "Warehouse B": "10"}

    # Main location holds nothing — the issue is refused even though the
    # ITEM has 20 units
    r = goods_out(client, "Cable", "1")
    assert r.status_code == 422
    assert "godown" in r.json()["detail"]
    assert "Main location" in r.json()["detail"]

    assert goods_out(client, "Cable", "3", godown="Warehouse A").status_code == 201
    pos = position_of(client, "Cable")
    assert pos["godowns"] == {"Warehouse A": "7", "Warehouse B": "10"}


def test_transfer_moves_quantity_between_godowns(client, fresh_db):
    goods_in(client, "Reels", "6", "50.00", godown="Warehouse A")
    r = post_stock(
        client,
        "transfer",
        [
            {
                "item": "Reels",
                "qty": "4",
                "to_ledger": "Reels @ B",
                "godown": "Warehouse A",
                "to_godown": "Warehouse B",
            },
        ],
        type_="JV",
    )
    assert r.status_code == 201, r.text
    pos = position_of(client, "Reels")
    assert pos["godowns"] == {"Warehouse A": "2", "Warehouse B": "4"}
    # the value pair moved between the ledgers, the item position is whole
    assert pos["qty"] == "6"
    assert pos["value"] == "300.00"

    # a location cannot lend what it does not hold
    r = post_stock(
        client,
        "transfer",
        [
            {
                "item": "Reels",
                "qty": "3",
                "to_ledger": "Reels @ C",
                "godown": "Warehouse A",
                "to_godown": "Warehouse C",
            },
        ],
        type_="JV",
    )
    assert r.status_code == 422
    assert "never go negative" in r.json()["detail"]


def test_recount_targets_a_godown_and_a_batch(client, fresh_db):
    goods_in(client, "Bolts", "10", "2.00", batch="BLT-1", godown="Store")
    goods_in(client, "Bolts", "5", "2.00", batch="BLT-2", godown="Store")

    # a plain count of a batch-carrying godown must name its batch
    r = post_stock(client, "recount", [{"item": "Bolts", "qty": "12", "godown": "Store"}])
    assert r.status_code == 422
    assert "names its batch" in r.json()["detail"]

    # count BLT-1 down to 4 at the Store
    r = post_stock(
        client,
        "recount",
        [
            {"item": "Bolts", "qty": "4", "batch": "BLT-1", "godown": "Store"},
        ],
    )
    assert r.status_code == 201, r.text
    pos = position_of(client, "Bolts")
    assert pos["qty"] == "9"  # 4 + 5
    split = {b["batch"]: b["qty"] for b in pos["batches"]}
    assert split == {"BLT-1": "4", "BLT-2": "5"}
    # the shrinkage posted at the average cost
    assert Decimal(computed_of(r.json(), "items", "Bolts")["value_delta"]) == Decimal("-12.00")


# ------------------------------------------------- master tracking demands
def test_tracking_modes_are_demands(client, fresh_db):
    make_item_master(client, "Tracked Tablets", track_batches=True)
    r = goods_in(client, "Tracked Tablets", "10", "5.00")
    assert r.status_code == 422
    assert "maintains batches" in r.json()["detail"]
    assert goods_in(client, "Tracked Tablets", "10", "5.00", batch="TT-1").status_code == 201
    # issues may omit the batch — FEFO decides (recorded, deterministic)
    assert goods_out(client, "Tracked Tablets", "3").status_code == 201

    make_item_master(client, "Tracked Pumps", track_serials=True)
    assert (
        "maintains serial numbers"
        in goods_in(client, "Tracked Pumps", "1", "9.00").json()["detail"]
    )
    assert goods_in(client, "Tracked Pumps", "1", "9.00", serials=["TP-1"]).status_code == 201
    r = goods_out(client, "Tracked Pumps", "1")
    assert r.status_code == 422
    assert "maintains serial numbers" in r.json()["detail"]
    assert goods_out(client, "Tracked Pumps", "1", serials=["TP-1"]).status_code == 201


def test_price_list_suggestion_is_annotated_never_applied(client, fresh_db):
    master = make_item_master(client, "Priced Widgets")
    ledger = client.post(
        "/api/v1/ledgers", json={"name": "MedPlus", "group_name": "Sundry Debtors"}
    ).json()
    price = client.post(
        "/api/v1/inventory/prices",
        json={
            "item_id": master["id"],
            "party_ledger_id": ledger["id"],
            "effective_from": date.today().isoformat(),
            "rate": "21.00",
        },
    )
    assert price.status_code == 201, price.text

    goods_in(client, "Priced Widgets", "10", "10.00")
    # the sale carries the operator's rate; the list price rides along as a note
    r = goods_out(client, "Priced Widgets", "2", party="MedPlus", rate="25.00")
    assert r.status_code == 201, r.text
    row = computed_of(r.json(), "items", "Priced Widgets")
    assert row["price_list_rate"] == "21.00"
    assert Decimal(computed_of(r.json(), "items", "Priced Widgets")["cost"]) == Decimal("20.00")


# ------------------------------------------------------------ manufacture
def _manufacture_payload(output, components, charges=(), bom=None):
    doc = {
        "movement": "manufacture",
        "components": components,
        "output": output,
    }
    if charges:
        doc["charges"] = charges
    if bom:
        doc["bom"] = bom
    return doc


def test_manufacturing_journal_rolls_costs_up_exactly(client, fresh_db):
    """The pack's most ambitious fold: components at moving averages plus
    the explicit charges, landing on the output as ONE exact amount."""
    goods_in(client, "Planks", "10", "100.00")  # avg 100
    goods_in(client, "Screws", "100", "1.00")  # avg 1

    client.post(
        "/api/v1/ledgers", json={"name": "Production Wages", "group_name": "Direct Expenses"}
    )
    doc = _manufacture_payload(
        {"item": "Desks", "qty": "2", "batch": "DSK-1"},
        [
            {"item": "Planks", "qty": "6"},
            {"item": "Screws", "qty": "40"},
        ],
        charges=[{"ledger": "Production Wages", "amount": "450.00"}],
    )
    r = post_stock(client, "manufacture", doc, type_="JV")
    assert r.status_code == 201, r.text
    body = r.json()

    # 6 planks @ 100 + 40 screws @ 1 + 450 wages = 1090.00, one output line
    by_ledger = {}
    for e in body["entries"]:
        by_ledger.setdefault(e["ledger_name"], []).append(e)
    assert Decimal(by_ledger["Desks"][0]["amount"]) == Decimal("1090.00")
    assert by_ledger["Desks"][0]["dc"] == "D"
    assert Decimal(by_ledger["Planks"][0]["amount"]) == Decimal("600.00")
    assert Decimal(by_ledger["Screws"][0]["amount"]) == Decimal("40.00")
    assert Decimal(by_ledger["Production Wages"][0]["amount"]) == Decimal("450.00")
    # balanced by construction — Law 1 proved it at posting

    computed = body["meta"]["stock"]["computed"]
    assert computed["material_total"] == "640.00"
    assert computed["charge_total"] == "450.00"
    assert computed["output_value"] == "1090.00"
    assert computed["output_unit_cost"] == "545.0000"

    # positions: components down, output up at the rolled cost
    planks = position_of(client, "Planks")
    screws = position_of(client, "Screws")
    desks = position_of(client, "Desks")
    assert planks["qty"] == "4" and planks["value"] == "400.00"
    assert screws["qty"] == "60" and screws["value"] == "60.00"
    assert desks["qty"] == "2" and desks["value"] == "1090.00"
    assert desks["batches"][0]["batch"] == "DSK-1"

    # reversal nets the whole journal: components restored, output gone
    rev = client.post(f"/api/v1/vouchers/{body['id']}/reverse")
    assert rev.status_code == 201, rev.text
    assert position_of(client, "Desks") is None
    assert position_of(client, "Planks")["qty"] == "10"
    assert position_of(client, "Screws")["qty"] == "100"


def test_manufacture_against_a_bom(client, fresh_db):
    desk = make_item_master(client, "Desk Units")
    plank = make_item_master(client, "Stud Planks")
    screw = make_item_master(client, "Stud Screws")
    bom = client.post(
        "/api/v1/inventory/boms",
        json={
            "name": "Desk-STD",
            "output_item_id": desk["id"],
            "output_qty": "2",
            "components": [
                {"component_item_id": plank["id"], "qty_per_output": "6"},
                {"component_item_id": screw["id"], "qty_per_output": "40"},
            ],
        },
    )
    assert bom.status_code == 201, bom.text

    goods_in(client, "Stud Planks", "10", "100.00")
    goods_in(client, "Stud Screws", "100", "1.00")

    # under-consumption against the recipe is refused
    under = _manufacture_payload(
        {"item": "Desk Units", "qty": "2"},
        [{"item": "Stud Planks", "qty": "6"}, {"item": "Stud Screws", "qty": "10"}],
        bom="Desk-STD",
    )
    r = post_stock(client, "manufacture", under, type_="JV")
    assert r.status_code == 422
    assert "Stud Screws" in r.json()["detail"] and "needs" in r.json()["detail"]

    # a component outside the recipe is refused
    extra = _manufacture_payload(
        {"item": "Desk Units", "qty": "2"},
        [
            {"item": "Stud Planks", "qty": "6"},
            {"item": "Stud Screws", "qty": "40"},
            {"item": "Mystery Glue", "qty": "1"},
        ],
        bom="Desk-STD",
    )
    r = post_stock(client, "manufacture", extra, type_="JV")
    assert r.status_code == 422
    assert "not in BOM" in r.json()["detail"]

    # the wrong output is refused
    wrong = _manufacture_payload(
        {"item": "Desk Units", "qty": "2"},
        [{"item": "Stud Planks", "qty": "6"}, {"item": "Stud Screws", "qty": "40"}],
        bom="Nope-STD",
    )
    r = post_stock(client, "manufacture", wrong, type_="JV")
    assert r.status_code == 422
    assert "no BOM named" in r.json()["detail"]

    # excess consumption is wastage — allowed
    ok = _manufacture_payload(
        {"item": "Desk Units", "qty": "2"},
        [{"item": "Stud Planks", "qty": "7"}, {"item": "Stud Screws", "qty": "45"}],
        bom="Desk-STD",
    )
    r = post_stock(client, "manufacture", ok, type_="JV")
    assert r.status_code == 201, r.text
    assert r.json()["meta"]["stock"]["computed"]["bom"] == "Desk-STD"


def test_manufacture_guards(client, fresh_db):
    # nothing to consume
    r = post_stock(
        client,
        "manufacture",
        _manufacture_payload(
            {"item": "Ghost Desks", "qty": "1"}, [{"item": "Ghost Planks", "qty": "1"}]
        ),
        type_="JV",
    )
    assert r.status_code == 422
    assert "no stock of 'Ghost Planks'" in r.json()["detail"]

    goods_in(client, "Boards", "4", "50.00")
    # a charge ledger under Stock-in-Hand values stock with stock — refused
    client.post("/api/v1/ledgers", json={"name": "Board Stock", "group_name": "Stock-in-Hand"})
    r = post_stock(
        client,
        "manufacture",
        _manufacture_payload(
            {"item": "Crates", "qty": "1"},
            [{"item": "Boards", "qty": "2"}],
            charges=[{"ledger": "Board Stock", "amount": "10.00"}],
        ),
        type_="JV",
    )
    assert r.status_code == 422
    assert "outside the stock" in r.json()["detail"]

    # serialized outputs are born with their serials
    make_item_master(client, "Serial Desks", track_serials=True)
    r = post_stock(
        client,
        "manufacture",
        _manufacture_payload(
            {"item": "Serial Desks", "qty": "1"}, [{"item": "Boards", "qty": "2"}]
        ),
        type_="JV",
    )
    assert r.status_code == 422
    assert "must assign them" in r.json()["detail"]
    r = post_stock(
        client,
        "manufacture",
        _manufacture_payload(
            {"item": "Serial Desks", "qty": "1", "serials": ["SD-0001"]},
            [{"item": "Boards", "qty": "2"}],
        ),
        type_="JV",
    )
    assert r.status_code == 201, r.text
    assert position_of(client, "Serial Desks")["serial_count"] == 1


# ---------------------------------------------------------------- reports
def test_the_five_reports_render_one_fold_three_ways(client, fresh_db):
    make_item_master(client, "Report Widgets", reorder_level="10", reorder_quantity="50")
    goods_in(
        client,
        "Report Widgets",
        "6",
        "10.00",
        batch="RW-A",
        expiry=(date.today() + timedelta(days=20)).isoformat(),
    )
    goods_in(
        client,
        "Report Widgets",
        "6",
        "10.00",
        batch="RW-B",
        expiry=(date.today() + timedelta(days=200)).isoformat(),
        godown="Annex",
    )

    def fetch(key, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return client.get(f"/api/v1/reports/pack/{key}?{query}")

    # position (JSON): the depth the UI browses
    r = fetch("stock-position", format="json")
    assert r.status_code == 200
    row = next(i for i in r.json()["items"] if i["item"] == "Report Widgets")
    assert row["qty"] == "12"
    assert row["godowns"]["Annex"] == "6"
    assert {b["batch"] for b in row["batches"]} == {"RW-A", "RW-B"}

    # godown-wise: the matrix with totals
    r = fetch("stock-godown", format="json")
    assert r.status_code == 200
    row = next(
        x for x in r.json()["rows"] if x["item"] == "Report Widgets" and x["godown"] == "Annex"
    )
    assert row["qty"] == "6"
    assert set(r.json()["godown_totals"]) >= {"Main location", "Annex"}

    # batch-wise
    r = fetch("stock-batch", format="csv")
    assert r.status_code == 200
    assert "RW-A" in r.text and "RW-B" in r.text

    # expiry ageing: RW-A is inside the 30-day bucket
    r = fetch("stock-expiry", format="json")
    assert r.status_code == 200
    row = next(x for x in r.json()["rows"] if x["batch"] == "RW-A")
    assert row["bucket"] == "<=30d"
    assert r.json()["bucket_totals"]["<=30d"] == "60.00"

    # shortfall: 12 on hand, level 10 — NOT short
    r = fetch("stock-shortfall", format="json")
    assert r.status_code == 200
    assert not any(x["item"] == "Report Widgets" for x in r.json()["rows"])
    # issue down to 6 → short by 4, order 50
    goods_out(client, "Report Widgets", "6")
    r = fetch("stock-shortfall", format="json")
    row = next(x for x in r.json()["rows"] if x["item"] == "Report Widgets")
    assert row["on_hand"] == "6"
    assert row["below_by"] == "4"
    assert row["suggested_order"] == "50"

    # every report also prints (PDF) — the shared layout engine
    for key in ("stock-position", "stock-godown", "stock-batch", "stock-expiry", "stock-shortfall"):
        pdf = fetch(key)
        assert pdf.status_code == 200, key
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content[:4] == b"%PDF"


def test_as_of_windows_the_fold(client, fresh_db):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    r = post_stock(
        client,
        "in",
        [{"item": "Timely", "qty": "5", "rate": "10.00"}],
        party="Omega Supplies",
        type_="PUR",
        date_=yesterday,
    )
    assert r.status_code == 201, r.text
    assert goods_in(client, "Timely", "5", "10.00").status_code == 201  # today

    r = client.get(f"/api/v1/reports/pack/stock-position?format=json&as_of={yesterday}")
    row = next(i for i in r.json()["items"] if i["item"] == "Timely")
    assert row["qty"] == "5"


# ----------------------------------------------------------- document view
def test_stock_documents_print(client, fresh_db):
    goods_in(client, "Printed", "3", "10.00", batch="PR-1", expiry="2027-01-01", godown="Shop")
    body = goods_in(
        client, "Printed", "3", "10.00", batch="PR-1", expiry="2027-01-01", godown="Shop"
    ).json()
    r = client.get(f"/api/v1/vouchers/{body['id']}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"

    # every movement kind prints as the physical document it is
    out = goods_out(client, "Printed", "1", batch="PR-1", godown="Shop").json()
    r = client.get(f"/api/v1/vouchers/{out['id']}/pdf")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"

    doc = _manufacture_payload(
        {"item": "Printed Goods", "qty": "1"},
        [{"item": "Printed", "qty": "1", "batch": "PR-1", "godown": "Shop"}],
        charges=[{"ledger": "Cost of Goods Sold", "amount": "5.00"}],
    )
    mfg = post_stock(client, "manufacture", doc, type_="JV").json()
    r = client.get(f"/api/v1/vouchers/{mfg['id']}/pdf")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"


# ------------------------------------------------------------ book isolation
def test_depth_is_book_scoped(client, fresh_db):
    books = client.get("/api/v1/books").json()
    default_id = next(b["id"] for b in books if b["slug"] == "default")
    default = {"X-Book-Id": str(default_id)}
    other = client.post("/api/v1/books", json={"name": "DepthCo"}).json()["book"]
    other_h = {"X-Book-Id": str(other["id"])}

    goods_in(client, "Shared", "5", "10.00", batch="SH-1", serials=None, headers=default)
    goods_in(client, "Shared", "7", "20.00", batch="SH-1", headers=other_h)

    mine = position_of(client, "Shared", headers=default)
    theirs = position_of(client, "Shared", headers=other_h)
    assert mine["qty"] == "5" and mine["value"] == "50.00"
    assert theirs["qty"] == "7" and theirs["value"] == "140.00"
    # the same batch name, two books, two independent quantities
    assert mine["batches"][0]["qty"] == "5"
    assert theirs["batches"][0]["qty"] == "7"
