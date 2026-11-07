"""The statutory reporting bridge (contrib/statutory.py, ADR-014): PF & ESI
statements folded from payroll runs — the first consumer of plugin API 0.6
pack reports (``GET /api/v1/reports/pack/{key}``).

The promises under test:
- PF: wage bases from the configured components (basic + DA by default),
  the statutory ceiling, the EE/EPS/ER/EDLI split — exact cents, whole
  rupees on request, every rate/ceiling/component set a parameter.
- ESI: the insurability threshold (at/below, gross only), the EE/ER shares.
- The fold agrees with the payslips': same runs, same reversal netting;
  off-cycle duplicates (policy-overridable) sum; payouts never appear;
  other periods never leak in.
- UAN/IP identifiers ride the run documents' employee entries and print
  exactly as posted — blanks stay blank, never invented.
- ``?format=csv`` serves the machine format (one header row, plain
  decimals); the default PDF greps (ADR-013's uncompressed streams).
- Guards: missing/garbage period 422, unknown parameters 422, unknown
  report key 404 naming the known ones.
- An empty month is a valid nil statement, not an error.
- Books are strangers: another book's runs never leak into a statement.

Fixture discipline: one module-scoped demo month (``demo``) seeded once on
a fresh schema; tests assert on it read-only or add their own employees,
mirroring ``test_payroll_plugin.py``.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import pytest

from ledgerforge.contrib.statutory import ESI_CSV_HEADER, PF_CSV_HEADER


# ------------------------------------------------------------------ helpers
def default_headers(client) -> dict | None:
    """Once a second book exists, requests must name one: pin to the default
    book (the books-are-strangers test creates Zeta mid-module)."""
    books = client.get("/api/v1/books").json()
    if len(books) == 1:
        return None
    default_id = next(b["id"] for b in books if b["slug"] == "default")
    return {"X-Book-Id": str(default_id)}


def _month(offset: int = 0) -> str:
    y, m = date.today().year, date.today().month + offset
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m += 12
        y += 1
    return f"{y:04d}-{m:02d}"


PERIOD = _month(0)  # the current work month
PERIOD_EMPTY = _month(-1)  # a month with no runs (nil statement)

PLACEHOLDER = [{"ledger_name": "Cash", "dc": "C", "amount": "0.01"}]


def comp(name: str, amount: str, kind: str, **extra) -> dict:
    out = {"name": name, "amount": amount, "kind": kind}
    out.update(extra)
    return out


def employee_entry(employee: str, components: list[dict], **extra) -> dict:
    out = {"employee": employee, "components": components}
    out.update(extra)
    return out


def post_run(client, period: str, employees: list[dict], headers=None, **meta_extra):
    meta = {"payroll": {"mode": "run", "period": period, "employees": employees}}
    meta.update(meta_extra)
    payload = {
        "voucher_type": "JV",
        "voucher_date": date.today().isoformat(),
        "entries": PLACEHOLDER,
        "meta": meta,
    }
    return client.post("/api/v1/vouchers", json=payload, headers=headers or default_headers(client))


def pf_csv_rows(client, query: str) -> list[list[str]]:
    r = client.get(
        f"/api/v1/reports/pack/pf?period={query}&format=csv", headers=default_headers(client)
    )
    assert r.status_code == 200, r.text
    return list(csv.reader(io.StringIO(r.text)))


def pf_csv_row_for(rows: list[list[str]], name: str) -> list[str]:
    matches = [row for row in rows[1:] if row[1] == name]
    assert len(matches) == 1, f"expected exactly one {name!r} row, got {matches}"
    return matches[0]


def esi_csv_rows(client, query: str) -> list[list[str]]:
    r = client.get(
        f"/api/v1/reports/pack/esi?period={query}&format=csv", headers=default_headers(client)
    )
    assert r.status_code == 200, r.text
    return list(csv.reader(io.StringIO(r.text)))


@pytest.fixture(scope="module")
def demo(client_module, fresh_db):
    """The module's shared demo month, seeded once: four employees covering
    every branch — the default wage base, the ceiling cap, a zero base (no
    matching components) and an above-threshold gross, with and without
    statutory identifiers."""
    client = client_module
    r = post_run(
        client,
        PERIOD,
        [
            # Basic + HRA: the default PF base counts Basic only (10,000)
            employee_entry(
                "Asha Verma",
                [
                    comp("Basic Salary", "10000.00", "earning"),
                    comp("HRA", "5000.00", "earning"),
                    comp("PF Payable", "1200.00", "withheld"),
                    comp("ESI Payable", "112.50", "withheld"),
                    comp("Employer PF", "1200.00", "employer", liability="PF Payable"),
                    comp("Employer ESI", "487.50", "employer", liability="ESI Payable"),
                ],
                uan="100000000001",
            ),
            # Basic 20,000 alone: the ceiling caps the base at 15,000
            employee_entry(
                "Rohan Iyer",
                [comp("Basic Salary", "20000.00", "earning")],
                uan="100000000002",
            ),
            # Special Allowance only: no matching wage components — zero bases
            employee_entry("Meera Nair", [comp("Special Allowance", "9000.00", "earning")]),
            # Gross above the ESI threshold: insurable = no; PF still capped
            employee_entry(
                "Kabir Shah",
                [comp("Basic Salary", "25000.00", "earning")],
                esi_ip="5100000001",
            ),
        ],
    )
    assert r.status_code == 201, r.text
    return client


# --------------------------------------------------------------------- PF
def test_pf_statement_pdf_math_and_caps(demo):
    r = demo.get(f"/api/v1/reports/pack/pf?period={PERIOD}", headers=default_headers(demo))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.headers["content-disposition"] == f'inline; filename="PF_statement_{PERIOD}.pdf"'
    data = r.content
    assert data.startswith(b"%PDF-1.4")

    # Asha: base 10,000 (Basic only; HRA is not a default wage component)
    assert b"Asha Verma" in data and b"1,200.00" in data  # EE 12%
    assert b"833.00" in data  # EPS 8.33%
    # Rohan & Kabir: the ceiling caps 20k/25k at 15,000
    assert b"15,000.00" in data
    assert b"1,249.50" in data  # EPS of 15,000
    assert b"550.50" in data  # ER = EE rate x base - EPS
    # totals: 2,450 + 3,675 + 0 + 3,675 = 9,800.00
    assert b"9,800.00" in data
    # Meera's zero base is called out, not silently dropped
    assert b"No matching wage components" in data


def test_pf_statement_csv(demo):
    rows = pf_csv_rows(demo, PERIOD)
    assert rows[0] == list(PF_CSV_HEADER)
    assert len(rows) == 5  # header + four members, sorted by name

    asha = pf_csv_row_for(rows, "Asha Verma")
    assert asha == [
        "100000000001",
        "Asha Verma",
        "15000.00",
        "10000.00",
        "10000.00",
        "10000.00",
        "1200.00",
        "833.00",
        "367.00",
        "50.00",
    ]
    rohan = pf_csv_row_for(rows, "Rohan Iyer")
    assert rohan[3] == "15000.00"  # the ceiling
    assert rohan[6:10] == ["1800.00", "1249.50", "550.50", "75.00"]
    meera = pf_csv_row_for(rows, "Meera Nair")
    assert meera[0] == ""  # no uan on the run — blank, never invented
    assert meera[6:10] == ["0.00", "0.00", "0.00", "0.00"]
    assert [r[1] for r in rows[1:]] == ["Asha Verma", "Kabir Shah", "Meera Nair", "Rohan Iyer"]

    # the response is the machine format: attachment, text/csv
    r = demo.get(
        f"/api/v1/reports/pack/pf?period={PERIOD}&format=csv", headers=default_headers(demo)
    )
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == f'attachment; filename="PF_statement_{PERIOD}.csv"'


def test_pf_parameter_overrides(demo):
    # wage= widens the base: Basic + HRA = 15,000 for Asha
    rows = pf_csv_rows(demo, f"{PERIOD}&wage=Basic Salary,HRA")
    assert pf_csv_row_for(rows, "Asha Verma")[6] == "1800.00"

    # ceiling= lowers the cap: Rohan's 20,000 base becomes 10,000
    rows = pf_csv_rows(demo, f"{PERIOD}&ceiling=10000")
    rohan = pf_csv_row_for(rows, "Rohan Iyer")
    assert rohan[3] == "10000.00"
    assert rohan[6:10] == ["1200.00", "833.00", "367.00", "50.00"]

    # round=rupee: EPS 1249.50 rounds up, ER absorbs the difference
    rows = pf_csv_rows(demo, f"{PERIOD}&round=rupee")
    assert pf_csv_row_for(rows, "Rohan Iyer")[6:10] == ["1800", "1250", "550", "75"]


def test_pf_guards(demo):
    for query, fragment in [
        ("", "period"),
        ("period=2026-13", "month"),
        ("period=2026-1", "YYYY-MM"),
        ("period=2026-10&round=centimes", "round"),
        ("period=2026-10&ceiling=abc", "ceiling"),
        ("period=2026-10&ceiling=-5", "non-negative"),
        ("period=2026-10&format=xlsx", "format"),
        ("period=2026-10&wage=,,", "wage"),
    ]:
        r = demo.get(f"/api/v1/reports/pack/pf?{query}")
        assert r.status_code == 422, (query, r.text)
        assert fragment in r.json()["detail"], (query, r.text)


# -------------------------------------------------------------------- ESI
def test_esi_statement_pdf_and_csv(demo):
    r = demo.get(f"/api/v1/reports/pack/esi?period={PERIOD}", headers=default_headers(demo))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.headers["content-disposition"] == f'inline; filename="ESI_statement_{PERIOD}.pdf"'
    data = r.content
    assert data.startswith(b"%PDF-1.4")
    assert b"112.50" in data  # Asha EE 0.75% of 15,000
    assert b"487.50" in data  # Asha ER 3.25%
    assert b"1,430.00" in data  # total ER: 487.50 + 650 + 292.50

    rows = esi_csv_rows(demo, PERIOD)
    assert rows[0] == list(ESI_CSV_HEADER)
    asha = [row for row in rows[1:] if row[1] == "Asha Verma"][0]
    assert asha == ["", "Asha Verma", "15000.00", "yes", "112.50", "487.50"]
    kabir = [row for row in rows[1:] if row[1] == "Kabir Shah"][0]
    assert kabir == ["5100000001", "Kabir Shah", "25000.00", "no", "0.00", "0.00"]


def test_esi_threshold_override(demo):
    rows = esi_csv_rows(demo, f"{PERIOD}&threshold=30000")
    kabir = [row for row in rows[1:] if row[1] == "Kabir Shah"][0]
    assert kabir == ["5100000001", "Kabir Shah", "25000.00", "yes", "187.50", "812.50"]


def test_esi_guards(demo):
    for query in [
        "",
        "period=not-a-month",
        "period=2026-10&threshold=xyz",
        "period=2026-10&format=pdfx",
    ]:
        r = demo.get(f"/api/v1/reports/pack/esi?{query}")
        assert r.status_code == 422, (query, r.text)


# ------------------------------------------------- the fold's fine print
def test_reversals_and_off_cycle_duplicates_sum(demo):
    r = post_run(
        demo, PERIOD, [employee_entry("Divya Rao", [comp("Basic Salary", "8000.00", "earning")])]
    )
    assert r.status_code == 201, r.text
    assert pf_csv_row_for(pf_csv_rows(demo, PERIOD), "Divya Rao")[3] == "8000.00"

    # an off-cycle correction (policy-overridable duplicate) sums into the month
    r = post_run(
        demo,
        PERIOD,
        [employee_entry("Divya Rao", [comp("Basic Salary", "2000.00", "earning")])],
        **{"policy:allow_duplicate_run": True},
    )
    assert r.status_code == 201, r.text
    divya = pf_csv_row_for(pf_csv_rows(demo, PERIOD), "Divya Rao")
    assert divya[2] == "10000.00"  # gross sums
    assert divya[6] == "1200.00"  # so does the base's EE share

    # a reversal nets its original out of the statement, exactly like YTD
    voucher_id = r.json()["id"]
    rev = demo.post(f"/api/v1/vouchers/{voucher_id}/reverse", headers=default_headers(demo))
    assert rev.status_code == 201, rev.text
    divya = pf_csv_row_for(pf_csv_rows(demo, PERIOD), "Divya Rao")
    assert divya[2] == "8000.00"  # only the un-reversed run remains
    assert divya[6] == "960.00"


def test_empty_period_is_a_nil_statement(demo):
    r = demo.get(f"/api/v1/reports/pack/pf?period={PERIOD_EMPTY}", headers=default_headers(demo))
    assert r.status_code == 200
    assert b"No payroll runs posted" in r.content

    assert pf_csv_rows(demo, PERIOD_EMPTY) == [list(PF_CSV_HEADER)]  # header only

    r = demo.get(f"/api/v1/reports/pack/esi?period={PERIOD_EMPTY}", headers=default_headers(demo))
    assert r.status_code == 200
    assert b"nil statement" in r.content


def test_payouts_never_accrue_wages(demo):
    payload = {
        "voucher_type": "PYT",
        "voucher_date": date.today().isoformat(),
        "entries": PLACEHOLDER,
        "meta": {
            "payroll": {
                "mode": "payout",
                "employee": "Asha Verma",
                "amount": "13687.50",
                "bank": "HDFC Bank",
            }
        },
    }
    r = demo.post("/api/v1/vouchers", json=payload, headers=default_headers(demo))
    assert r.status_code == 201, r.text
    # the payout settles a balance; Asha's wage row is unchanged
    assert pf_csv_row_for(pf_csv_rows(demo, PERIOD), "Asha Verma")[2] == "15000.00"


def test_unknown_report_key_names_the_known_ones(demo):
    r = demo.get("/api/v1/reports/pack/gst")
    assert r.status_code == 404, r.text
    detail = r.json()["detail"]
    assert "gst" in detail and "pf" in detail and "esi" in detail


def test_books_are_strangers(demo):
    r = demo.post("/api/v1/books", json={"name": "Zeta Ventures", "base_currency": "INR"})
    assert r.status_code == 201, r.text
    zeta_id = r.json()["book"]["id"]

    # the same employee name in another book, with different wages
    r = post_run(
        demo,
        PERIOD,
        [employee_entry("Asha Verma", [comp("Basic Salary", "9999.00", "earning")])],
        headers={"X-Book-Id": str(zeta_id)},
    )
    assert r.status_code == 201, r.text

    # the default book, pinned via header — Zeta's 9,999 never leaks
    asha = pf_csv_row_for(pf_csv_rows(demo, PERIOD), "Asha Verma")
    assert asha[3] == "10000.00"
