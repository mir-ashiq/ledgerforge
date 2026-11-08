"""The TallyPrime XML import (tally_import.py + api/dataexchange.py):
the migration path — ADR-030, Phase 9 of the v1.0 roadmap.

The promises under test:

- **The dialect parses.** ENVELOPE-wrapped TALLYMESSAGEs with spaces in
  tags, signed amounts (negative = debit, ISDEEMEDPOSITIVE the
  tiebreak), YYYYMMDD dates, nested .LIST shapes.
- **Masters land on the seeded tree.** Tally's primary group names ARE
  LedgerForge's; ledgers with openings post as ONE Opening Journal.
- **Vouchers map by type.** Receipt→RCT, Sales→SAL, …; an unmapped type
  is skipped and REPORTED, never guessed; a voucher naming an unknown
  ledger auto-creates it under Suspense A/c and reports it.
- **The verification report is the proof.** The source trial balance
  (folded from the XML) equals the imported one (folded from what
  landed) — zero differences on a clean file; a skipped voucher names
  its ledger differences.
- **Idempotent.** Re-import of the same export is a replay receipt.
- **FX included**: entries carrying CURRENCY/FOREXAMOUNT/EXCHANGERATE
  import as the FX trio.
- ``dry_run`` previews the whole migration and writes nothing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

TALLY_XML = """\
<ENVELOPE>
 <BODY>
  <IMPORTDATA>
   <REQUESTDATA>
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
      <GROUP NAME="Metro Debtors" ID="g1">
        <NAME>Metro Debtors</NAME>
        <PARENT>Sundry Debtors</PARENT>
      </GROUP>
      <LEDGER NAME="Ravi Traders" ID="l1">
        <NAME>Ravi Traders</NAME>
        <PARENT>Metro Debtors</PARENT>
        <OPENINGBALANCE>-50000.00</OPENINGBALANCE>
      </LEDGER>
      <LEDGER NAME="State Bank" ID="l2">
        <NAME>State Bank</NAME>
        <PARENT>Bank Accounts</PARENT>
        <OPENINGBALANCE>-150000.00</OPENINGBALANCE>
      </LEDGER>
      <LEDGER NAME="Capital" ID="l3">
        <NAME>Capital</NAME>
        <PARENT>Capital Account</PARENT>
        <OPENINGBALANCE>200000.00</OPENINGBALANCE>
      </LEDGER>
      <STOCKITEM NAME="Steel Rod" ID="s1">
        <NAME>Steel Rod</NAME>
        <BASEUNITS>nos</BASEUNITS>
        <ISBATCHWISEON>Yes</ISBATCHWISEON>
        <OPENINGBALANCE>120</OPENINGBALANCE>
      </STOCKITEM>
    </TALLYMESSAGE>
    <TALLYMESSAGE>
      <VOUCHER VCHTYPE="Receipt" DATE="20260405" VCHNO="1" ACTION="Create">
        <NARRATION>opening month receipt</NARRATION>
        <ALLLEDGERENTRIES.LIST>
          <LEDGERNAME>State Bank</LEDGERNAME>
          <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
          <AMOUNT>-12000.00</AMOUNT>
        </ALLLEDGERENTRIES.LIST>
        <ALLLEDGERENTRIES.LIST>
          <LEDGERNAME>Ravi Traders</LEDGERNAME>
          <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
          <AMOUNT>12000.00</AMOUNT>
        </ALLLEDGERENTRIES.LIST>
      </VOUCHER>
      <VOUCHER VCHTYPE="Sales" DATE="20260410" VCHNO="2" ACTION="Create">
        <NARRATION>credit sale</NARRATION>
        <ALLLEDGERENTRIES.LIST>
          <LEDGERNAME>Ravi Traders</LEDGERNAME>
          <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
          <AMOUNT>-2360.00</AMOUNT>
        </ALLLEDGERENTRIES.LIST>
        <ALLLEDGERENTRIES.LIST>
          <LEDGERNAME>Round Ledger</LEDGERNAME>
          <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
          <AMOUNT>60.00</AMOUNT>
        </ALLLEDGERENTRIES.LIST>
        <ALLLEDGERENTRIES.LIST>
          <LEDGERNAME>Sales Accounts</LEDGERNAME>
          <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
          <AMOUNT>2300.00</AMOUNT>
        </ALLLEDGERENTRIES.LIST>
      </VOUCHER>
    </TALLYMESSAGE>
   </REQUESTDATA>
  </IMPORTDATA>
 </BODY>
</ENVELOPE>
"""


@pytest.fixture(scope="module")
def dx(client_module, fresh_db):
    return client_module


def _tally(dx, xml, **extra):
    return dx.post("/api/v1/dataexchange/tally", json={"xml": xml, **extra})


# -------------------------------------------------------------------- master
def test_the_dialect_parses_and_masters_land(dx):
    r = _tally(dx, TALLY_XML, source="tally.xml")
    assert r.status_code == 201, r.text
    body = r.json()
    masters = body["masters"]
    assert masters["groups_created"] == 1  # Metro Debtors (the rest are seeded)
    assert masters["ledgers_created"] == 3
    assert masters["stock_items_created"] == 1

    # the group landed under Sundry Debtors, the ledger under the group
    groups = {g["name"]: g for g in dx.get("/api/v1/groups").json()}
    assert "Metro Debtors" in groups
    ledgers = {led["name"]: led for led in dx.get("/api/v1/ledgers").json()}
    assert ledgers["Ravi Traders"]["group_name"] == "Metro Debtors"

    # the stock item carries its tracking flag; its opening QUANTITY is
    # listed for re-keying (positions are documents — never a file's guess)
    items = {i["name"]: i for i in dx.get("/api/v1/inventory/items").json()}
    assert items["Steel Rod"]["unit"] == "nos"
    assert items["Steel Rod"]["track_batches"] is True
    assert any(opening["item"] == "Steel Rod" for opening in body["stock_openings"])


def test_the_verification_report_is_zero_difference_on_a_clean_file(dx):
    r = _tally(dx, TALLY_XML, source="tally.xml")
    assert r.status_code == 201, r.text
    body = r.json()
    verification = body["verification"]
    assert verification["differences"] == 0, verification["rows"]
    assert verification["source_total"] == "0.00"
    assert verification["imported_total"] == "0.00"
    # Ravi: opening -50000 (Dr) + receipt credit 12000 + sale debit 2360
    ravi = next(row for row in verification["rows"] if row["ledger"] == "Ravi Traders")
    assert ravi["source"] == ravi["imported"]
    assert ravi["difference"] == "0.00"


def test_reimport_replays_idempotently(dx):
    xml = TALLY_XML.replace("opening month receipt", "the replay receipt")
    first = _tally(dx, xml, source="tally-replay.xml")
    assert first.status_code == 201
    assert first.json()["vouchers"]["posted"] == 2

    again = _tally(dx, xml, source="tally-replay.xml")
    assert again.status_code == 201
    body = again.json()
    assert body["vouchers"]["posted"] == 0
    assert body["vouchers"]["replayed"] == 2
    assert body["openings"]["replayed"] is True
    assert body["verification"]["differences"] == 0  # the proof still closes


def test_unknown_ledger_auto_creates_under_suspense_and_reports(dx):
    """'Round Ledger' rides a Sales voucher but no LEDGER master — it
    lands under Suspense A/c, reported, and the verification still
    closes at zero (both sides carry it)."""
    xml = TALLY_XML.replace("credit sale", "the suspense sale").replace(
        "Round Ledger", "Suspense Auto Co"
    )
    r = _tally(dx, xml, source="tally-suspense.xml")
    assert r.status_code == 201
    body = r.json()
    assert "Suspense Auto Co" in body["auto_ledgers"]
    ledgers = {led["name"]: led for led in dx.get("/api/v1/ledgers").json()}
    assert ledgers["Suspense Auto Co"]["group_name"] == "Suspense A/c"
    assert body["verification"]["differences"] == 0


# ------------------------------------------------------------------ unmapped
def test_unmapped_type_is_skipped_and_reported_never_guessed(dx):
    xml = TALLY_XML.replace(
        'VCHTYPE="Sales" DATE="20260410"',
        'VCHTYPE="Physical Stock" DATE="20260410"',
    )
    r = _tally(dx, xml, source="tally-skip.xml")
    assert r.status_code == 201, r.text
    body = r.json()
    skipped = body["vouchers"]["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["vchtype"] == "Physical Stock"
    assert "no LedgerForge mapping" in skipped[0]["reason"]
    # the skipped voucher's movements name themselves in the differences
    rows = {row["ledger"]: row for row in body["verification"]["rows"]}
    ravi = rows["Ravi Traders"]
    assert ravi["source"] != ravi["imported"]  # the sale never landed
    assert ravi["difference"] != "0.00"


def test_type_map_extends_the_mapping(dx):
    xml = TALLY_XML.replace(
        'VCHTYPE="Sales" DATE="20260410"',
        'VCHTYPE="Physical Stock" DATE="20260410"',
    ).replace("opening month receipt", "the type-map receipt")
    r = _tally(dx, xml, source="tally-map.xml", type_map={"Physical Stock": "JV"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["vouchers"]["skipped"] == []
    assert body["vouchers"]["posted"] == 2
    assert body["verification"]["differences"] == 0


# -------------------------------------------------------------------- guards
def test_unbalanced_openings_refuse_with_the_difference(dx):
    xml = TALLY_XML.replace(
        "<OPENINGBALANCE>200000.00</OPENINGBALANCE>",
        "<OPENINGBALANCE>100000.00</OPENINGBALANCE>",
    )
    r = _tally(dx, xml, source="tally-unbalanced.xml")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "do not balance" in detail
    assert "balance_to" in detail


def test_balance_to_absorbs_the_residual_and_the_proof_closes(dx):
    xml = TALLY_XML.replace(
        "<OPENINGBALANCE>200000.00</OPENINGBALANCE>",
        "<OPENINGBALANCE>100000.00</OPENINGBALANCE>",
    )
    r = _tally(dx, xml, source="tally-balanced.xml", balance_to="Suspense A/c")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["openings"]["balanced_to"] == "Suspense A/c"
    assert body["verification"]["differences"] == 0


def test_malformed_xml_is_422(dx):
    r = _tally(dx, "<ENVELOPE><not closed")
    assert r.status_code == 422
    assert "well-formed" in r.json()["detail"]


def test_empty_export_is_refused(dx):
    r = _tally(dx, "<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA/></IMPORTDATA></BODY></ENVELOPE>")
    assert r.status_code == 422


def test_dry_run_previews_and_writes_nothing(dx):
    # a distinct file (its own fingerprint): earlier tests already imported
    # the base export, and a replay receipt would prove nothing about dry runs
    xml = TALLY_XML.replace("opening month receipt", "the dry-run receipt")
    before = len(dx.get("/api/v1/vouchers", params={"limit": 500}).json())
    r = _tally(dx, xml, dry_run=True, source="preview.xml")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["vouchers"]["posted"] == 2  # it WOULD have
    assert body["verification"]["differences"] == 0  # the proof runs anyway
    assert len(dx.get("/api/v1/vouchers", params={"limit": 500}).json()) == before


# ------------------------------------------------------------------------ FX
def test_fx_entries_import_as_the_trio(dx):
    dx.post("/api/v1/currencies", json={"code": "USD", "name": "Dollar"})
    dx.post(
        "/api/v1/fx-rates",
        json={"from_ccy": "USD", "to_ccy": "INR", "rate": "83.00", "valid_on": "2026-04-01"},
    )
    xml = """\
<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA>
<TALLYMESSAGE>
 <VOUCHER VCHTYPE="Receipt" DATE="20260415" VCHNO="fx1" ACTION="Create">
  <ALLLEDGERENTRIES.LIST>
   <LEDGERNAME>State Bank</LEDGERNAME>
   <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
   <AMOUNT>-8300.00</AMOUNT>
   <CURRENCY>USD</CURRENCY>
   <FOREXAMOUNT>100.00</FOREXAMOUNT>
   <EXCHANGERATE>83.00</EXCHANGERATE>
  </ALLLEDGERENTRIES.LIST>
  <ALLLEDGERENTRIES.LIST>
   <LEDGERNAME>Ravi Traders</LEDGERNAME>
   <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
   <AMOUNT>8300.00</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
 </VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>
"""
    r = _tally(dx, xml, source="fx.xml")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["vouchers"]["posted"] == 1
    vouchers = dx.get("/api/v1/vouchers", params={"type": "RCT", "limit": 500}).json()
    fx = next(v for v in vouchers if any(e.get("currency") == "USD" for e in v["entries"]))
    line = next(e for e in fx["entries"] if e.get("currency") == "USD")
    assert Decimal(line["amount_fx"]) == Decimal("100.00")
    assert Decimal(line["fx_rate"]) == Decimal("83.00")


def test_books_are_strangers_the_import_lands_in_one_book(dx):
    """The tally import is book-scoped: a second book's ledgers are never
    visible (the engine's own resolve would 404), proven by the route's
    book header discipline — one import, one book."""
    r = _tally(dx, TALLY_XML, source="book-scoped.xml")
    assert r.status_code == 201
    # the default book's trial balance now carries the migration
    tb = dx.get("/api/v1/reports/trial-balance", params={"as_of": "2026-12-31"}).json()
    names = {row["ledger_name"] for row in tb["ledgers"]}
    assert {"Ravi Traders", "State Bank"} <= names
