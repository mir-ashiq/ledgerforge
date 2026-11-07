"""The GST compliance pack (contrib/gst.py, ADR-017, issue #6): place of
supply, HSN rate tables with effective dates, GSTR-ready statements and the
e-invoicing outbox + its out-of-process bridge.

The promises under test:
- Place of supply: same state ⇒ CGST + SGST (the SGST line the exact
  remainder), different state ⇒ IGST — in document mode and in the legacy
  quick-tax shape alike.
- HSN tables: an item's slab comes from the effective-dated table at the
  voucher date (12% mobile phones before 2020-04-01, 18% after); explicit
  tax_rate is slab-whitelisted and must agree with the table; unknown
  chapters are refused loudly, never guessed.
- The accounting shape is TallyPrime's: party line at the grand total, one
  line per item, tax lines under Duties & Taxes (Input ledgers inward);
  the computed block persists with the voucher (ADR-010).
- GSTR-1 (B2B register) and the HSN summary replay posted facts — same
  figures the ledger posted, by construction; reversals net; purchases
  never leak into the outward register; other books never leak at all.
- The IRN outbox: B2B sales queue after commit (LF_GST_OUTBOX), the
  posting path never touches the network, and an unwritable outbox is
  isolated (posting still succeeds).
- The bridge: 2xx ships and records the IRN, 4xx parks with the error,
  5xx/network stays pending — eventual consistency, on purpose.
- Guards: missing/garbage period 422, unknown side/format/round 422, an
  empty month a valid nil statement.

Fixture discipline (the statutory suite's): one module-scoped fresh schema;
the demo tax month (PERIOD) is seeded once and asserted on exactly — every
later test posts into its own month (PERIOD_ALT / PERIOD_QUICK), so PERIOD
stays reproducible regardless of test order.
"""

from __future__ import annotations

import csv
import io
import json
import threading
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ledgerforge.contrib.gst import (
    GST_HSN_CSV_HEADER,
    GSTR1_CSV_HEADER,
    rate_for,
)
from ledgerforge.contrib.irn_bridge import main as bridge_main

# a Karnataka supplier (29) selling intra-state (29) and to Tamil Nadu (33)
SUPPLIER = "29"
KA_GSTIN = "29ABCDE1234F1Z5"  # a Karnataka-registered buyer
TN_GSTIN = "33ABCDE1234F1Z5"  # a Tamil Nadu-registered buyer


def _month(offset: int = 0) -> str:
    y, m = date.today().year, date.today().month + offset
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m += 12
        y += 1
    return f"{y:04d}-{m:02d}"


PERIOD = _month(0)  # the module's shared demo month — exact assertions only
PERIOD_ALT = _month(-1)  # the place-of-supply / HSN-table extras' month
PERIOD_QUICK = _month(-2)  # a quick-tax-only month (the footnote test)
PERIOD_EMPTY = _month(-3)  # a month with no GST documents (nil statement)

BALANCED = [
    {"ledger_name": "Cash", "dc": "D", "amount": "100.00"},
    {"ledger_name": "Cash", "dc": "C", "amount": "100.00"},
]


def default_headers(client) -> dict | None:
    """Once a second book exists, requests must name one (the books-are-
    strangers test creates Helios mid-module)."""
    books = client.get("/api/v1/books").json()
    if len(books) == 1:
        return None
    default_id = next(b["id"] for b in books if b["slug"] == "default")
    return {"X-Book-Id": str(default_id)}


def post_gst(
    client,
    doc: dict,
    voucher_date: str | None = None,
    headers=None,
    entries: list[dict] | None = None,
    **extra,
):
    """Post a voucher carrying ``meta.gst``. Document mode replaces the
    lines, so a minimal placeholder suffices; quick-tax keeps them, so the
    default is a balanced Cash pair."""
    meta = {"gst": doc}
    meta.update(extra)
    return client.post(
        "/api/v1/vouchers",
        json={
            "voucher_type": "SAL",
            "voucher_date": voucher_date or date.today().isoformat(),
            "entries": entries if entries is not None else BALANCED,
            "meta": meta,
        },
        headers=headers or default_headers(client),
    )


def b2b_sale(party: str, gstin: str, pos: str, items: list[dict], **extra) -> dict:
    doc = {
        "mode": "sale",
        "party": party,
        "party_gstin": gstin,
        "supplier_state": SUPPLIER,
        "place_of_supply": pos,
        "items": items,
    }
    doc.update(extra)
    return doc


def gstr1_rows(client, query: str) -> list[list[str]]:
    r = client.get(
        f"/api/v1/reports/pack/gstr1?period={query}&format=csv", headers=default_headers(client)
    )
    assert r.status_code == 200, r.text
    return list(csv.reader(io.StringIO(r.text)))


def hsn_rows(client, query: str) -> list[list[str]]:
    r = client.get(
        f"/api/v1/reports/pack/gst-hsn?period={query}&format=csv", headers=default_headers(client)
    )
    assert r.status_code == 200, r.text
    return list(csv.reader(io.StringIO(r.text)))


def _lines_of(response) -> dict[tuple[str, str], Decimal]:
    return {
        (e["ledger_name"], e["dc"]): Decimal(e["amount"]) for e in response.json()["entries"]
    }


@pytest.fixture(scope="module")
def demo(client_module, fresh_db):
    """The module's shared demo month (PERIOD), seeded once: an intra-state
    B2B sale (mixed slabs incl. an exempt chapter), an inter-state B2B sale
    that gets reversed (netting proof), a B2C sale (no GSTIN — outside the
    register), an inward purchase and a quick-tax voucher."""
    client = client_module

    # intra-state: Karnataka → Karnataka; explicit 18 + HSN-resolved 5 and 0
    r = post_gst(
        client,
        b2b_sale(
            "Chennai Traders",
            KA_GSTIN,
            "29",
            [
                {"description": "Steel pins", "hsn": "7317", "qty": 100, "rate": "10.00",
                 "tax_rate": "18"},
                {"description": "Palm oil tins", "hsn": "1511", "qty": 2, "rate": "250.00"},
                {"description": "Textbook lot", "hsn": "4901", "qty": 3, "rate": "199.00"},
            ],
        ),
    )
    assert r.status_code == 201, r.text

    # inter-state: Karnataka → Tamil Nadu; HSN-resolved 18% (mobiles, today)
    r = post_gst(
        client,
        b2b_sale(
            "Madras Machines",
            TN_GSTIN,
            "33",
            [{"description": "Feature phones", "hsn": "8517", "qty": 4, "rate": "1250.00"}],
        ),
    )
    assert r.status_code == 201, r.text
    inter_id = r.json()["id"]

    # B2C: no GSTIN — outside the B2B register, inside the HSN summary
    r = post_gst(
        client,
        {
            "mode": "sale",
            "party": "Walk-in Customer",
            "supplier_state": SUPPLIER,
            "items": [{"description": "Nails box", "hsn": "7317", "qty": 1, "rate": "90.00"}],
        },
    )
    assert r.status_code == 201, r.text

    # inward: purchase from a Karnataka supplier — Input ledgers, inward side
    r = post_gst(
        client,
        {
            "mode": "purchase",
            "party": "Bengaluru Wholesale",
            "party_gstin": KA_GSTIN,
            "supplier_state": SUPPLIER,
            "place_of_supply": SUPPLIER,
            "items": [
                {"description": "Palm oil tins", "hsn": "1511", "qty": 10, "rate": "240.00"}
            ],
        },
    )
    assert r.status_code == 201, r.text

    # quick-tax: no HSN/party facts — excluded from the register, footnoted
    r = post_gst(client, {"rate": "18"})
    assert r.status_code == 201, r.text

    # the inter-state invoice is reversed: it nets out of every statement
    rev = client.post(f"/api/v1/vouchers/{inter_id}/reverse")
    assert rev.status_code == 201, rev.text
    return client


# ------------------------------------------------------------ place of supply
def test_intra_state_sale_splits_cgst_sgst(demo):
    r = post_gst(
        demo,
        b2b_sale(
            "Mysuru Retail",
            KA_GSTIN,
            "29-Karnataka",
            [{"description": "Steel pins", "hsn": "7317", "qty": 10, "rate": "100.00",
              "tax_rate": "18"}],
        ),
        voucher_date=f"{PERIOD_ALT}-10",
    )
    assert r.status_code == 201, r.text
    lines = _lines_of(r)
    assert lines[("Mysuru Retail", "D")] == Decimal("1180.00")  # the grand total
    assert lines[("Sales", "C")] == Decimal("1000.00")
    assert lines[("CGST Payable", "C")] == Decimal("90.00")  # 9% — the half slab
    assert lines[("SGST Payable", "C")] == Decimal("90.00")  # the exact remainder

    # the tax ledgers live under Duties & Taxes
    ledgers = {row["name"]: row for row in demo.get("/api/v1/ledgers").json()}
    assert ledgers["CGST Payable"]["group_name"] == "Duties & Taxes"
    assert ledgers["SGST Payable"]["group_name"] == "Duties & Taxes"


def test_inter_state_sale_levies_igst(demo):
    r = post_gst(
        demo,
        b2b_sale(
            "Coimbatore Co",
            TN_GSTIN,
            "Tamil Nadu",  # by name, not code
            [{"description": "Steel pins", "hsn": "7317", "qty": 10, "rate": "100.00",
              "tax_rate": "18"}],
        ),
        voucher_date=f"{PERIOD_ALT}-11",
    )
    assert r.status_code == 201, r.text
    lines = _lines_of(r)
    assert lines[("Coimbatore Co", "D")] == Decimal("1180.00")
    assert lines[("IGST Payable", "C")] == Decimal("180.00")  # the whole slab
    assert not any(name.startswith(("CGST", "SGST")) for (name, _dc) in lines)
    # the levy decision persists in the computed block (ADR-010)
    computed = r.json()["meta"]["gst"]["computed"]
    assert computed["supply"] == "inter"
    assert computed["place_of_supply"] == "33"
    assert computed["place_of_supply_name"] == "Tamil Nadu"
    assert computed["grand_total"] == "1180.00"


def test_purchase_debits_input_ledgers(demo):
    r = post_gst(
        demo,
        {
            "mode": "purchase",
            "party": "Hubli Suppliers",
            "party_gstin": KA_GSTIN,
            "supplier_state": SUPPLIER,
            "place_of_supply": SUPPLIER,
            "items": [
                {"description": "Palm oil tins", "hsn": "1511", "qty": 5, "rate": "200.00"}
            ],
        },
        voucher_date=f"{PERIOD_ALT}-12",
    )
    assert r.status_code == 201, r.text
    lines = _lines_of(r)
    assert lines[("Hubli Suppliers", "C")] == Decimal("1050.00")  # 1000 + 5% intra
    assert lines[("Purchases", "D")] == Decimal("1000.00")
    assert lines[("Input CGST", "D")] == Decimal("25.00")
    assert lines[("Input SGST", "D")] == Decimal("25.00")
    ledgers = {row["name"]: row for row in demo.get("/api/v1/ledgers").json()}
    assert ledgers["Hubli Suppliers"]["group_name"] == "Sundry Creditors"
    assert ledgers["Input CGST"]["group_name"] == "Duties & Taxes"


def test_quick_tax_honors_place_of_supply(demo):
    # the legacy shape, intra by default — the example pack's behavior, kept
    r = post_gst(demo, {"rate": "18"}, voucher_date=f"{PERIOD_ALT}-13")
    assert r.status_code == 201, r.text
    lines = _lines_of(r)
    assert lines[("Cash", "D")] == Decimal("118.00")  # 100 + 18 tax
    assert lines[("CGST Payable", "C")] == Decimal("9.00")
    assert lines[("SGST Payable", "C")] == Decimal("9.00")

    # ...and inter-state when the place of supply says so (the v0.13 upgrade)
    r = post_gst(
        demo,
        {"rate": "18", "supplier_state": SUPPLIER, "place_of_supply": "33"},
        voucher_date=f"{PERIOD_ALT}-14",
    )
    assert r.status_code == 201, r.text
    lines = _lines_of(r)
    assert lines[("IGST Payable", "C")] == Decimal("18.00")
    assert not any(name.startswith(("CGST", "SGST")) for (name, _dc) in lines)
    assert r.json()["meta"]["gst"]["computed"]["supply"] == "inter"


def test_pos_without_supplier_state_is_refused(demo):
    r = post_gst(demo, {"rate": "18", "place_of_supply": "33"})
    assert r.status_code == 422, r.text
    assert "supplier_state" in r.json()["detail"]


# --------------------------------------------------------------- HSN tables
def test_rate_for_effective_dates():
    from ledgerforge.contrib.gst import DEFAULT_HSN_TABLE as table

    # the famous revision: 8517 (mobile phones) 12% until 2020-03-31, 18% after
    assert rate_for(table, "8517", date(2019, 6, 1)) == Decimal("12")
    assert rate_for(table, "8517129090", date(2020, 3, 31)) == Decimal("12")  # 8-digit HSN
    assert rate_for(table, "8517", date(2020, 4, 1)) == Decimal("18")
    assert rate_for(table, "8517", date(2026, 11, 7)) == Decimal("18")
    # stable chapters
    assert rate_for(table, "0401", date(2018, 1, 1)) == Decimal("0")
    assert rate_for(table, "1511", date(2026, 1, 1)) == Decimal("5")
    assert rate_for(table, "7317", date(2026, 1, 1)) == Decimal("18")
    # an unknown chapter is None — the caller decides how to complain
    assert rate_for(table, "9999", date(2026, 1, 1)) is None


def test_document_uses_hsn_table_at_voucher_date(demo):
    if date.today().year < 2024:  # pragma: no cover — clock-dependent guard
        pytest.skip("needs a system clock after 2023 to backdate into 2019")
    # a 2019 voucher: feature phones carried 12%
    r = post_gst(
        demo,
        b2b_sale(
            "Pondy Phones",
            TN_GSTIN,
            "34",
            [{"description": "Feature phones", "hsn": "8517", "qty": 1, "rate": "1000.00"}],
        ),
        voucher_date="2019-06-01",
    )
    assert r.status_code == 201, r.text
    lines = _lines_of(r)
    assert lines[("IGST Payable", "C")] == Decimal("120.00")  # 12% in 2019
    # the resolved slab is posted on the item — a stored fact (Law 2)
    assert r.json()["meta"]["gst"]["items"][0]["tax_rate"] == "12"


def test_hsn_slab_disagreement_is_a_veto(demo):
    r = post_gst(
        demo,
        b2b_sale(
            "Greedy Traders",
            KA_GSTIN,
            "29",
            [{"description": "Palm oil tins", "hsn": "1511", "qty": 1, "rate": "100.00",
              "tax_rate": "18"}],
        ),
    )
    assert r.status_code == 422, r.text
    assert "disagrees with the HSN table" in r.json()["detail"]


def test_unknown_hsn_and_bad_slabs_are_refused(demo):
    r = post_gst(
        demo,
        b2b_sale(
            "Mystery Traders",
            KA_GSTIN,
            "29",
            [{"description": "Widgets", "hsn": "9999", "qty": 1, "rate": "100.00"}],
        ),
    )
    assert r.status_code == 422, r.text
    assert "not in the rate table" in r.json()["detail"]

    r = post_gst(
        demo,
        b2b_sale(
            "Fat Finger Traders",
            KA_GSTIN,
            "29",
            [{"description": "Steel pins", "qty": 1, "rate": "100.00", "tax_rate": "15"}],
        ),
    )
    assert r.status_code == 422, r.text
    assert "not a GST slab" in r.json()["detail"]


def test_operator_hsn_table_via_env(demo, tmp_path, monkeypatch):
    table = [
        {
            "hsn": "9999",
            "description": "Widgets, assorted",
            "rate": "28",
            "effective_from": "2017-07-01",
        }
    ]
    path = tmp_path / "hsn.json"
    path.write_text(json.dumps(table))
    monkeypatch.setenv("LF_GST_HSN_JSON", str(path))

    r = post_gst(
        demo,
        b2b_sale(
            "Widget Buyers",
            KA_GSTIN,
            "29",
            [{"description": "Widgets", "hsn": "9999", "qty": 1, "rate": "100.00"}],
        ),
        voucher_date=f"{PERIOD_ALT}-15",
    )
    assert r.status_code == 201, r.text
    lines = _lines_of(r)
    assert lines[("CGST Payable", "C")] == Decimal("14.00")  # 28% halved, intra
    monkeypatch.delenv("LF_GST_HSN_JSON")


# ------------------------------------------------------------------- guards
def test_gstin_and_state_guards(demo):
    r = post_gst(demo, b2b_sale("Bad Gstin", "29ABCD1234F1Z5", "29", []))  # 14 chars
    assert r.status_code == 422, r.text
    assert "GSTIN" in r.json()["detail"]

    r = post_gst(
        demo,
        b2b_sale(
            "Bad State", KA_GSTIN, "99", [{"description": "Pins", "qty": 1, "rate": "10"}]
        ),
    )
    assert r.status_code == 422, r.text
    assert "not a GST state" in r.json()["detail"]

    r = post_gst(demo, {"mode": "loan", "party": "X"})
    assert r.status_code == 422, r.text
    assert "mode" in r.json()["detail"]


# ------------------------------------------------------------ the statements
def test_gstr1_b2b_register(demo):
    rows = gstr1_rows(demo, PERIOD)
    assert rows[0] == list(GSTR1_CSV_HEADER)
    data = rows[1:]
    # PERIOD's B2B rows: Chennai Traders alone — Madras Machines was
    # reversed, Walk-in is B2C, the purchase is inward, quick-tax has no
    # party facts
    chennai = next(row for row in data if row[1] == "Chennai Traders")
    assert chennai[0] == KA_GSTIN
    assert chennai[3] == date.today().isoformat()
    assert chennai[4] == "29"  # place of supply
    assert chennai[5] == "18+5+0"  # slabs in first-appearance order
    # taxable: pins 1,000 + palm oil 500 + books 597 = 2,097.00
    assert chennai[6] == "2097.00"
    # intra: CGST = SGST = 90.00 + 12.50 + 0.00 = 102.50, IGST 0
    assert chennai[7] == "102.50"
    assert chennai[8] == "102.50"
    assert chennai[9] == "0.00"
    assert {row[1] for row in data} == {"Chennai Traders"}

    # the response is the machine format: attachment, text/csv
    r = demo.get(
        f"/api/v1/reports/pack/gstr1?period={PERIOD}&format=csv", headers=default_headers(demo)
    )
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == f'attachment; filename="GSTR1_{PERIOD}.csv"'


def test_gstr1_pdf_renders(demo):
    r = demo.get(f"/api/v1/reports/pack/gstr1?period={PERIOD}", headers=default_headers(demo))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.headers["content-disposition"] == f'inline; filename="GSTR1_{PERIOD}.pdf"'
    data = r.content
    assert data.startswith(b"%PDF-1.4")
    assert b"Chennai Traders" in data
    assert b"2,097.00" in data  # taxable
    assert b"205.00" in data  # total tax: 102.50 × 2
    assert b"verify against the portal" in data  # the honesty footer


def test_hsn_summary_sides(demo):
    # default side=outward: PERIOD's sales only
    rows = hsn_rows(demo, PERIOD)
    assert rows[0] == list(GST_HSN_CSV_HEADER)
    outward = {row[1]: row for row in rows[1:]}
    assert outward["7317"][3] == "18"
    # pins 1,000 (B2B) + nails 90 (B2C) = 1,090.00, qty 101
    assert outward["7317"][4] == "101"
    assert outward["7317"][5] == "1090.00"
    assert outward["1511"][5] == "500.00"
    assert outward["4901"][5] == "597.00"
    assert all(row[0] == "outward" for row in rows[1:])

    # side=inward: the purchase only — the input-tax figures
    rows = hsn_rows(demo, f"{PERIOD}&side=inward")
    inward = rows[1:]
    assert len(inward) == 1
    assert inward[0][1] == "1511"
    assert inward[0][4] == "10"
    assert inward[0][5] == "2400.00"
    assert inward[0][6] == "60.00" and inward[0][7] == "60.00"  # 5% split

    # side=both: both directions
    rows = hsn_rows(demo, f"{PERIOD}&side=both")
    assert {row[0] for row in rows[1:]} == {"outward", "inward"}

    # the HSN PDF renders through the shared layout engine
    r = demo.get(f"/api/v1/reports/pack/gst-hsn?period={PERIOD}", headers=default_headers(demo))
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF-1.4")
    assert b"Palm oil" in r.content


def test_statements_net_reversals(demo):
    # post a B2B sale and reverse it: neither register nor summary shows it
    r = post_gst(
        demo,
        b2b_sale(
            "Doomed Traders",
            KA_GSTIN,
            "29",
            [{"description": "Steel pins", "hsn": "7317", "qty": 1, "rate": "500.00"}],
        ),
    )
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    assert any(row[1] == "Doomed Traders" for row in gstr1_rows(demo, PERIOD)[1:])

    rev = demo.post(f"/api/v1/vouchers/{vid}/reverse", headers=default_headers(demo))
    assert rev.status_code == 201, rev.text
    assert not any(row[1] == "Doomed Traders" for row in gstr1_rows(demo, PERIOD)[1:])
    # the HSN summary no longer carries the 500.00 of taxable value
    outward = {row[1]: row for row in hsn_rows(demo, PERIOD)[1:]}
    assert outward["7317"][5] == "1090.00"


def test_nil_month_and_guards(demo):
    r = demo.get(
        f"/api/v1/reports/pack/gstr1?period={PERIOD_EMPTY}", headers=default_headers(demo)
    )
    assert r.status_code == 200
    assert b"nil statement" in r.content
    assert gstr1_rows(demo, PERIOD_EMPTY) == [list(GSTR1_CSV_HEADER)]

    for query, fragment in [
        ("", "period"),
        ("period=2026-13", "YYYY-MM"),
        ("period=not-a-month", "YYYY-MM"),
        (f"period={PERIOD}&format=xlsx", "format"),
        (f"period={PERIOD}&round=centimes", "round"),
    ]:
        r = demo.get(f"/api/v1/reports/pack/gstr1?{query}", headers=default_headers(demo))
        assert r.status_code == 422, (query, r.text)
        assert fragment in r.json()["detail"], (query, r.text)

    # the side= parameter is the HSN summary's
    for query, fragment in [
        (f"period={PERIOD}&side=sideways", "side"),
        (f"period={PERIOD}&format=xlsx", "format"),
        (f"period={PERIOD}&round=centimes", "round"),
    ]:
        r = demo.get(f"/api/v1/reports/pack/gst-hsn?{query}", headers=default_headers(demo))
        assert r.status_code == 422, (query, r.text)
        assert fragment in r.json()["detail"], (query, r.text)


def test_quick_tax_excluded_and_footnoted(demo):
    # a month whose only GST voucher is quick-tax: a nil register, honestly
    r = post_gst(demo, {"rate": "18"}, voucher_date=f"{PERIOD_QUICK}-15")
    assert r.status_code == 201, r.text

    r = demo.get(
        f"/api/v1/reports/pack/gstr1?period={PERIOD_QUICK}", headers=default_headers(demo)
    )
    assert r.status_code == 200
    assert b"quick-tax voucher" in r.content
    assert gstr1_rows(demo, PERIOD_QUICK) == [list(GSTR1_CSV_HEADER)]


def test_books_are_strangers(demo):
    r = demo.post("/api/v1/books", json={"name": "Helios Exports", "base_currency": "INR"})
    assert r.status_code == 201, r.text
    helios_id = r.json()["book"]["id"]

    # the same party name in another book, with different amounts
    r = post_gst(
        demo,
        b2b_sale(
            "Chennai Traders",
            "29ZZZZZ9999C1ZD",
            "29",
            [{"description": "Steel pins", "hsn": "7317", "qty": 1, "rate": "999.00"}],
        ),
        headers={"X-Book-Id": str(helios_id)},
    )
    assert r.status_code == 201, r.text

    # the default book, pinned via header — Helios' 999.00 never leaks
    chennai = next(
        row for row in gstr1_rows(demo, PERIOD)[1:] if row[1] == "Chennai Traders"
    )
    assert chennai[6] == "2097.00"


# ------------------------------------------------------------ the IRN outbox
def test_b2b_sales_queue_after_commit(demo, tmp_path, monkeypatch):
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("LF_GST_OUTBOX", str(outbox))

    r = post_gst(
        demo,
        b2b_sale(
            "IRP Traders",
            KA_GSTIN,
            "29",
            [{"description": "Steel pins", "hsn": "7317", "qty": 2, "rate": "100.00"}],
        ),
        voucher_date=f"{PERIOD_ALT}-16",
    )
    assert r.status_code == 201, r.text

    lines = [json.loads(line) for line in outbox.read_text().splitlines()]
    mine = [line for line in lines if line["voucher_no"] == r.json()["voucher_no"]]
    assert len(mine) == 1
    envelope = mine[0]
    assert envelope["kind"] == "lf-gst-invoice/1"
    assert envelope["buyer"]["gstin"] == KA_GSTIN
    assert envelope["buyer"]["name"] == "IRP Traders"
    assert envelope["place_of_supply"] == "29"
    assert envelope["totals"]["taxable"] == "200.00"
    assert envelope["totals"]["cgst"] == "18.00"
    assert envelope["totals"]["grand_total"] == "236.00"
    assert envelope["items"][0]["hsn"] == "7317"
    monkeypatch.delenv("LF_GST_OUTBOX")


def test_outbox_gates_and_isolation(demo, tmp_path, monkeypatch):
    # no GSTIN (B2C) and skip-irn vouchers never queue — the file stays absent
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("LF_GST_OUTBOX", str(outbox))
    r = post_gst(
        demo,
        {
            "mode": "sale",
            "party": "Walk-in Again",
            "supplier_state": SUPPLIER,
            "items": [{"description": "Nails box", "hsn": "7317", "qty": 1, "rate": "10.00"}],
        },
        voucher_date=f"{PERIOD_ALT}-17",
    )
    assert r.status_code == 201, r.text
    r = post_gst(
        demo,
        b2b_sale(
            "Skipped Traders",
            KA_GSTIN,
            "29",
            [{"description": "Pins", "hsn": "7317", "qty": 1, "rate": "10.00"}],
        ),
        voucher_date=f"{PERIOD_ALT}-18",
        **{"policy:skip_irn": True},
    )
    assert r.status_code == 201, r.text
    assert not outbox.exists()

    # an unwritable outbox is logged and isolated: the posting still lands
    blocker = tmp_path / "no-such-dir"
    blocker.write_text("i am a file, not a directory")
    monkeypatch.setenv("LF_GST_OUTBOX", str(blocker / "sub" / "o.jsonl"))
    r = post_gst(
        demo,
        b2b_sale(
            "Isolated Traders",
            KA_GSTIN,
            "29",
            [{"description": "Pins", "hsn": "7317", "qty": 1, "rate": "10.00"}],
        ),
        voucher_date=f"{PERIOD_ALT}-19",
    )
    assert r.status_code == 201, r.text  # the event handler failed; posting didn't
    monkeypatch.delenv("LF_GST_OUTBOX")


# ---------------------------------------------------------------- the bridge
class _Adapter(BaseHTTPRequestHandler):
    """A stand-in IRP adapter: 200 + ack normally, 400 for 'broken' buyers,
    503 for 'flaky' buyers while unhealthy."""

    healthy = False

    def do_POST(self):  # noqa: N802 — http.server's contract
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        buyer = body.get("buyer", {}).get("name", "")
        if "broken" in buyer.lower():
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "schema version retired"}')
            return
        if "flaky" in buyer.lower() and not _Adapter.healthy:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"error": "try again"}')
            return
        ack = {
            "irn": f"IRN-{body.get('voucher_no', '').replace(' ', '-')}",
            "ack_no": 1234567890,
            "ack_date": "2026-11-07 18:45:00",
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(ack).encode())

    def log_message(self, *args):  # silence the test server
        pass


@pytest.fixture()
def adapter():
    server = HTTPServer(("127.0.0.1", 0), _Adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_bridge_ships_parks_and_retries(tmp_path, adapter):
    outbox = tmp_path / "outbox.jsonl"
    responses = tmp_path / "irn-responses.jsonl"
    pending = [
        {"kind": "lf-gst-invoice/1", "voucher_no": "SAL 10", "voucher_id": 10,
         "buyer": {"name": "Fine Traders", "gstin": KA_GSTIN}, "items": [], "totals": {}},
        {"kind": "lf-gst-invoice/1", "voucher_no": "SAL 11", "voucher_id": 11,
         "buyer": {"name": "Broken Traders", "gstin": KA_GSTIN}, "items": [], "totals": {}},
        {"kind": "lf-gst-invoice/1", "voucher_no": "SAL 12", "voucher_id": 12,
         "buyer": {"name": "Flaky Traders", "gstin": KA_GSTIN}, "items": [], "totals": {}},
    ]
    outbox.write_text("".join(json.dumps(line) + "\n" for line in pending))

    # first pass (adapter unhealthy): 200 for Fine, 400 for Broken, 503 Flaky
    code = bridge_main(
        ["--outbox", str(outbox), "--endpoint", adapter, "--responses", str(responses), "--once"]
    )
    assert code == 1  # a parked 4xx is a nonzero exit — an operator looks
    shipped = [json.loads(line) for line in responses.read_text().splitlines()]
    assert len(shipped) == 1 and shipped[0]["voucher_no"] == "SAL 10"
    assert shipped[0]["irn"] == "IRN-SAL-10"
    assert shipped[0]["ack_no"] == 1234567890
    failed = [
        json.loads(line)
        for line in (tmp_path / "outbox.jsonl.failed.jsonl").read_text().splitlines()
    ]
    assert len(failed) == 1 and failed[0]["voucher_no"] == "SAL 11"
    assert "schema version retired" in failed[0]["error"]
    still = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert [line["voucher_no"] for line in still] == ["SAL 12"]  # the 503 stays pending

    # the adapter recovers: the pending line ships; the parked one stays put
    _Adapter.healthy = True
    code = bridge_main(
        ["--outbox", str(outbox), "--endpoint", adapter, "--responses", str(responses), "--once"]
    )
    assert code == 0
    assert outbox.read_text() == ""
    shipped = [json.loads(line) for line in responses.read_text().splitlines()]
    assert [row["voucher_no"] for row in shipped] == ["SAL 10", "SAL 12"]


def test_bridge_requires_outbox(adapter):
    import os

    saved = os.environ.pop("LF_GST_OUTBOX", None)
    try:
        with pytest.raises(SystemExit) as exc:
            bridge_main(["--endpoint", adapter, "--once"])
        assert exc.value.code == 2  # argparse's usage error
    finally:
        if saved is not None:
            os.environ["LF_GST_OUTBOX"] = saved
