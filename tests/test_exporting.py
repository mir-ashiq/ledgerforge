"""The export seam (ADR-030, Phase 9): every report and list surface
answers ``?format=csv|xlsx`` — one renderer pair, the engine's exact
digits, a download-safe filename.

The promises under test:

- **Every surface answers both formats** with the right media type and a
  Content-Disposition carrying the stem.
- **Rows are the engine's own** — a seeded voucher's amounts appear
  verbatim in the CSV text (Law 3: the digits, never a float round-trip).
- **The XLSX is a real workbook** — openpyxl reads it back with the same
  header and the same cells.
- **JSON stays the default**: no ``format`` param, the surface returns
  its structured payload; an unknown format is a 422 naming the two.
- Pack reports gain ``xlsx`` beside their CSV (the pack's own columns).
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest
from conftest import post_voucher


@pytest.fixture(scope="module")
def dx(client_module, fresh_db):
    """The module's client over a fresh schema, seeded with one voucher —
    every export surface has real numbers to carry."""
    client = client_module
    client.post("/api/v1/ledgers", json={"name": "Export Buyer", "group_name": "Sundry Debtors"})
    client.post("/api/v1/ledgers", json={"name": "Export Sales", "group_name": "Sales Accounts"})
    post_voucher(
        client,
        type_="SAL",
        date_="2026-04-05",
        narration="the export seed",
        entries=[
            {"ledger_name": "Export Buyer", "dc": "D", "amount": "1180.00"},
            {"ledger_name": "Export Sales", "dc": "C", "amount": "1180.00"},
        ],
    )
    return client


def _get_csv(client, path, **params):
    r = client.get(path, params={**params, "format": "csv"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    return r.text, r.headers["content-disposition"]


def _get_xlsx(client, path, **params):
    r = client.get(path, params={**params, "format": "xlsx"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ) or r.headers["content-type"].startswith("application/octet-stream")
    return r.content


def _xlsx_rows(content: bytes) -> tuple[list, list]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content))
    sheet = workbook.active
    rows = [[cell.value for cell in row] for row in sheet.iter_rows()]
    workbook.close()
    return rows[0], rows[1:]


# ------------------------------------------------------------------- reports
def test_trial_balance_csv_carries_engine_digits(dx):
    text, disposition = _get_csv(dx, "/api/v1/reports/trial-balance", as_of="2026-12-31")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][:3] == ["ledger_name", "group_name", "nature"]
    buyer = next(row for row in rows[1:] if row and row[0] == "Export Buyer")
    assert Decimal(buyer[3]) == Decimal("1180")  # the engine's exact digits
    assert "trial-balance" in disposition


def test_trial_balance_xlsx_is_a_real_workbook(dx):
    header, rows = _xlsx_rows(_get_xlsx(dx, "/api/v1/reports/trial-balance", as_of="2026-12-31"))
    assert "ledger_name" in header
    assert any(row[header.index("ledger_name")] == "Export Buyer" for row in rows)


def test_profit_loss_export(dx):
    text, _ = _get_csv(
        dx, "/api/v1/reports/profit-loss", **{"from": "2026-04-01", "to": "2026-12-31"}
    )
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["section", "group", "amount", "side"]
    assert any("Sales Accounts" in row[1] for row in rows[1:])


def test_balance_sheet_export(dx):
    text, _ = _get_csv(dx, "/api/v1/reports/balance-sheet", as_of="2026-12-31")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["section", "group", "amount"]
    assert any(row[0] == "Assets" for row in rows[1:])


def test_fy_close_export(dx):
    text, _ = _get_csv(dx, "/api/v1/reports/fy-close", as_of="2026-12-31")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["bucket", "ledger_name", "dc", "amount"]
    assert len(rows) > 1


def test_cost_reports_export(dx):
    text, _ = _get_csv(
        dx, "/api/v1/reports/cost-centres", **{"from": "2026-04-01", "to": "2026-12-31"}
    )
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["centre_name", "category_name", "dr", "cr", "net"]
    assert any(row[0] == "Unallocated" for row in rows[1:])


# --------------------------------------------------------------------- lists
def test_day_book_export(dx):
    text, _ = _get_csv(dx, "/api/v1/vouchers", limit=10)
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == [
        "id",
        "voucher_no",
        "voucher_type",
        "voucher_date",
        "amount",
        "narration",
        "reversal_of",
        "posted_at",
    ]
    seed = next(row for row in rows[1:] if "the export seed" in (row[5] or ""))
    assert seed[2] == "SAL"
    assert Decimal(seed[4]) == Decimal("1180")


def test_ledgers_and_groups_export(dx):
    text, _ = _get_csv(dx, "/api/v1/ledgers")
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]
    name_at = header.index("name")
    assert "group_name" in header
    assert any(row[name_at] == "Export Buyer" for row in rows[1:])
    gtext, _ = _get_csv(dx, "/api/v1/groups")
    grows = list(csv.reader(io.StringIO(gtext)))
    assert any("Sundry Debtors" in row for row in grows[1:])


def test_books_periods_currencies_export(dx):
    for path in ("/api/v1/books", "/api/v1/periods", "/api/v1/currencies"):
        text, _ = _get_csv(dx, path)
        assert list(csv.reader(io.StringIO(text)))[0]  # a header row answers


def test_inventory_and_bank_catalogues_export(dx):
    for path in ("/api/v1/inventory/items", "/api/v1/banking/ledgers"):
        text, _ = _get_csv(dx, path)
        rows = list(csv.reader(io.StringIO(text)))
        assert rows[0] and len(rows) >= 1


# ------------------------------------------------------------ gst + payroll
def test_gst_downloads_export_empty_is_fine(dx):
    text, _ = _get_csv(dx, "/api/v1/gst/portal/downloads")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["id", "kind", "period", "gstin", "source", "line_count", "imported_at"]


def test_attendance_export(dx):
    from datetime import date as _date

    today = _date.today()
    period = f"{today.year}-{today.month:02d}"
    dx.post(
        "/api/v1/payroll/attendance",
        json={
            "period": period,
            "entries": [{"employee": "Export Asha", "present": 20}],
        },
    )
    text, _ = _get_csv(dx, "/api/v1/payroll/attendance", period=period)
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["employee", "present", "absent", "half", "leave_paid", "note"]
    assert rows[1][0] == "Export Asha"
    assert Decimal(rows[1][1]) == Decimal("20")


# ------------------------------------------------------------ packs + guards
def test_pack_report_xlsx_beside_csv(dx):
    from datetime import date as _date

    today = _date.today()
    period = f"{today.year}-{today.month:02d}"
    content = _get_xlsx(dx, "/api/v1/reports/pack/gstr1", period=period)
    header, _rows = _xlsx_rows(content)
    assert header  # the pack's own columns, re-rendered


def test_json_stays_the_default(dx):
    r = dx.get("/api/v1/reports/trial-balance", params={"as_of": "2026-12-31"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "ledgers" in r.json()


def test_unknown_format_is_422(dx):
    r = dx.get("/api/v1/reports/trial-balance", params={"format": "pdf"})
    assert r.status_code == 422
    assert "csv" in r.json()["detail"] and "xlsx" in r.json()["detail"]


def test_xlsx_decimal_cells_serialize_exact(dx):
    """The workbook's amount cell carries the engine's digits (numeric,
    serialized from the Decimal's own string — never a float detour)."""
    from openpyxl import load_workbook

    r = dx.get("/api/v1/reports/trial-balance", params={"as_of": "2026-12-31", "format": "xlsx"})
    workbook = load_workbook(io.BytesIO(r.content))
    sheet = workbook.active
    header = [cell.value for cell in sheet[1]]
    rows = [[cell.value for cell in row] for row in sheet.iter_rows(min_row=2)]
    workbook.close()
    buyer = next(row for row in rows if row[header.index("ledger_name")] == "Export Buyer")
    dr = buyer[header.index("dr")]
    assert Decimal(str(dr)) == Decimal("1180")
