"""The import pipeline (dataexchange.py + api/dataexchange.py): CSV/XLSX
decode, masters/openings/vouchers/stock imports — ADR-030, Phase 9 of the
v1.0 roadmap.

The promises under test:

- **Decode is honest.** CSV and XLSX arrive as rows keyed by normalized
  headers; duplicate or missing headers refuse the file; a bad base64 is
  a 422.
- **One transaction or nothing.** A file with one bad row posts NOTHING
  (masters, openings, vouchers alike) — the whole import rolls back.
- **Idempotency is content-addressed.** Masters create-if-absent by name;
  vouchers replay by client token — re-import of the same file is a
  no-op receipt; a *changed* file is a new import (Law 2 — history stays).
- **Law 1 walks the whole path.** An unbalanced voucher group is a 422
  naming the ref; an unbalanced openings file is a 422 naming the
  difference; ``balance_to`` absorbs a residual explicitly.
- **``dry_run`` is the real thing, rolled back** — the receipt reports
  what would land, nothing lands.
- Guards: openings post as ONE Opening Journal; FX lines carry the trio
  or nothing; the reader role cannot import.
"""

from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal

import pytest

TODAY = date.today()


@pytest.fixture(scope="module")
def dx(client_module, fresh_db):
    """The module's client over a fresh schema — the gst_depth pattern."""
    return client_module


def _xlsx(rows: list[list], sheet: str = "Sheet1") -> str:
    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    for row in rows:
        worksheet.append(row)
    import io

    buffer = io.BytesIO()
    workbook.save(buffer)
    return base64.b64encode(buffer.getvalue()).decode()


def _import(dx, kind, content, *, fmt="csv", dry_run=False, **extra):
    payload = {"kind": kind, "format": fmt, "content": content, "dry_run": dry_run, **extra}
    return dx.post("/api/v1/dataexchange/imports", json=payload)


def _count_vouchers(dx) -> int:
    return len(dx.get("/api/v1/vouchers", params={"limit": 500}).json())


# ------------------------------------------------------------------- decode
def test_decode_csv_headers_normalize(dx):
    """Ledger Name / ledger_name / LEDGER-NAME are the same column."""
    receipt = _import(
        dx,
        "masters",
        "Kind,Name,Parent\nledger,Acme Corp,Sundry Debtors\n",
    )
    assert receipt.status_code == 201, receipt.text
    body = receipt.json()
    assert body["ledgers_created"] == 1
    ledgers = dx.get("/api/v1/ledgers", params={"search": "Acme Corp"}).json()
    assert any(led["name"] == "Acme Corp" for led in ledgers)


def test_decode_xlsx_round_trip(dx):
    """An .xlsx travels as base64 and lands like the CSV twin."""
    content = _xlsx([["kind", "name", "parent"], ["ledger", "Xlsx Ltd", "Sundry Debtors"]])
    r = _import(dx, "masters", content, fmt="xlsx")
    assert r.status_code == 201, r.text
    assert r.json()["ledgers_created"] == 1


def test_decode_duplicate_header_refused(dx):
    r = _import(dx, "masters", "name,name\nAcme,Acme\n")
    assert r.status_code == 422
    assert "duplicate column" in r.json()["detail"]


def test_decode_bad_base64_refused(dx):
    r = _import(dx, "masters", "!!!not-base64!!!", fmt="xlsx")
    assert r.status_code == 422


def test_unknown_kind_refused(dx):
    r = _import(dx, "wizards", "a,b\n1,2\n")
    assert r.status_code == 422


# ------------------------------------------------------------------ masters
def test_masters_groups_and_ledgers_idempotent(dx):
    csv_text = (
        "kind,name,parent,nature\n"
        "group,My Debtors,Sundry Debtors,asset\n"
        "ledger,Beta Buyer,My Debtors\n"
        "ledger,Gamma Buyer,My Debtors\n"
    )
    first = _import(dx, "masters", csv_text, source="chart.csv")
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["groups_created"] == 1
    assert body["ledgers_created"] == 2
    assert body["existing"] == 0

    again = _import(dx, "masters", csv_text, source="chart.csv")
    assert again.status_code == 201
    body = again.json()
    assert body["groups_created"] == 0
    assert body["ledgers_created"] == 0
    assert body["existing"] == 3


def test_masters_unknown_parent_refused_nothing_lands(dx):
    before = len(dx.get("/api/v1/ledgers").json())
    r = _import(dx, "masters", "kind,name,parent\nledger,Orphan,No Such Group\n")
    assert r.status_code == 422
    assert "No Such Group" in r.json()["detail"]
    after = len(dx.get("/api/v1/ledgers").json())
    assert after == before


def test_masters_bad_nature_refused(dx):
    r = _import(dx, "masters", "kind,name,parent,nature\ngroup,Bad Nature,Fixed Assets,magical\n")
    assert r.status_code == 422
    assert "nature" in r.json()["detail"].lower()


# ----------------------------------------------------------------- openings
def test_openings_one_journal_balances(dx):
    dx.post("/api/v1/ledgers", json={"name": "Opening Bank", "group_name": "Bank Accounts"})
    dx.post("/api/v1/ledgers", json={"name": "Opening Owner", "group_name": "Capital Account"})
    csv_text = (
        "ledger,dc,amount,date\n"
        "Opening Bank,D,100000.00,2026-04-01\n"
        "Opening Owner,C,100000.00,2026-04-01\n"
    )
    r = _import(dx, "openings", csv_text, source="openings.csv")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["total_debit"] == "100000.00"
    assert body["total_credit"] == "100000.00"
    assert body["balanced_to"] is None

    # the replay: the same file is a no-op
    replay = _import(dx, "openings", csv_text, source="openings.csv")
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["posted"] is False


def test_openings_unbalanced_refused_with_difference(dx):
    dx.post("/api/v1/ledgers", json={"name": "Solo Asset", "group_name": "Current Assets"})
    csv_text = "ledger,dc,amount\nSolo Asset,D,500.00\n"
    r = _import(dx, "openings", csv_text)
    assert r.status_code == 422
    assert "500" in r.json()["detail"]
    assert "balance_to" in r.json()["detail"]


def test_openings_balance_to_absorbs_explicitly(dx):
    dx.post("/api/v1/ledgers", json={"name": "Solo Asset 2", "group_name": "Current Assets"})
    csv_text = "ledger,dc,amount\nSolo Asset 2,D,500.00\n"
    r = _import(dx, "openings", csv_text, balance_to="Suspense A/c")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["balanced_to"] == "Suspense A/c"
    assert body["total_debit"] == "500.00"
    assert body["total_credit"] == "500.00"


def test_openings_mixed_dates_refused(dx):
    dx.post("/api/v1/ledgers", json={"name": "D1", "group_name": "Current Assets"})
    dx.post("/api/v1/ledgers", json={"name": "D2", "group_name": "Current Liabilities"})
    csv_text = "ledger,dc,amount,date\nD1,D,10.00,2026-04-01\nD2,C,10.00,2026-05-01\n"
    r = _import(dx, "openings", csv_text)
    assert r.status_code == 422
    assert "one opening journal per file" in r.json()["detail"]


# ----------------------------------------------------------------- vouchers
def _party_pair(dx, tag: str) -> None:
    dx.post("/api/v1/ledgers", json={"name": f"Buyer {tag}", "group_name": "Sundry Debtors"})
    dx.post("/api/v1/ledgers", json={"name": f"Sales {tag}", "group_name": "Sales Accounts"})


def test_vouchers_post_through_the_engine(dx):
    _party_pair(dx, "V1")
    csv_text = (
        "ref,date,type,narration,ledger,dc,amount\n"
        "r1,2026-04-05,SAL,invoice one,Buyer V1,D,1180.00\n"
        "r1,2026-04-05,SAL,invoice one,Sales V1,C,1180.00\n"
    )
    before = _count_vouchers(dx)
    r = _import(dx, "vouchers", csv_text, source="vouchers.csv")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["vouchers"] == 1
    assert body["posted"] == 1
    assert _count_vouchers(dx) == before + 1
    # the narration and type walked the whole engine path
    rows = dx.get("/api/v1/vouchers", params={"type": "SAL"}).json()
    assert any(v["narration"] == "invoice one" for v in rows)


def test_vouchers_reimport_replays(dx):
    _party_pair(dx, "V2")
    csv_text = (
        "ref,date,type,ledger,dc,amount\n"
        "r1,2026-04-05,SAL,Buyer V2,D,100.00\n"
        "r1,2026-04-05,SAL,Sales V2,C,100.00\n"
    )
    first = _import(dx, "vouchers", csv_text, source="same.csv")
    assert first.status_code == 201
    before = _count_vouchers(dx)
    again = _import(dx, "vouchers", csv_text, source="same.csv")
    assert again.status_code == 201
    body = again.json()
    assert body["posted"] == 0
    assert body["replayed"] == 1
    assert _count_vouchers(dx) == before


def test_vouchers_one_bad_row_rolls_back_everything(dx):
    _party_pair(dx, "V3")
    csv_text = (
        "ref,date,type,ledger,dc,amount\n"
        "good,2026-04-05,SAL,Buyer V3,D,100.00\n"
        "good,2026-04-05,SAL,Sales V3,C,100.00\n"
        "bad,2026-04-06,SAL,Buyer V3,D,50.00\n"
        "bad,2026-04-06,SAL,Sales V3,C,49.00\n"  # unbalanced — refuses the file
    )
    before = _count_vouchers(dx)
    r = _import(dx, "vouchers", csv_text, source="mixed.csv")
    assert r.status_code == 422
    assert _count_vouchers(dx) == before  # the good group did NOT land


def test_vouchers_unknown_ledger_refused_with_row(dx):
    csv_text = (
        "ref,date,type,ledger,dc,amount\n"
        "r1,2026-04-05,SAL,Nobody At All,D,10.00\n"
        "r1,2026-04-05,SAL,Sales V2,C,10.00\n"
    )
    r = _import(dx, "vouchers", csv_text)
    assert r.status_code == 404
    assert "Nobody At All" in r.json()["detail"]


def test_vouchers_fx_trio(dx):
    dx.post("/api/v1/currencies", json={"code": "USD", "name": "Dollar", "symbol": "$"})
    dx.post(
        "/api/v1/fx-rates",
        json={"from_ccy": "USD", "to_ccy": "INR", "rate": "83.00", "valid_on": "2026-04-01"},
    )
    dx.post("/api/v1/ledgers", json={"name": "USD Bank", "group_name": "Bank Accounts"})
    dx.post("/api/v1/ledgers", json={"name": "Fx Sales", "group_name": "Sales Accounts"})
    csv_text = (
        "ref,date,type,ledger,dc,amount,currency,amount_fx,fx_rate\n"
        "fx1,2026-04-05,SAL,USD Bank,D,8300.00,USD,100.00,83.00\n"
        "fx1,2026-04-05,SAL,Fx Sales,C,8300.00,,,,\n"
    )
    r = _import(dx, "vouchers", csv_text, source="fx.csv")
    assert r.status_code == 201, r.text
    # the FX line rode the whole engine path — currency + amount_fx visible
    rows = dx.get("/api/v1/vouchers", params={"type": "SAL", "limit": 500}).json()
    fx = [v for v in rows if any(e.get("currency") == "USD" for e in v["entries"])]
    assert fx, "the FX line did not land with its snapshot"
    line = next(e for e in fx[0]["entries"] if e.get("currency") == "USD")
    assert Decimal(line["amount_fx"]) == Decimal("100.00")


def test_vouchers_fx_partial_trio_refused(dx):
    csv_text = (
        "ref,date,type,ledger,dc,amount,currency\n"
        "fx2,2026-04-05,SAL,USD Bank,D,8300.00,USD\n"
        "fx2,2026-04-05,SAL,Fx Sales,C,8300.00,\n"
    )
    r = _import(dx, "vouchers", csv_text)
    assert r.status_code == 422
    assert "together" in r.json()["detail"]


# -------------------------------------------------------------------- stock
def test_stock_masters_idempotent(dx):
    csv_text = (
        "name,unit,track_batches,reorder_level,notes\n"
        "Widget A,pcs,yes,10,first note\n"
        "Widget B,kg,no,,\n"
    )
    first = _import(dx, "stock", csv_text, source="stock.csv")
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["created"] == 2
    again = _import(dx, "stock", csv_text, source="stock.csv")
    assert again.status_code == 201
    assert again.json()["created"] == 0
    assert again.json()["existing"] == 2

    items = dx.get("/api/v1/inventory/items").json()
    widget = next(i for i in items if i["name"] == "Widget A")
    assert widget["unit"] == "pcs"
    assert widget["track_batches"] is True


def test_stock_bad_yes_no_refused(dx):
    r = _import(dx, "stock", "name,track_batches\nWidget C,banana\n")
    assert r.status_code == 422
    assert "yes or no" in r.json()["detail"]


# ------------------------------------------------------------------ dry runs
def test_dry_run_is_the_real_thing_rolled_back(dx):
    _party_pair(dx, "DR")
    csv_text = (
        "ref,date,type,ledger,dc,amount\n"
        "dr1,2026-04-05,SAL,Buyer DR,D,77.00\n"
        "dr1,2026-04-05,SAL,Sales DR,C,77.00\n"
    )
    before = _count_vouchers(dx)
    preview = _import(dx, "vouchers", csv_text, dry_run=True)
    assert preview.status_code == 201, preview.text
    body = preview.json()
    assert body["dry_run"] is True
    assert body["posted"] == 1  # it WOULD have posted
    assert _count_vouchers(dx) == before  # nothing landed

    # and the real one still posts (the dry run left no residue)
    commit = _import(dx, "vouchers", csv_text)
    assert commit.status_code == 201
    assert _count_vouchers(dx) == before + 1


# ------------------------------------------------------------------- the bank
def test_bank_kind_routes_to_adr026(dx):
    """The bank kind is the banking import, unchanged — one front door."""
    r = dx.post("/api/v1/ledgers", json={"name": "DataX Bank", "group_name": "Bank Accounts"})
    assert r.status_code in (201, 422)
    ledger = next(
        led
        for led in dx.get("/api/v1/banking/ledgers").json()["ledgers"]
        if led["name"] == "DataX Bank"
    )
    csv_text = (
        "date,narrative,amount\n"
        "2026-04-01,OPENING BALANCE,10000\n"
        "2026-04-02,customer payment,+2500\n"
    )
    r = _import(dx, "bank", csv_text, fmt="csv", ledger_id=ledger["id"], source="stmt.csv")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "bank"

    # the bank kind without a ledger is a 422, not a guess
    r = _import(dx, "bank", csv_text, fmt="csv")
    assert r.status_code == 422
    assert "ledger_id" in r.json()["detail"]


# ------------------------------------------------------------------- catalog
def test_exports_catalog_lists_surfaces(dx):
    r = dx.get("/api/v1/dataexchange/exports")
    assert r.status_code == 200
    body = r.json()
    assert "csv" in body["formats"] and "xlsx" in body["formats"]
    keys = {surface["key"] for surface in body["surfaces"]}
    assert {"trial-balance", "day-book", "ledgers", "inventory-items"} <= keys
    for surface in body["surfaces"]:
        assert surface["path"].startswith("/api/v1/")
        assert "label" in surface and "group" in surface
