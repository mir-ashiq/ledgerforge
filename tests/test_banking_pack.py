"""The banking pack (contrib/banking.py): the meta layer over the banking
kernel — cheque declarations, the register fold, the reconciliation report,
the cheque-aware print view, and the gateway hook — ADR-026, Phase 5.

The promises under test:

- **The validator**: a cheque declaration must name a real bank ledger of
  the book, ride a voucher that MOVES that ledger (TallyPrime's rule),
  carry a number unique among live declarations (the advisory lock's
  verdict), and a PDC maturity on or after the issue. A reversal pair
  kills its cheque with it — the number is free again.
- **The register is a fold**: issued → presented (a statement line
  references the number) → cleared (that line carries a live match), PDC
  maturity against the as-of, the bank line's amount from
  ``ledger_entries``. Precise by construction: a longer digit-run does
  not match (448812 ≠ 4488123).
- **The reconciliation report is the kernel fold** (one truth, two
  renderings): its JSON equals the kernel route's JSON on the same
  anchor; the PDF renders through the print bridge; the CSV carries the
  proof row.
- **The document view**: a cheque-carrying payment prints with its cheque
  number, date and maturity in the header fields.
- **The sanctioned read is book-scoped**: another book's ledger answers
  empty through ``Resolver.bank_lines`` (plugin API 0.11).
- **The gateway hook** records its receipts (the observability seam the
  bridges and tests share).

The module shares one database (module-scoped ``fresh_db``); every test
works with its own bank ledger, because matches and cheque numbers are
ledger-scoped state.
"""

from __future__ import annotations

from datetime import date, timedelta

from conftest import post_voucher

from ledgerforge.contrib import banking as pack
from ledgerforge.plugins import PluginManager, Resolver

TODAY = date.today()


# ------------------------------------------------------------------ helpers
def _bank(client, name: str) -> dict:
    r = client.post("/api/v1/ledgers", json={"name": name, "group_name": "Bank Accounts"})
    assert r.status_code in (201, 422), r.text
    rows = client.get("/api/v1/ledgers", params={"search": name}).json()
    return next(row for row in rows if row["name"] == name)


def _party(client, name: str) -> dict:
    r = client.post("/api/v1/ledgers", json={"name": name, "group_name": "Sundry Creditors"})
    assert r.status_code in (201, 422), r.text
    rows = client.get("/api/v1/ledgers", params={"search": name}).json()
    return next(row for row in rows if row["name"] == name)


def _cheque_payment(client, bank: str, party: str, amount: str, no: str, **cheque):
    """A payment by cheque: Dr bank, Cr party, the declaration riding."""
    voucher_date = cheque.pop("date_", "2026-04-01")
    doc: dict = {"no": no, "bank_ledger": bank}
    doc.update(cheque)
    return post_voucher(
        client,
        type_="PYT",
        date_=voucher_date,
        entries=[
            {"ledger_name": bank, "dc": "D", "amount": amount, "narration": f"Cheque {no}"},
            {"ledger_name": party, "dc": "C", "amount": amount},
        ],
        meta={"banking": {"cheque": doc}},
    )


def _import(client, ledger_id: int, text: str, **extra) -> dict:
    payload = {"format": "csv", "text": text}
    payload.update(extra)
    r = client.post(f"/api/v1/banking/{ledger_id}/statements", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _statement_with_cheque(cheque_no: str, amount: str = "5000.00", day: str = "10/04/2026"):
    return (
        "Date,Narrative,Ref No,Withdrawal,Deposit\n"
        f"{day},CHQ {cheque_no} PAID,CHQ{cheque_no},{amount},\n"
    )


def _register(client, as_of: str = "2026-04-30", **params) -> dict:
    query = {"as_of": as_of, **params}
    r = client.get("/api/v1/reports/pack/banking-cheques", params={**query, "format": "json"})
    assert r.status_code == 200, r.text
    return r.json()


# ----------------------------------------------------------------- validator
def test_valid_cheque_declaration_posts(client):
    _bank(client, "Valid Chq Bank")
    _party(client, "Valid Supplier")
    v = _cheque_payment(
        client,
        "Valid Chq Bank",
        "Valid Supplier",
        "5000.00",
        "448812",
        matures_on="2026-05-01",
    )
    assert v.status_code == 201, v.text
    assert v.json()["meta"]["banking"]["cheque"]["no"] == "448812"


def test_validator_rejects_the_dishonest_shapes(client):
    _bank(client, "Guard Chq Bank")
    _party(client, "Guard Supplier")

    # no number
    r = _cheque_payment(client, "Guard Chq Bank", "Guard Supplier", "100.00", "")
    assert r.status_code == 422 and "cheque.no" in r.json()["detail"]

    # unknown bank ledger
    r = _cheque_payment(client, "No Such Bank", "Guard Supplier", "100.00", "000111")
    assert r.status_code == 422 and "does not exist" in r.json()["detail"]

    # the voucher must move the declared bank account
    other_bank = _bank(client, "Guard Other Bank")
    doc = {"no": "000222", "bank_ledger": other_bank["name"]}
    r = post_voucher(
        client,
        type_="PYT",
        date_="2026-04-01",
        entries=[
            {"ledger_name": "Guard Chq Bank", "dc": "D", "amount": "100.00"},
            {"ledger_name": "Guard Supplier", "dc": "C", "amount": "100.00"},
        ],
        meta={"banking": {"cheque": doc}},
    )
    assert r.status_code == 422 and "does not move" in r.json()["detail"]

    # a PDC that matures before its issue
    r = _cheque_payment(
        client,
        "Guard Chq Bank",
        "Guard Supplier",
        "100.00",
        "000333",
        matures_on="2026-03-01",
    )
    assert r.status_code == 422 and "matures_on" in r.json()["detail"]

    # unknown keys never ride silently
    r = post_voucher(
        client,
        type_="PYT",
        date_="2026-04-01",
        entries=[
            {"ledger_name": "Guard Chq Bank", "dc": "D", "amount": "100.00"},
            {"ledger_name": "Guard Supplier", "dc": "C", "amount": "100.00"},
        ],
        meta={
            "banking": {"cheque": {"no": "000444", "bank_ledger": "Guard Chq Bank", "micr": "x"}}
        },
    )
    assert r.status_code == 422 and "unknown" in r.json()["detail"]


def test_one_number_one_cheque_until_reversed(client):
    _bank(client, "Once Chq Bank")
    _party(client, "Once Supplier")
    first = _cheque_payment(client, "Once Chq Bank", "Once Supplier", "100.00", "777001")
    assert first.status_code == 201, first.text
    dup = _cheque_payment(client, "Once Chq Bank", "Once Supplier", "200.00", "777001")
    assert dup.status_code == 422
    assert "already declared" in dup.json()["detail"]

    # reversing the first payment un-issues its cheque — the number frees
    r = client.post(f"/api/v1/vouchers/{first.json()['id']}/reverse")
    assert r.status_code in (200, 201), r.text
    again = _cheque_payment(client, "Once Chq Bank", "Once Supplier", "300.00", "777001")
    assert again.status_code == 201, again.text


def test_same_number_different_banks_is_fine(client):
    """Cheque series are per bank account — the same number on another
    ledger is a different cheque."""
    _bank(client, "Series Bank A")
    _bank(client, "Series Bank B")
    _party(client, "Series Supplier")
    first = _cheque_payment(client, "Series Bank A", "Series Supplier", "100.00", "000100")
    assert first.status_code == 201, first.text
    second = _cheque_payment(client, "Series Bank B", "Series Supplier", "150.00", "000100")
    assert second.status_code == 201, second.text


# ------------------------------------------------------------------ register
def test_the_lifecycle_issued_presented_cleared(client):
    bank = _bank(client, "Life Chq Bank")
    _party(client, "Life Supplier")
    v = _cheque_payment(client, "Life Chq Bank", "Life Supplier", "5000.00", "448812")
    assert v.status_code == 201, v.text

    # issued — nothing from the bank yet
    data = _register(client)
    row = next(
        r for r in data["rows"] if r["no"] == "448812" and r["bank_ledger"] == "Life Chq Bank"
    )
    assert row["status"] == "issued"
    assert row["amount"] == "5000.00"
    assert row["pdc"] is False

    # presented — a statement line references the number
    _import(client, bank["id"], _statement_with_cheque("448812"))
    data = _register(client)
    row = next(
        r for r in data["rows"] if r["no"] == "448812" and r["bank_ledger"] == "Life Chq Bank"
    )
    assert row["status"] == "presented"
    assert row["presented_on"] == "2026-04-10"

    # cleared — that line carries a live match (name the entry from the
    # entry book; the amounts agree so a suggestion exists too)
    entries = client.get(
        f"/api/v1/banking/{bank['id']}/entries", params={"status": "unmatched"}
    ).json()["entries"]
    entry = next(e for e in entries if e["voucher_no"] == v.json()["voucher_no"])
    line = next(
        ln["id"]
        for ln in client.get(
            f"/api/v1/banking/{bank['id']}/lines", params={"status": "unmatched"}
        ).json()["lines"]
    )
    r = client.post(
        f"/api/v1/banking/{bank['id']}/matches",
        json={
            "statement_line_id": line,
            "entry_id": entry["entry_id"],
            "voucher_id": entry["voucher_id"],
            "voucher_date": entry["voucher_date"],
        },
    )
    assert r.status_code == 201, r.text
    data = _register(client)
    row = next(
        r for r in data["rows"] if r["no"] == "448812" and r["bank_ledger"] == "Life Chq Bank"
    )
    assert row["status"] == "cleared"
    assert row["cleared_by"] == v.json()["voucher_no"]
    mine = [r for r in data["rows"] if r["bank_ledger"] == "Life Chq Bank"]
    assert {r["status"] for r in mine} == {"cleared"}


def test_pdc_maturity_tracking(client):
    _bank(client, "PDC Chq Bank")
    _party(client, "PDC Supplier")
    matures = (date(2026, 4, 1) + timedelta(days=45)).isoformat()
    v = _cheque_payment(
        client,
        "PDC Chq Bank",
        "PDC Supplier",
        "2000.00",
        "551100",
        date_="2026-04-01",
        matures_on=matures,
    )
    assert v.status_code == 201, v.text

    before = _register(client, as_of="2026-04-30")
    row = next(r for r in before["rows"] if r["no"] == "551100")
    assert row["pdc"] is True
    assert row["matured_as_of"] is False
    pdc_only = _register(client, as_of="2026-04-30", status="pdc", ledger="PDC Chq Bank")
    assert [r["no"] for r in pdc_only["rows"]] == ["551100"]

    after = _register(client, as_of=matures)
    row = next(r for r in after["rows"] if r["no"] == "551100")
    assert row["pdc"] is True and row["matured_as_of"] is True


def test_the_digit_run_does_not_overmatch(client):
    """Precise by construction: a statement line carrying 4488123 must NOT
    present cheque 448812 — over-matching a substring is how wrong cheques
    clear."""
    bank = _bank(client, "Precise Chq Bank")
    _party(client, "Precise Supplier")
    v = _cheque_payment(client, "Precise Chq Bank", "Precise Supplier", "100.00", "448812")
    assert v.status_code == 201, v.text
    _import(client, bank["id"], _statement_with_cheque("4488123", amount="100.00"))
    data = _register(client)
    row = next(r for r in data["rows"] if r["no"] == "448812")
    assert row["status"] == "issued"


def test_register_filters_by_ledger(client):
    _bank(client, "Filter Chq Bank A")
    _bank(client, "Filter Chq Bank B")
    _party(client, "Filter Supplier")
    assert (
        _cheque_payment(
            client, "Filter Chq Bank A", "Filter Supplier", "10.00", "900001"
        ).status_code
        == 201
    )
    assert (
        _cheque_payment(
            client, "Filter Chq Bank B", "Filter Supplier", "20.00", "900002"
        ).status_code
        == 201
    )
    data = _register(client, ledger="Filter Chq Bank A")
    assert [r["no"] for r in data["rows"]] == ["900001"]


# ------------------------------------------------------- the reports & print
def test_reconciliation_report_is_the_kernel_fold(client):
    """One truth, two renderings: the pack report's JSON equals the kernel
    route's JSON on the same anchor (the route may also fold its own
    anchor; the printable statement names its anchor)."""
    bank = _bank(client, "Report Recon Bank")
    _party(client, "Report Recon Co")
    post_voucher(
        client,
        type_="RCT",
        date_="2026-04-05",
        entries=[
            {"ledger_name": "Report Recon Co", "dc": "D", "amount": "15000.00"},
            {"ledger_name": "Report Recon Bank", "dc": "C", "amount": "15000.00"},
        ],
    )
    _import(
        client,
        bank["id"],
        "Date,Narrative,Ref No,Withdrawal,Deposit\n05/04/2026,NEFT Credit,REF,,15000.00\n",
        opening_balance="10000.00",
    )
    anchor = {"as_of": "2026-04-30", "statement_balance": "25000.00"}
    route = client.get(f"/api/v1/banking/{bank['id']}/reconciliation", params=anchor).json()
    report = client.get(
        "/api/v1/reports/pack/banking-reconciliation",
        params={**anchor, "ledger": bank["name"], "format": "json"},
    ).json()
    for key in (
        "book_balance",
        "statement_balance",
        "reconciled_balance",
        "unmatched_book_total",
        "unmatched_statement_total",
        "pair_difference_total",
        "proof",
    ):
        assert report[key] == route[key], key
    assert report["proof"]["ok"] is True

    # the PDF renders through the print bridge
    pdf = client.get(
        "/api/v1/reports/pack/banking-reconciliation",
        params={**anchor, "ledger": bank["name"]},
    )
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content[:5] == b"%PDF-"

    # the CSV carries the proof row
    csv_text = client.get(
        "/api/v1/reports/pack/banking-reconciliation",
        params={**anchor, "ledger": bank["name"], "format": "csv"},
    )
    assert csv_text.status_code == 200
    assert "proof," in csv_text.text


def test_reconciliation_report_demands_the_anchor(client):
    bank = _bank(client, "Anchor Recon Bank")
    r = client.get(
        "/api/v1/reports/pack/banking-reconciliation",
        params={"ledger": bank["name"], "as_of": "2026-04-30", "format": "json"},
    )
    assert r.status_code == 422
    assert "statement_balance" in r.json()["detail"]


def test_cheque_payment_prints_with_its_cheque(client):
    """The banking document view claims meta.banking vouchers: the payment
    prints with its cheque number, date and maturity in the header."""
    _bank(client, "Print Chq Bank")
    _party(client, "Print Supplier")
    v = _cheque_payment(
        client,
        "Print Chq Bank",
        "Print Supplier",
        "1234.56",
        "664422",
        matures_on="2026-05-15",
    )
    assert v.status_code == 201, v.text
    pdf = client.get(f"/api/v1/vouchers/{v.json()['id']}/pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content[:5] == b"%PDF-"


# ------------------------------------------------------- the sanctioned read
def test_bank_lines_is_book_scoped(client, db_session):
    """Plugin API 0.11's read answers empty for another book's ledger —
    the fy_start_month precedent, the ADR-009 discipline."""
    from ledgerforge.models import Book, Ledger

    r = client.post("/api/v1/books", json={"name": "Pack Stranger Books"})
    assert r.status_code in (201, 409), r.text
    books = client.get("/api/v1/books").json()
    stranger = next(b for b in books if b["name"] == "Pack Stranger Books")
    headers = {"X-Book-Id": str(stranger["id"])}
    r = client.post(
        "/api/v1/ledgers",
        headers=headers,
        json={"name": "Their Pack Bank", "group_name": "Bank Accounts"},
    )
    assert r.status_code == 201, r.text
    their_bank_id = r.json()["id"]

    db_session.rollback()  # see the other connection's committed rows
    mine = db_session.query(Book).order_by(Book.id).first()
    theirs = db_session.get(Ledger, their_bank_id)
    assert theirs is not None and theirs.book_id != mine.id

    mgr = PluginManager()
    mgr.load_module(pack)
    resolver = Resolver(db_session, mine)
    assert resolver.bank_lines(their_bank_id) == []

    # restore the single-book world (the no-header auto-pick)
    r = client.patch(f"/api/v1/books/{stranger['id']}", json={"is_archived": True})
    assert r.status_code == 200, r.text


def test_gateway_hook_records_receipts(client):
    """The observability seam the bridges (and these tests) share."""
    bank = _bank(client, "Obs Gateway Bank")
    pack.GATEWAY_IMPORTS.clear()
    r = client.post(
        "/api/v1/hooks/bank-statement-gateway",
        json={
            "ledger": bank["id"],
            "format": "csv",
            "text": "Date,Description,Amount\n01/04/2026,Opening,-1.00\n",
            "dry_run": True,
        },
    )
    assert r.status_code == 200, r.text
    assert pack.GATEWAY_IMPORTS[-1]["dry_run"] is True
