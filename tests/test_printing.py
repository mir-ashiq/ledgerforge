"""The print bridge (ADR-013): any voucher as a PDF, plugin document views
first (payslip, tax invoice), the voucher slip as the universal fallback.

The promises under test:
- The writer emits structurally valid PDF 1.4 (qpdf-clean xref, base-14
  fonts, WinAnsi degradation for unmappable glyphs, escaped text runs).
- ``GET /vouchers/{id}/pdf`` serves ``application/pdf`` with a sensible
  filename; content streams carry the document's strings (uncompressed —
  greppable bytes).
- A payroll run prints as the **payslip** of ``?employee=``: components
  with YTD, gross/withheld/net, and the payable position replayed through
  the pack's own ``payable()`` fold — a payslip that disagrees with the
  ledger is impossible by construction. Two employees need the param;
  the sole employee of a single run is the default; strangers are 422s.
- A sales invoice prints as the **tax invoice**: items, GST slabs, the
  computed totals.
- Everything unclaimed — plain vouchers, mystery meta keys, payouts,
  reversals — prints as the voucher slip (Dr/Cr, totals, Law 2 note).
- Books are strangers: another book's id is a 404.
- Long vouchers paginate; rows never split.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from conftest import TODAY, create_ledger, post_voucher

from ledgerforge.plugins import DocumentColumn, DocumentSection, DocumentView
from ledgerforge.reporting.printing import PdfWriter, format_amount, render_view

PAGE_RE = re.compile(rb"/Type /Page\b")


# ------------------------------------------------------------------ helpers
def ensure_ledgers(client, *name_group: tuple[str, str]) -> None:
    """Create masters the seed doesn't ship (idempotent: module-scoped
    fresh_db is shared by every test in this file)."""
    existing = {row["name"] for row in client.get("/api/v1/ledgers").json()}
    for name, group in name_group:
        if name not in existing:
            create_ledger(client, name, group)


def _month(offset: int = 0) -> str:
    y, m = date.today().year, date.today().month + offset
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


PERIOD = _month(0)
PERIOD_PREV = _month(-1)

PLACEHOLDER = [{"ledger_name": "Cash", "dc": "C", "amount": "0.01"}]


def comp(name: str, amount: str, kind: str, **extra) -> dict:
    return {"name": name, "amount": amount, "kind": kind, **extra}


def post_payroll_run(client, period: str, employees: list[dict]):
    return post_voucher(
        client,
        type_="JV",
        entries=PLACEHOLDER,
        meta={
            "payroll": {"mode": "run", "period": period, "employees": employees},
        },
    )


def post_payout(client, employee: str, amount: str, bank: str = "HDFC Bank"):
    return post_voucher(
        client,
        type_="PYT",
        entries=PLACEHOLDER,
        meta={
            "payroll": {
                "mode": "payout",
                "employee": employee,
                "amount": amount,
                "bank": bank,
            }
        },
    )


def get_pdf(client, voucher_id: int, query: str = ""):
    return client.get(f"/api/v1/vouchers/{voucher_id}/pdf{query}")


def page_count(data: bytes) -> int:
    return len(PAGE_RE.findall(data))


# ------------------------------------------------------------- writer units
def test_writer_escapes_and_degrades_unmappable_glyphs():
    writer = PdfWriter()
    sheet = writer.add_page()
    sheet.text(48.0, 60.0, "A(m\\)ps ₹")
    data = writer.build()
    # parens and backslash escaped, the rupee sign degrades to '?'
    assert rb"(A\(m\\\)ps ?) Tj" in data


def test_writer_xref_offsets_point_at_objects():
    writer = PdfWriter(title="x")
    writer.add_page()
    writer.add_page()
    data = writer.build()

    start = data.rfind(b"startxref\n")
    xref_at = int(data[start + len(b"startxref\n") :].split(b"\n", 1)[0])
    assert data[xref_at : xref_at + 4] == b"xref"
    # entries begin after the free head; every offset lands on "N 0 obj"
    table = data[xref_at:].split(b"\n")
    count = int(table[1].split()[1])
    assert count == 9  # 1 catalog + 2 pages tree + 2 fonts + 2×(page, content)
    for number in range(1, count):
        entry = table[2 + number].split()
        offset = int(entry[0])
        assert data[offset:].startswith(f"{number} 0 obj".encode())


def test_format_amount_money_display():
    assert format_amount(Decimal("68200")) == "68,200.00"
    assert format_amount(Decimal("1180.5")) == "1,180.50"
    assert format_amount(Decimal("-0.005")) == "0.00"  # never -0.00
    assert format_amount(Decimal("-1234.56")) == "-1,234.56"


def test_render_view_layout_basics():
    view = DocumentView(
        title="Payslip",
        subtitle="Salary - Acme",
        reference="JV-1",
        fields=(("Employee", "Asha Verma"),),
        sections=(
            DocumentSection(
                title="Earnings",
                columns=(
                    DocumentColumn("Component", 2.6),
                    DocumentColumn("Amount", 1.0, "right"),
                ),
                rows=(("Basic Salary", "50,000.00"),),
            ),
        ),
        totals=(("Net pay", "68,200.00"),),
        footnotes=("one note",),
    )
    data = render_view(view)
    assert data.startswith(b"%PDF-1.4")
    assert b"(Payslip) Tj" in data
    assert b"(Asha Verma) Tj" in data
    assert b"(68,200.00) Tj" in data
    assert page_count(data) == 1


# ------------------------------------------------------------ voucher slips
def test_plain_voucher_prints_as_voucher_slip(client, fresh_db):
    ensure_ledgers(client, ("Local Sales", "Sales Accounts"), ("SGST Payable", "Duties & Taxes"))
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "1180.00"},
            {"ledger_name": "Local Sales", "dc": "C", "amount": "1000.00"},
            {"ledger_name": "SGST Payable", "dc": "C", "amount": "180.00"},
        ],
    )
    assert r.status_code == 201, r.text
    voucher_id = r.json()["id"]

    pdf = get_pdf(client, voucher_id)
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"].startswith('inline; filename="SAL-')
    data = pdf.content
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    assert b"(Voucher) Tj" in data
    assert b"(Local Sales) Tj" in data
    assert b"1,180.00" in data and b"1,000.00" in data
    assert b"(Total debits) Tj" in data
    assert page_count(data) == 1


def test_unknown_meta_key_falls_back_to_the_slip(client, fresh_db):
    ensure_ledgers(client, ("Local Sales", "Sales Accounts"))
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "10.00"},
            {"ledger_name": "Local Sales", "dc": "C", "amount": "10.00"},
        ],
        meta={"mystery": {"anything": True}},
    )
    assert r.status_code == 201, r.text
    pdf = get_pdf(client, r.json()["id"])
    assert pdf.status_code == 200
    # claimed by no factory → the slip, with the document named in the header
    assert b"(Voucher) Tj" in pdf.content
    assert b"mystery" in pdf.content


def test_long_voucher_paginates_without_splitting_rows(client, fresh_db):
    ensure_ledgers(client, ("Local Sales", "Sales Accounts"))
    entries = []
    for i in range(1, 81):
        entries.append(
            {"ledger_name": "Cash", "dc": "D", "amount": "1.00", "narration": f"line {i}"}
        )
        entries.append({"ledger_name": "Local Sales", "dc": "C", "amount": "1.00"})
    r = post_voucher(client, entries=entries)
    assert r.status_code == 201, r.text
    pdf = get_pdf(client, r.json()["id"])
    assert pdf.status_code == 200
    assert page_count(pdf.content) >= 2
    # the entries table header repeats on every page
    assert pdf.content.count(b"(Ledger account)") == page_count(pdf.content)


# ---------------------------------------------------------------- payslips
ASHA = "Asha Verma"
ROHAN = "Rohan Iyer"


def asha_components() -> list[dict]:
    return [
        comp("Basic Salary", "50000.00", "earning"),
        comp("HRA", "20000.00", "earning"),
        comp("PF Payable", "1800.00", "withheld"),
        comp("Employer PF", "1800.00", "employer", liability="PF Payable"),
    ]


def test_payslip_pdf(client, fresh_db):
    r = post_payroll_run(
        client,
        PERIOD,
        [
            {"employee": ASHA, "components": asha_components()},
            {"employee": ROHAN, "components": [comp("Basic Salary", "30000.00", "earning")]},
        ],
    )
    assert r.status_code == 201, r.text
    voucher = r.json()

    pdf = get_pdf(client, voucher["id"], query=f"?employee={ASHA}")
    assert pdf.status_code == 200, pdf.text
    assert pdf.headers["content-type"] == "application/pdf"
    disposition = pdf.headers["content-disposition"]
    assert disposition.startswith('inline; filename="Payslip_')
    assert ASHA.replace(" ", "_") in disposition

    data = pdf.content
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    assert b"(Payslip) Tj" in data
    assert f"({ASHA}) Tj".encode() in data
    # the components and their YTD (first run of the fiscal year)
    assert b"(Basic Salary) Tj" in data
    assert b"(50,000.00) Tj" in data
    assert b"(20,000.00) Tj" in data
    # totals: gross, withheld, net — the README's example figures
    assert b"(Gross earnings) Tj" in data and b"70,000.00" in data
    assert b"(Total deductions) Tj" in data and b"1,800.00" in data
    assert b"(Net pay) Tj" in data and b"68,200.00" in data
    # the other employee's figures are NOT on Asha's payslip
    assert b"30,000.00" not in data
    assert page_count(data) == 1


def test_payslip_needs_the_employee_param_for_multi_employee_runs(client, fresh_db):
    r = post_payroll_run(
        client,
        PERIOD,
        [
            {"employee": "Meera Nair", "components": [comp("Basic Salary", "10000.00", "earning")]},
            {"employee": "Vikram Rao", "components": [comp("Basic Salary", "12000.00", "earning")]},
        ],
    )
    assert r.status_code == 201, r.text
    pdf = get_pdf(client, r.json()["id"])
    assert pdf.status_code == 422
    detail = pdf.json()["detail"]
    assert "employee" in detail and "Meera Nair" in detail and "Vikram Rao" in detail


def test_payslip_rejects_an_employee_outside_the_run(client, fresh_db):
    r = post_payroll_run(
        client,
        PERIOD,
        [{"employee": "Kiran Shah", "components": [comp("Basic", "1000.00", "earning")]}],
    )
    assert r.status_code == 201, r.text
    pdf = get_pdf(client, r.json()["id"], query="?employee=Ghost")
    assert pdf.status_code == 422
    assert "Kiran Shah" in pdf.json()["detail"]


def test_single_employee_run_defaults_to_its_only_employee(client, fresh_db):
    r = post_payroll_run(
        client,
        PERIOD,
        [{"employee": "Nisha Gupta", "components": [comp("Basic", "9000.00", "earning")]}],
    )
    assert r.status_code == 201, r.text
    pdf = get_pdf(client, r.json()["id"])
    assert pdf.status_code == 200
    assert b"(Payslip) Tj" in pdf.content
    assert b"(Nisha Gupta) Tj" in pdf.content


def test_payslip_payable_reflects_the_all_time_fold(client, fresh_db):
    # one run last month, a partial payout, then the payslip of that run:
    # payable must show accrued - paid = due, from the pack's own fold
    r = post_payroll_run(
        client,
        PERIOD_PREV,
        [
            {
                "employee": "Dev Patel",
                "components": [
                    comp("Basic Salary", "30000.00", "earning"),
                    comp("Professional Tax", "500.00", "withheld"),
                ],
            }
        ],
    )
    assert r.status_code == 201, r.text
    payout = post_payout(client, "Dev Patel", "10000.00")
    assert payout.status_code == 201, payout.text

    pdf = get_pdf(client, r.json()["id"], query="?employee=Dev Patel")
    assert pdf.status_code == 200
    data = pdf.content
    assert b"29,500.00" in data  # net of the run
    assert b"due 19,500.00" in data  # 29,500 accrued - 10,000 paid


def test_payout_prints_as_voucher_slip_not_payslip(client, fresh_db):
    r = post_payroll_run(
        client,
        PERIOD,
        [{"employee": "Sona Ali", "components": [comp("Basic", "8000.00", "earning")]}],
    )
    assert r.status_code == 201, r.text
    payout = post_payout(client, "Sona Ali", "8000.00")
    assert payout.status_code == 201, payout.text

    pdf = get_pdf(client, payout.json()["id"])
    assert pdf.status_code == 200
    assert b"(Voucher) Tj" in pdf.content  # payouts decline the payslip view
    assert b"Payslip" not in pdf.content
    assert b"HDFC Bank" in pdf.content


def test_reversal_prints_as_voucher_slip_and_original_stays_a_payslip(client, fresh_db):
    r = post_payroll_run(
        client,
        PERIOD,
        [{"employee": "Tara Menon", "components": [comp("Basic Salary", "7000.00", "earning")]}],
    )
    assert r.status_code == 201, r.text
    voucher_id = r.json()["id"]

    reversal = client.post(f"/api/v1/vouchers/{voucher_id}/reverse")
    assert reversal.status_code == 201, reversal.text
    reversal_id = reversal.json()["id"]

    # the reversal (empty meta) is the voucher slip, pointing at its original
    pdf = get_pdf(client, reversal_id)
    assert pdf.status_code == 200
    assert b"(Voucher) Tj" in pdf.content
    assert b"(Reversal of) Tj" in pdf.content

    # the original keeps printing as the payslip
    original = get_pdf(client, voucher_id, query="?employee=Tara Menon")
    assert original.status_code == 200
    assert b"(Payslip) Tj" in original.content


# ------------------------------------------------------------ tax invoices
def test_tax_invoice_pdf(client, fresh_db):
    r = post_voucher(
        client,
        type_="SAL",
        entries=PLACEHOLDER,
        meta={
            "invoice": {
                "customer": "Acme Traders",
                "items": [
                    {"description": "Widgets", "qty": "10", "rate": "100.00", "tax_rate": "18"},
                    {"description": "Installation", "qty": "1", "rate": "250.00", "tax_rate": "0"},
                ],
                "due_days": 30,
            }
        },
    )
    assert r.status_code == 201, r.text
    voucher = r.json()

    pdf = get_pdf(client, voucher["id"])
    assert pdf.status_code == 200
    assert pdf.headers["content-disposition"].startswith('inline; filename="Invoice_')
    data = pdf.content
    assert b"(Tax Invoice) Tj" in data
    assert b"(Acme Traders) Tj" in data
    assert b"(Widgets) Tj" in data
    assert b"(Installation) Tj" in data
    assert b"1,000.00" in data  # 10 × 100
    assert b"250.00" in data
    assert b"(Subtotal) Tj" in data and b"1,250.00" in data
    assert b"180.00" in data  # 18% of 1000
    assert b"(Grand total) Tj" in data and b"1,430.00" in data
    # the computed due date rides along (voucher date + 30 days)
    due = (date.fromisoformat(TODAY) + timedelta(days=30)).isoformat()
    assert due.encode() in data


# ------------------------------------------------------------ book barriers
def test_books_are_strangers_on_print_too(client, fresh_db):
    ensure_ledgers(client, ("Local Sales", "Sales Accounts"))
    r = post_voucher(
        client,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "5.00"},
            {"ledger_name": "Local Sales", "dc": "C", "amount": "5.00"},
        ],
    )
    assert r.status_code == 201, r.text
    voucher_id = r.json()["id"]

    other = client.post("/api/v1/books", json={"name": "Zeta Print Co", "base_currency": "INR"})
    assert other.status_code == 201, other.text
    other_id = other.json()["book"]["id"]

    # the voucher does not exist in the other book
    pdf = get_pdf(client, voucher_id)
    assert pdf.status_code == 400  # several books now: the header is required
    denied = client.get(f"/api/v1/vouchers/{voucher_id}/pdf", headers={"X-Book-Id": str(other_id)})
    assert denied.status_code == 404

    books = client.get("/api/v1/books").json()
    default_id = next(b["id"] for b in books if b["slug"] == "default")
    allowed = client.get(
        f"/api/v1/vouchers/{voucher_id}/pdf", headers={"X-Book-Id": str(default_id)}
    )
    assert allowed.status_code == 200
    assert b"(Voucher) Tj" in allowed.content
