"""The GST compliance depth (contrib/gst.py + gst_compliance.py, ADR-028,
roadmap Phase 7 / issue #13): credit & debit notes with returns netting,
multi-GSTIN segmentation, the GSTR-3B summary, the GSTR-9 scaffolding,
the GSTR-2A/2B ITC reconciliation against imported portal downloads, and
the e-way-bill outbox with its out-of-process bridge.

The promises under test:
- Notes: a credit note walks its sale backwards (party credited, revenue
  and tax debited — the same ledgers, opposite side) and nets every
  outward figure; a debit note does the same to the inward side and the
  ITC. The register names each document (INV/CN/DN).
- Multi-GSTIN: a voucher's supplier_gstin segments every statement; the
  ?gstin= scope filters, the unfiltered statement carries all.
- GSTR-3B: outward intra/inter, the eligible ITC and the net payable
  from the same folds — the ledger's numbers or no numbers; the
  not-captured heads (reverse charge, exempt) are blanks, never guesses.
- GSTR-9: twelve monthly folds and the annual totals, scaffolding the
  annexure tables without inventing portal-only cells.
- The comparison: the books' inward register (keyed on the supplier's
  GSTIN and their own document number) against the imported portal
  statement — matched / mismatched / missing both sides, magnitudes
  priced, the ITC both sides claim; inward documents without the
  supplier's number are counted, never guessed.
- The portal import: all-or-nothing, idempotent by fingerprint; a
  revised download supersedes (the latest snapshot per registration).
- The e-way-bill outbox: threshold-crossing documents queue after commit
  (LF_GST_EWB_OUTBOX), below-threshold/skip-policy/quick-tax never do;
  the bridge ships (2xx), parks (4xx) and retries (5xx) — the IRN
  discipline, applied to movement.

Fixture discipline (the statutory suite's): one module-scoped fresh
schema; the demo tax month (PERIOD) is seeded once and asserted on
exactly — later tests post into their own months (or run after the
PERIOD assertions, in file order), so PERIOD stays reproducible.
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

from ledgerforge.contrib.ewb_bridge import main as ewb_bridge_main
from ledgerforge.contrib.gst import (
    GST_RECON_CSV_HEADER,
    GSTR3B_CSV_HEADER,
    GSTR9_CSV_HEADER,
)

SUPPLIER = "29"
KA_GSTIN = "29ABCDE1234F1Z5"
MH_BUYER = "27AAACA1234A1Z5"
MH_SUPPLIER = "27BBBBA1234B1Z2"

# this book's own two registrations (multi-GSTIN, ADR-028)
REG_KA = "29PQRSX1234K1ZP"
REG_MH = "27PQRSX1234M1Z9"


def _month(offset: int = 0) -> str:
    y, m = date.today().year, date.today().month + offset
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m += 12
        y += 1
    return f"{y:04d}-{m:02d}"


PERIOD = _month(0)  # the module's shared demo month
PERIOD_ALT = _month(-1)  # the multi-GSTIN month
PERIOD_EWB = _month(-2)  # the e-way-bill month

BALANCED = [
    {"ledger_name": "Cash", "dc": "D", "amount": "100.00"},
    {"ledger_name": "Cash", "dc": "C", "amount": "100.00"},
]


def default_headers(client) -> dict | None:
    books = client.get("/api/v1/books").json()
    if len(books) == 1:
        return None
    default_id = next(b["id"] for b in books if b["slug"] == "default")
    return {"X-Book-Id": str(default_id)}


def post_gst(client, doc, voucher_date=None, headers=None, entries=None, **extra):
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


def pack_json(client, key: str, query: str) -> dict:
    r = client.get(
        f"/api/v1/reports/pack/{key}?{query}&format=json",
        headers=default_headers(client),
    )
    assert r.status_code == 200, r.text
    return r.json()


def pack_csv(client, key: str, query: str) -> list[list[str]]:
    r = client.get(
        f"/api/v1/reports/pack/{key}?{query}&format=csv",
        headers=default_headers(client),
    )
    assert r.status_code == 200, r.text
    return list(csv.reader(io.StringIO(r.text)))


def download_2b(period: str, entries: list[dict], gstin: str = REG_KA) -> str:
    """One operator-adapted portal download (the lf-gst-2b/1 contract)."""
    return json.dumps(
        {
            "kind": "lf-gst-2b/1",
            "period": period,
            "gstin": gstin,
            "source": "GSTR-2B (test)",
            "entries": entries,
        }
    )


def portal_entry(
    doc_no: str,
    taxable: str,
    *,
    doc_type: str = "INV",
    cgst: str = "0.00",
    sgst: str = "0.00",
    igst: str = "0.00",
    supplier: str = KA_GSTIN,
    party: str = "Bengaluru Wholesale",
    itc_available: bool = True,
) -> dict:
    return {
        "supplier_gstin": supplier,
        "party": party,
        "doc_type": doc_type,
        "doc_no": doc_no,
        "doc_date": date.today().isoformat(),
        "taxable": taxable,
        "cgst": cgst,
        "sgst": sgst,
        "igst": igst,
        "pos": "29",
        "itc_available": itc_available,
    }


def import_2b(client, text: str, dry_run: bool = False):
    return client.post(
        "/api/v1/gst/portal/import",
        json={"text": text, "source": "GSTR-2B (test)", "dry_run": dry_run},
        headers=default_headers(client),
    )


@pytest.fixture(scope="module")
def demo(client_module, fresh_db):
    """The module's shared demo month (PERIOD), seeded once: a B2B sale and
    its partial credit note (returns netting, outward), a supplier purchase
    and its debit note (the ITC's netting, inward) — the exact figures
    every 3B/9/recon assertion prices."""
    client = client_module

    # outward: B2B intra-state sale — Steel pins 100 × 10.00 @ 18%
    r = post_gst(
        client,
        {
            "mode": "sale",
            "party": "Chennai Traders",
            "party_gstin": KA_GSTIN,
            "supplier_state": SUPPLIER,
            "place_of_supply": SUPPLIER,
            "items": [
                {
                    "description": "Steel pins",
                    "hsn": "7317",
                    "qty": 100,
                    "rate": "10.00",
                    "tax_rate": "18",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text

    # outward return: credit note — 10 pins come back (a tenth of the sale)
    r = post_gst(
        client,
        {
            "mode": "credit_note",
            "party": "Chennai Traders",
            "party_gstin": KA_GSTIN,
            "supplier_state": SUPPLIER,
            "place_of_supply": SUPPLIER,
            "items": [
                {
                    "description": "Steel pins return",
                    "hsn": "7317",
                    "qty": 10,
                    "rate": "10.00",
                    "tax_rate": "18",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text

    # inward: purchase — palm oil 10 × 240.00 @ 5% (the supplier's number rides)
    r = post_gst(
        client,
        {
            "mode": "purchase",
            "party": "Bengaluru Wholesale",
            "party_gstin": KA_GSTIN,
            "supplier_state": SUPPLIER,
            "place_of_supply": SUPPLIER,
            "invoice_no": "BW-101",
            "items": [{"description": "Palm oil tins", "hsn": "1511", "qty": 10, "rate": "240.00"}],
        },
    )
    assert r.status_code == 201, r.text

    # inward return: debit note — 2 tins go back (the supplier will file
    # their own credit note against the same number)
    r = post_gst(
        client,
        {
            "mode": "debit_note",
            "party": "Bengaluru Wholesale",
            "party_gstin": KA_GSTIN,
            "supplier_state": SUPPLIER,
            "place_of_supply": SUPPLIER,
            "invoice_no": "BW-CN-1",
            "items": [
                {
                    "description": "Palm oil tins return",
                    "hsn": "1511",
                    "qty": 2,
                    "rate": "240.00",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    return client


# ------------------------------------------------------------- the notes
def test_credit_note_walks_the_sale_backwards(demo):
    r = post_gst(
        demo,
        {
            "mode": "credit_note",
            "party": "Return Traders",
            "party_gstin": KA_GSTIN,
            "supplier_state": SUPPLIER,
            "place_of_supply": SUPPLIER,
            "items": [
                {
                    "description": "Steel pins return",
                    "hsn": "7317",
                    "qty": 1,
                    "rate": "10.00",
                    "tax_rate": "18",
                }
            ],
        },
        voucher_date=f"{PERIOD_EWB}-15",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    by_ledger = {e["ledger_name"]: e for e in body["entries"]}
    # the party line credited at the grand total (the receivable gives back)
    assert by_ledger["Return Traders"]["dc"] == "C"
    assert Decimal(by_ledger["Return Traders"]["amount"]) == Decimal("11.80")
    # the revenue leg debited on Sales Returns (income walked backwards)
    assert by_ledger["Sales Returns"]["dc"] == "D"
    assert Decimal(by_ledger["Sales Returns"]["amount"]) == Decimal("10.00")
    # the tax leg debited on the SAME CGST/SGST Payable ledgers the sale used
    assert by_ledger["CGST Payable"]["dc"] == "D"
    assert Decimal(by_ledger["CGST Payable"]["amount"]) == Decimal("0.90")
    assert by_ledger["SGST Payable"]["dc"] == "D"
    assert Decimal(by_ledger["SGST Payable"]["amount"]) == Decimal("0.90")


def test_debit_note_walks_the_purchase_backwards(demo):
    r = post_gst(
        demo,
        {
            "mode": "debit_note",
            "party": "Supplier Returns Ltd",
            "party_gstin": KA_GSTIN,
            "supplier_state": SUPPLIER,
            "place_of_supply": SUPPLIER,
            "invoice_no": "SR-1",
            "items": [
                {
                    "description": "Palm oil tins return",
                    "hsn": "1511",
                    "qty": 1,
                    "rate": "240.00",
                }
            ],
        },
        voucher_date=f"{PERIOD_EWB}-15",
    )
    assert r.status_code == 201, r.text
    by_ledger = {e["ledger_name"]: e for e in r.json()["entries"]}
    assert by_ledger["Supplier Returns Ltd"]["dc"] == "D"
    assert by_ledger["Purchase Returns"]["dc"] == "C"
    assert Decimal(by_ledger["Purchase Returns"]["amount"]) == Decimal("240.00")
    # the input credit reverses on the SAME Input ledgers
    assert by_ledger["Input CGST"]["dc"] == "C"
    assert Decimal(by_ledger["Input CGST"]["amount"]) == Decimal("6.00")
    assert by_ledger["Input SGST"]["dc"] == "C"


def test_notes_net_the_ledger(demo):
    tb = demo.get(
        f"/api/v1/reports/trial-balance?as_of={date.today().isoformat()}",
        headers=default_headers(demo),
    ).json()
    by_ledger = {row["ledger_name"]: row for row in tb["ledgers"]}
    # CGST Payable: 90.00 (sale) − 9.00 (PERIOD credit note) − 0.90 (the
    # PERIOD_ALT note posted above) = 80.10 credit — the balance is as-of
    # today, every posted note included
    payable = by_ledger["CGST Payable"]
    assert Decimal(payable["cr"]) == Decimal("90.00")
    assert Decimal(payable["dr"]) == Decimal("9.90")
    assert payable["dc"] == "C" and Decimal(payable["balance"]) == Decimal("80.10")
    # Input CGST: 60.00 (purchase) − 12.00 (PERIOD debit note) − 6.00 (the
    # PERIOD_ALT note posted above) = 42.00 debit
    inputs = by_ledger["Input CGST"]
    assert Decimal(inputs["dr"]) == Decimal("60.00")
    assert Decimal(inputs["cr"]) == Decimal("18.00")
    assert inputs["dc"] == "D" and Decimal(inputs["balance"]) == Decimal("42.00")
    # the returns ledgers carry their own masses (both months' notes)
    assert Decimal(by_ledger["Sales Returns"]["dr"]) == Decimal("110.00")
    assert Decimal(by_ledger["Purchase Returns"]["cr"]) == Decimal("720.00")


def test_gstr1_register_names_the_notes(demo):
    data = pack_json(demo, "gstr1", f"period={PERIOD}")
    rows = data["invoices"]
    by_type = {row["doc_type"]: row for row in rows}
    assert set(by_type) == {"INV", "CN"}
    assert by_type["INV"]["taxable"] == "1000.00"
    assert by_type["INV"]["cgst"] == "90.00"
    # the credit note nets the register: negative taxable, negative tax
    assert by_type["CN"]["taxable"] == "-100.00"
    assert by_type["CN"]["cgst"] == "-9.00"
    assert by_type["CN"]["sgst"] == "-9.00"
    # the computed block carries the match key facts the recon needs
    assert data["gstin"] is None  # unfiltered: the whole book


def test_gstr3b_the_summary_math(demo):
    data = pack_json(demo, "gstr3b", f"period={PERIOD}")
    o, i = data["outward"], data["inward"]
    # sale 1,000 − credit note 100 = 900 intra taxable; tax 180 − 18 = 162
    assert o["intra"]["taxable"] == "900.00"
    assert o["intra"]["cgst"] == "81.00"
    assert o["intra"]["sgst"] == "81.00"
    assert o["intra"]["igst"] == "0.00"
    assert o["documents"] == 1 and o["returns"] == 1
    assert i["intra"]["taxable"] == "1920.00"  # 2,400 − 480
    # ITC: 120 (purchase) − 24 (debit note) = 96; payable 162 − 96 = 66
    assert data["itc_available"] == "96.00"
    assert data["outward_tax"] == "162.00"
    assert data["net_payable"] == "66.00"


def test_gstr3b_renderings_and_honest_blanks(demo):
    rows = pack_csv(demo, "gstr3b", f"period={PERIOD}")
    assert rows[0] == list(GSTR3B_CSV_HEADER)
    body = {row[1]: row for row in rows[1:]}
    assert body["Taxable intra-state supplies (net of returns)"][2:] == [
        "900.00",
        "81.00",
        "81.00",
        "0.00",
    ]
    assert body["Net ITC available"][5] == "96.00"
    assert body["Net tax payable (outward tax − net ITC)"][5] == "66.00"
    # the not-captured heads are named, never guessed (paren-free bytes:
    # the PDF escapes parentheses in its text strings)
    r = demo.get(f"/api/v1/reports/pack/gstr3b?period={PERIOD}", headers=default_headers(demo))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert b"Reverse-charge imports" in r.content
    assert b"exempt inward supplies" in r.content


def test_gstr9_scaffolding(demo):
    year = PERIOD[:4]
    data = pack_json(demo, "gstr9", f"period={year}")
    assert [m["month"] for m in data["months"]] == [f"{year}-{m:02d}" for m in range(1, 13)]
    month = next(m for m in data["months"] if m["month"] == PERIOD)
    assert month["outward_taxable"] == "900.00"
    assert month["cgst"] == "81.00"
    assert month["itc"] == "96.00"
    # the annual totals are exactly the twelve months' own sums
    t = data["totals"]
    assert Decimal(t["outward_taxable"]) == sum(
        (Decimal(m["outward_taxable"]) for m in data["months"]), Decimal("0")
    )
    assert Decimal(t["itc"]) == sum((Decimal(m["itc"]) for m in data["months"]), Decimal("0"))
    assert Decimal(t["net_payable"]) == (
        Decimal(t["cgst"]) + Decimal(t["sgst"]) + Decimal(t["igst"]) - Decimal(t["itc"])
    )
    # the CSV scaffolds the same twelve rows
    rows = pack_csv(demo, "gstr9", f"period={year}")
    assert rows[0] == list(GSTR9_CSV_HEADER)
    assert len(rows) == 14  # header + 12 months + total
    # the guards: a month-shaped period is refused — the 9 wants a year
    r = demo.get(f"/api/v1/reports/pack/gstr9?period={PERIOD}", headers=default_headers(demo))
    assert r.status_code == 422
    assert "YYYY" in r.json()["detail"]


# --------------------------------------------------------- multi-GSTIN
def test_multi_gstin_segments_every_statement(demo):
    # two registrations, one book: a Karnataka sale and a Maharashtra one
    r = post_gst(
        demo,
        {
            "mode": "sale",
            "party": "Local Traders",
            "party_gstin": KA_GSTIN,
            "supplier_state": "29",
            "supplier_gstin": REG_KA,
            "place_of_supply": "29",
            "items": [
                {
                    "description": "Steel pins",
                    "hsn": "7317",
                    "qty": 5,
                    "rate": "50.00",
                    "tax_rate": "18",
                }
            ],
        },
        voucher_date=f"{PERIOD_ALT}-10",
    )
    assert r.status_code == 201, r.text
    r = post_gst(
        demo,
        {
            "mode": "sale",
            "party": "Mumbai Traders",
            "party_gstin": MH_BUYER,
            "supplier_state": "27",
            "supplier_gstin": REG_MH,
            "place_of_supply": "27",
            "items": [
                {
                    "description": "Steel pins",
                    "hsn": "7317",
                    "qty": 10,
                    "rate": "100.00",
                    "tax_rate": "18",
                }
            ],
        },
        voucher_date=f"{PERIOD_ALT}-11",
    )
    assert r.status_code == 201, r.text
    # an inward purchase on the Maharashtra registration, with the
    # supplier's number — the filtered comparison's matched row
    r = post_gst(
        demo,
        {
            "mode": "purchase",
            "party": "Pune Wholesale",
            "party_gstin": MH_SUPPLIER,
            "supplier_state": "27",
            "supplier_gstin": REG_MH,
            "place_of_supply": "27",
            "invoice_no": "MH-SUP-1",
            "items": [
                {
                    "description": "Palm oil tins",
                    "hsn": "1511",
                    "qty": 4,
                    "rate": "500.00",
                }
            ],
        },
        voucher_date=f"{PERIOD_ALT}-12",
    )
    assert r.status_code == 201, r.text

    # unfiltered: both registrations' sales
    both = pack_json(demo, "gstr1", f"period={PERIOD_ALT}")
    assert len(both["invoices"]) == 2
    # each registration sees only its own
    ka = pack_json(demo, "gstr1", f"period={PERIOD_ALT}&gstin={REG_KA}")
    assert [row["party"] for row in ka["invoices"]] == ["Local Traders"]
    assert ka["gstin"] == REG_KA
    mh = pack_json(demo, "gstr1", f"period={PERIOD_ALT}&gstin={REG_MH}")
    assert [row["party"] for row in mh["invoices"]] == ["Mumbai Traders"]
    # the 3B segments too: Maharashtra's month is its own sale plus its
    # own purchase's ITC (2,000 of palm oil @ 5%)
    mh3b = pack_json(demo, "gstr3b", f"period={PERIOD_ALT}&gstin={REG_MH}")
    assert mh3b["outward"]["intra"]["taxable"] == "1000.00"
    assert mh3b["outward"]["intra"]["cgst"] == "90.00"
    assert mh3b["itc_available"] == "100.00"

    # the filtered comparison end-to-end: the Maharashtra registration's
    # download matches its own purchase
    text = download_2b(
        PERIOD_ALT,
        [
            portal_entry(
                "MH-SUP-1",
                "2000.00",
                cgst="50.00",
                sgst="50.00",
                supplier=MH_SUPPLIER,
                party="Pune Wholesale",
            )
        ],
        gstin=REG_MH,
    )
    r = import_2b(demo, text)
    assert r.status_code == 201, r.text
    recon = pack_json(demo, "gst-recon", f"period={PERIOD_ALT}&kind=2b&gstin={REG_MH}")
    assert recon["counts"] == {
        "matched": 1,
        "mismatched": 0,
        "missing_books": 0,
        "missing_portal": 0,
    }
    assert recon["itc"]["books"] == "100.00"
    assert recon["itc"]["portal"] == "100.00"
    assert recon["itc"]["difference"] == "0.00"
    # a garbage gstin scope is refused loudly
    r = demo.get(
        f"/api/v1/reports/pack/gstr1?period={PERIOD_ALT}&gstin=not-a-gstin",
        headers=default_headers(demo),
    )
    assert r.status_code == 422


# -------------------------------------------------- the portal import
def test_portal_import_receipt_lines_and_guards(demo):
    entries = [
        portal_entry("BW-101", "2400.00", cgst="60.00", sgst="60.00"),
        portal_entry("BW-CN-1", "480.00", doc_type="CN", cgst="12.00", sgst="12.00"),
    ]
    text = download_2b(PERIOD, entries)

    # dry run: validated, nothing written
    r = import_2b(demo, text, dry_run=True)
    assert r.status_code == 201, r.text
    assert r.json()["dry_run"] is True and r.json()["new"] == 2
    assert demo.get("/api/v1/gst/portal/downloads", headers=default_headers(demo)).json()[
        "downloads"
    ] == [] or all(
        d["period"] != PERIOD
        for d in demo.get("/api/v1/gst/portal/downloads", headers=default_headers(demo)).json()[
            "downloads"
        ]
    )

    # the real import: a receipt with the counts
    r = import_2b(demo, text)
    assert r.status_code == 201, r.text
    receipt = r.json()
    assert receipt["kind"] == "2b" and receipt["period"] == PERIOD
    assert receipt["gstin"] == REG_KA
    assert receipt["new"] == 2 and receipt["duplicates"] == 0 and receipt["revision"] == 1

    # the lines feed serves the stored facts
    lines = demo.get(
        f"/api/v1/gst/portal/lines?kind=2b&period={PERIOD}",
        headers=default_headers(demo),
    ).json()["lines"]
    assert [line["doc_no"] for line in lines] == ["BW-101", "BW-CN-1"]

    # the guards: all-or-nothing, loud
    for bad in (
        "not json",
        json.dumps({"kind": "lf-gst-2b/1"}),
        json.dumps({"kind": "lf-gst-2b/1", "period": PERIOD, "gstin": "bad"}),
        download_2b(
            PERIOD,
            [
                {
                    "supplier_gstin": KA_GSTIN,
                    "doc_no": "X",
                    "doc_type": "XX",
                    "doc_date": "2026-01-01",
                    "taxable": "1",
                }
            ],
        ),
    ):
        r = import_2b(demo, bad)
        assert r.status_code == 422, (bad, r.text)


def test_reimport_is_idempotent_and_revisions_supersede(demo):
    entries = [portal_entry("BW-900", "100.00", cgst="5.00", sgst="5.00")]
    first = import_2b(demo, download_2b(PERIOD, entries))
    assert first.status_code == 201 and first.json()["new"] == 1
    # the same download again: every fingerprint reproduces — a no-op
    again = import_2b(demo, download_2b(PERIOD, entries))
    assert again.status_code == 201
    assert again.json()["new"] == 0 and again.json()["duplicates"] == 1
    # a revised row (the portal moved) lands as a new snapshot…
    revised = [portal_entry("BW-900", "150.00", cgst="7.50", sgst="7.50")]
    third = import_2b(demo, download_2b(PERIOD, revised))
    assert third.status_code == 201 and third.json()["revision"] == 3
    # …and the fold prefers the latest snapshot
    lines = demo.get(
        f"/api/v1/gst/portal/lines?kind=2b&period={PERIOD}",
        headers=default_headers(demo),
    ).json()["lines"]
    bw900 = [line for line in lines if line["doc_no"] == "BW-900"]
    assert len(bw900) == 1 and Decimal(bw900[0]["taxable"]) == Decimal("150.00")
    # the receipts list carries the honest provenance
    downloads = demo.get("/api/v1/gst/portal/downloads", headers=default_headers(demo)).json()[
        "downloads"
    ]
    assert len([d for d in downloads if d["period"] == PERIOD and d["kind"] == "2b"]) == 3


# ------------------------------------------------- the comparison fold
def test_recon_matched_mismatched_missing_both_sides(demo):
    # books-only document (the portal has not drafted it yet)
    r = post_gst(
        demo,
        {
            "mode": "purchase",
            "party": "Bengaluru Wholesale",
            "party_gstin": KA_GSTIN,
            "supplier_state": SUPPLIER,
            "place_of_supply": SUPPLIER,
            "invoice_no": "BW-200",
            "items": [{"description": "Palm oil tins", "hsn": "1511", "qty": 5, "rate": "240.00"}],
        },
    )
    assert r.status_code == 201, r.text
    # books document the portal disagrees with (amounts differ)
    r = post_gst(
        demo,
        {
            "mode": "purchase",
            "party": "Bengaluru Wholesale",
            "party_gstin": KA_GSTIN,
            "supplier_state": SUPPLIER,
            "place_of_supply": SUPPLIER,
            "invoice_no": "BW-300",
            "items": [{"description": "Palm oil tins", "hsn": "1511", "qty": 8, "rate": "250.00"}],
        },
    )
    assert r.status_code == 201, r.text

    text = download_2b(
        PERIOD,
        [
            portal_entry("BW-101", "2400.00", cgst="60.00", sgst="60.00"),
            # the supplier's own credit note — pairs the books' debit note
            portal_entry("BW-CN-1", "480.00", doc_type="CN", cgst="12.00", sgst="12.00"),
            # the mismatch: books 2,000/100, portal 2,100/105
            portal_entry("BW-300", "2100.00", cgst="52.50", sgst="52.50"),
            # portal-only: the supplier filed, the books never booked it
            portal_entry("BW-102", "999.00", igst="179.82"),
            # portal-only AND ITC-ineligible (the portal's own verdict)
            portal_entry("BW-950", "500.00", cgst="22.50", sgst="22.50", itc_available=False),
        ],
    )
    r = import_2b(demo, text)
    assert r.status_code == 201, r.text

    data = pack_json(demo, "gst-recon", f"period={PERIOD}&kind=2b")
    assert data["counts"] == {
        "matched": 2,
        "mismatched": 1,
        "missing_books": 2,
        "missing_portal": 1,
    }
    by_no = {row["doc_no"]: row for row in data["rows"]}
    # matched: invoice and the DN↔CN pair, magnitudes priced
    assert by_no["BW-101"]["state"] == "matched"
    assert by_no["BW-CN-1"]["state"] == "matched"
    assert by_no["BW-CN-1"]["books_taxable"] == "-480.00"
    assert by_no["BW-CN-1"]["portal_taxable"] == "480.00"
    # mismatched: the reason names the disagreement exactly
    assert by_no["BW-300"]["state"] == "mismatched"
    assert "taxable 2000.00↔2100.00" in by_no["BW-300"]["reason"]
    assert "tax 100.00↔105.00" in by_no["BW-300"]["reason"]
    # missing both sides
    assert by_no["BW-102"]["state"] == "missing_books"
    assert by_no["BW-200"]["state"] == "missing_portal"
    assert by_no["BW-200"]["books_taxable"] == "1200.00"
    # the ITC both sides claim: books 120−24+100+60 = 256; portal
    # 120−24+105+179.82+45 = 425.82 — the difference is priced exactly
    assert data["itc"]["books"] == "256.00"
    assert data["itc"]["portal"] == "425.82"
    assert data["itc"]["difference"] == "-169.82"
    assert data["itc"]["portal_ineligible_rows"] == 1
    assert data["itc"]["portal_ineligible_tax"] == "45.00"
    # the CSV carries every state, machine-readable
    rows = pack_csv(demo, "gst-recon", f"period={PERIOD}&kind=2b")
    assert rows[0] == list(GST_RECON_CSV_HEADER)
    assert len(rows) == 1 + 6  # header + every document compared
    # the PDF renders with the four buckets named
    r = demo.get(
        f"/api/v1/reports/pack/gst-recon?period={PERIOD}&kind=2b",
        headers=default_headers(demo),
    )
    assert r.status_code == 200
    assert b"Missing in books" in r.content
    assert b"Missing in portal" in r.content
    # kind guards: a 2c statement is not a thing
    r = demo.get(
        f"/api/v1/reports/pack/gst-recon?period={PERIOD}&kind=2c",
        headers=default_headers(demo),
    )
    assert r.status_code == 422


def test_unmatchable_inward_is_counted_never_guessed(demo):
    # an inward document with the supplier's GSTIN but no invoice number:
    # the comparison cannot key it — counted, excluded, footnoted
    r = post_gst(
        demo,
        {
            "mode": "purchase",
            "party": "Nameless Invoice Co",
            "party_gstin": KA_GSTIN,
            "supplier_state": SUPPLIER,
            "place_of_supply": SUPPLIER,
            "items": [{"description": "Palm oil tins", "hsn": "1511", "qty": 1, "rate": "240.00"}],
        },
        voucher_date=f"{PERIOD_ALT}-20",
    )
    assert r.status_code == 201, r.text
    data = pack_json(demo, "gst-recon", f"period={PERIOD_ALT}&kind=2b")
    assert data["unmatchable_books"] >= 1
    assert all(row["doc_no"] != "" for row in data["rows"])  # no invented keys
    # a period with no import at all: an honest empty comparison
    empty = pack_json(demo, "gst-recon", f"period={PERIOD_ALT}&kind=2a")
    assert empty["rows"] == [] or all(row["state"] in ("missing_portal",) for row in empty["rows"])


# ------------------------------------------------- the e-way-bill outbox
def test_ewb_outbox_threshold_and_gates(demo, tmp_path, monkeypatch):
    outbox = tmp_path / "ewb.jsonl"
    monkeypatch.setenv("LF_GST_EWB_OUTBOX", str(outbox))

    def ewb_doc(mode, qty, rate, **extra):
        doc = {
            "mode": mode,
            "party": "Movers & Co",
            "party_gstin": KA_GSTIN,
            "supplier_state": SUPPLIER,
            "supplier_gstin": REG_KA,
            "place_of_supply": SUPPLIER,
            "items": [
                {
                    "description": "Machine tools",
                    "hsn": "7317",
                    "qty": qty,
                    "rate": rate,
                    "tax_rate": "18",
                }
            ],
        }
        doc.update(extra)
        return doc

    # above the line: 50 × 1,000 = 50,000 taxable + 9,000 tax → queues
    r = post_gst(demo, ewb_doc("sale", 50, "1000.00"), voucher_date=f"{PERIOD_EWB}-05")
    assert r.status_code == 201, r.text
    # below the line: 1 × 1,000 → never queues
    r = post_gst(demo, ewb_doc("sale", 1, "1000.00"), voucher_date=f"{PERIOD_EWB}-06")
    assert r.status_code == 201, r.text
    # the operator opts out: above the line, skipped by policy
    r = post_gst(
        demo,
        ewb_doc("sale", 50, "1000.00"),
        voucher_date=f"{PERIOD_EWB}-07",
        **{"policy:skip_ewb": True},
    )
    assert r.status_code == 201, r.text
    # quick-tax carries no goods facts: never queues
    r = post_gst(
        demo,
        {"rate": "18"},
        voucher_date=f"{PERIOD_EWB}-08",
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "60000.00"},
            {"ledger_name": "Sales", "dc": "C", "amount": "60000.00"},
        ],
    )
    assert r.status_code == 201, r.text
    # an inward movement above the line queues too — goods move both ways
    r = post_gst(demo, ewb_doc("purchase", 50, "1000.00"), voucher_date=f"{PERIOD_EWB}-09")
    assert r.status_code == 201, r.text

    lines = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert [line["doc_type"] for line in lines] == ["INV", "INV"]
    sale = lines[0]
    assert sale["kind"] == "lf-gst-ewaybill/1"
    assert sale["registrant"]["gstin"] == REG_KA
    assert sale["counterparty"]["gstin"] == KA_GSTIN
    assert sale["totals"]["grand_total"] == "59000.00"
    assert sale["movement"]["vehicle_no"] == ""  # Part-B facts ride when captured
    assert sale["voucher_no"]

    # the deployment's own line moves the threshold: nothing more queues
    outbox2 = tmp_path / "ewb2.jsonl"
    monkeypatch.setenv("LF_GST_EWB_OUTBOX", str(outbox2))
    monkeypatch.setenv("LF_GST_EWB_THRESHOLD", "100000")
    r = post_gst(demo, ewb_doc("sale", 50, "1000.00"), voucher_date=f"{PERIOD_EWB}-10")
    assert r.status_code == 201, r.text
    assert not outbox2.exists()

    # Part-B facts ride the envelope when the operator captured them
    outbox3 = tmp_path / "ewb3.jsonl"
    monkeypatch.setenv("LF_GST_EWB_OUTBOX", str(outbox3))
    monkeypatch.delenv("LF_GST_EWB_THRESHOLD")
    doc = ewb_doc("sale", 50, "1000.00")
    doc["eway"] = {
        "transporter_gstin": "29TRNSP0001A1Z5",
        "transporter_name": "Deccan Freight",
        "vehicle_no": "KA01AB1234",
        "from_place": "Bengaluru warehouse",
        "distance_km": 350,
    }
    r = post_gst(demo, doc, voucher_date=f"{PERIOD_EWB}-11")
    assert r.status_code == 201, r.text
    env = json.loads(outbox3.read_text().splitlines()[0])
    assert env["movement"]["vehicle_no"] == "KA01AB1234"
    assert env["movement"]["transporter_name"] == "Deccan Freight"
    assert env["movement"]["distance_km"] == 350


# --------------------------------------------------------- the bridge
class _EwbAdapter(BaseHTTPRequestHandler):
    """A stand-in e-way-bill adapter: 200 + EWB number normally, 400 for
    'broken' documents, 503 while unhealthy."""

    healthy = False

    def do_POST(self):  # noqa: N802 — http.server's contract
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        no = body.get("voucher_no", "")
        if "broken" in no.lower():
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "invalid part-A details"}')
            return
        if "flaky" in no.lower() and not _EwbAdapter.healthy:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"error": "portal busy"}')
            return
        ack = {
            "ewb_no": f"1210{abs(hash(no)) % 10**8:08d}",
            "ewb_date": "2026-11-08 10:30:00",
            "valid_upto": "2026-11-10 23:59:00",
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(ack).encode())

    def log_message(self, *args):  # silence the test server
        pass


@pytest.fixture()
def adapter():
    server = HTTPServer(("127.0.0.1", 0), _EwbAdapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_ewb_bridge_ships_parks_and_retries(tmp_path, adapter):
    outbox = tmp_path / "ewb.jsonl"
    responses = tmp_path / "ewb-responses.jsonl"
    pending = [
        {"kind": "lf-gst-ewaybill/1", "voucher_no": "SAL 20", "voucher_id": 20},
        {"kind": "lf-gst-ewaybill/1", "voucher_no": "BROKEN 21", "voucher_id": 21},
        {"kind": "lf-gst-ewaybill/1", "voucher_no": "FLAKY 22", "voucher_id": 22},
    ]
    outbox.write_text("".join(json.dumps(line) + "\n" for line in pending))

    code = ewb_bridge_main(
        ["--outbox", str(outbox), "--endpoint", adapter, "--responses", str(responses), "--once"]
    )
    assert code == 1  # a parked 4xx is a nonzero exit — an operator looks
    shipped = [json.loads(line) for line in responses.read_text().splitlines()]
    assert len(shipped) == 1 and shipped[0]["voucher_no"] == "SAL 20"
    assert shipped[0]["ewb_no"].isdigit() and len(shipped[0]["ewb_no"]) == 12
    assert shipped[0]["valid_upto"] == "2026-11-10 23:59:00"
    failed = [
        json.loads(line) for line in (tmp_path / "ewb.jsonl.failed.jsonl").read_text().splitlines()
    ]
    assert len(failed) == 1 and failed[0]["voucher_no"] == "BROKEN 21"
    assert "invalid part-A details" in failed[0]["error"]
    still = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert [line["voucher_no"] for line in still] == ["FLAKY 22"]

    _EwbAdapter.healthy = True
    code = ewb_bridge_main(
        ["--outbox", str(outbox), "--endpoint", adapter, "--responses", str(responses), "--once"]
    )
    assert code == 0
    assert outbox.read_text() == ""
    shipped = [json.loads(line) for line in responses.read_text().splitlines()]
    assert [row["voucher_no"] for row in shipped] == ["SAL 20", "FLAKY 22"]


def test_ewb_bridge_requires_outbox(adapter):
    import os

    saved = os.environ.pop("LF_GST_EWB_OUTBOX", None)
    try:
        with pytest.raises(SystemExit) as exc:
            ewb_bridge_main(["--endpoint", adapter, "--once"])
        assert exc.value.code == 2  # argparse's usage error
    finally:
        if saved is not None:
            os.environ["LF_GST_EWB_OUTBOX"] = saved
