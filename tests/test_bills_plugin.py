"""The bills pack (contrib/bills.py): bill-wise & outstanding management —
ADR-023, Phase 2 of the v1.0 roadmap.

The promises under test:

- **Allocations ride vouchers that move a party ledger** — plain vouchers
  and document-mode (meta.invoice / meta.receipt) alike — and refs are
  dr/cr allocations exactly like ledger lines (TallyPrime's bill-wise
  columns): ``new`` opens, ``against`` settles toward zero, ``on_account``
  pools.
- **The conservation law**: Σ ref dr − Σ ref cr == the party's net movement
  on the voucher, enforced after all transformers; and its corollary —
  Σ open bills + on-account == the party's trial-balance figure, the master
  theorem, asserted here against the real trial balance.
- Settlement guards: unopened bills, over-settlement (the excess belongs
  in on_account), re-settling closed bills, settling away from zero,
  duplicate open refs, unknown parties, vouchers that don't move the party,
  payroll conflicts, empty documents — all 422/404.
- Reports: bills receivable/payable with ageing buckets (not due / 0-30 /
  31-60 / 61-90 / 90+), the bill-linked ledger statement with opening and
  closing balances, interest on overdue bills with the documented rate
  resolution, and the party-wise open-bills feed — PDF (print bridge), CSV
  and JSON (plugin API 0.8).
- The **interest_charge** document mode: derives Dr party / Cr Interest
  Income from the overdue fold, auto-opens a bill for its total (the master
  theorem demands it), and refuses ambiguity (other document keys, explicit
  refs, as_of ≠ voucher date, nothing overdue, repeat refs).
- Reversals net in the fold (undo an invoice → the bill re-opens; undo a
  receipt → the settlement restores); books are strangers; idempotent
  replay allocates nothing twice.

Like the invoicing suite, the module shares one database (module-scoped
``fresh_db``), so every test works with its own party — a book's bill refs
are namespaced by party, and distinct parties keep the tests independent.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from conftest import post_voucher

from ledgerforge.contrib import bills as bills_pack

TODAY = date.today()


# ------------------------------------------------------------------ helpers
def _ensure_ledgers(client, *specs: tuple[str, str], headers: dict | None = None) -> None:
    """Create ledgers, tolerating the ones an earlier test already made
    (the module shares one book)."""
    for name, group in specs:
        r = client.post(
            "/api/v1/ledgers", headers=headers, json={"name": name, "group_name": group}
        )
        assert r.status_code in (201, 422), (name, r.text)


def _bill_meta(party: str, refs: list[dict], **extra) -> dict:
    doc: dict = {"party": party, "refs": refs}
    doc.update(extra)
    return {"bills": doc}


def _new(ref: str, amount: str, *, due_days: int | None = None, cr: bool = False, **extra) -> dict:
    entry: dict = {"kind": "new", "ref": ref, "cr" if cr else "dr": amount}
    if due_days is not None:
        entry["due_days"] = due_days
    entry.update(extra)
    return entry


def _against(ref: str, amount: str, *, cr: bool = False) -> dict:
    return {"kind": "against", "ref": ref, "cr" if cr else "dr": amount}


def _on_account(amount: str, *, cr: bool = False) -> dict:
    return {"kind": "on_account", "cr" if cr else "dr": amount}


def _party_lines(party: str, dr: str | None = None, cr: str | None = None) -> list[dict]:
    lines = []
    if dr:
        lines.append({"ledger_name": party, "dc": "D", "amount": dr})
    if cr:
        lines.append({"ledger_name": party, "dc": "C", "amount": cr})
    return lines


def _trial_balance_of(client, ledger_name: str) -> Decimal:
    rows = client.get("/api/v1/reports/trial-balance").json()["ledgers"]
    row = next(r for r in rows if r["ledger_name"] == ledger_name)
    return Decimal(row["balance"]) * (1 if row["dc"] == "D" else -1)


def _json(client, path: str, headers: dict | None = None) -> dict:
    r = client.get(f"/api/v1/reports/pack/{path}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _post(
    client,
    party: str,
    refs: list[dict],
    *,
    type_: str = "SAL",
    dr: str = "0.00",
    cr: str = "0.00",
    counter: str = "Sales",
    date_: str | None = None,
    **extra,
):
    """A party-moving voucher with bills allocations — the common shape."""
    entries: list[dict] = []
    if dr != "0.00":
        entries.append({"ledger_name": party, "dc": "D", "amount": dr})
    if cr != "0.00":
        entries.append({"ledger_name": party, "dc": "C", "amount": cr})
    entries.append(
        {
            "ledger_name": counter,
            "dc": "C" if dr != "0.00" else "D",
            "amount": dr if dr != "0.00" else cr,
        }
    )
    return post_voucher(
        client, type_=type_, date_=date_, meta=_bill_meta(party, refs, **extra), entries=entries
    )


# ------------------------------------------------- the allocation document
def test_invoice_opens_a_bill(client, fresh_db):
    """A sales invoice (meta.invoice) carrying meta.bills opens the bill for
    its grand total — the two packs cooperate: invoicing builds the lines,
    bills validates the split. The invariant ties them together."""
    _ensure_ledgers(client, ("Acme Traders", "Sundry Debtors"))
    r = post_voucher(
        client,
        meta={
            "invoice": {
                "customer": "Acme Traders",
                "items": [
                    {"description": "Widgets", "qty": "10", "rate": "100.00", "tax_rate": "18"}
                ],
            },
            **_bill_meta("Acme Traders", [_new("INV-1", "1180.00", due_days=30)]),
        },
        entries=[
            {"ledger_name": "Acme Traders", "dc": "D", "amount": "0.01"},
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
        ],
    )
    assert r.status_code == 201, r.text

    data = _json(client, "bills-open?party=Acme%20Traders&format=json")
    assert data["on_account"] == "0.00"
    assert len(data["bills"]) == 1
    bill = data["bills"][0]
    assert bill["ref"] == "INV-1"
    assert bill["side"] == "dr"
    assert bill["outstanding"] == "1180.00"
    assert bill["bucket"] == "not due"
    assert bill["due_on"] == (TODAY + timedelta(days=30)).isoformat()
    # the master theorem, from the first document on
    assert Decimal(bill["outstanding"]) == _trial_balance_of(client, "Acme Traders")


def test_receipt_settles_with_against_and_on_account(client, fresh_db):
    """The TallyPrime receipt flow: allocate against open bills, the excess
    lands on account. The ledger sees one Cr party line; the bills see the
    split."""
    party = "Settle Co"
    _ensure_ledgers(
        client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"), ("Bank", "Bank Accounts")
    )
    assert _post(client, party, [_new("INV-1", "1000.00")], dr="1000.00").status_code == 201

    rec = post_voucher(
        client,
        type_="RCT",
        meta=_bill_meta(
            party, [_against("INV-1", "700.00", cr=True), _on_account("50.00", cr=True)]
        ),
        entries=[
            {"ledger_name": "Bank", "dc": "D", "amount": "750.00"},
            *_party_lines(party, cr="750.00"),
        ],
    )
    assert rec.status_code == 201, rec.text

    data = _json(client, f"bills-open?party={party.replace(' ', '%20')}&format=json")
    assert data["bills"][0]["outstanding"] == "300.00"
    assert data["on_account"] == "-50.00"  # a credit pool: the party overpaid

    # the master theorem: bills + on-account == the trial balance figure
    # (ledger balance 1000 − 750 = 250 Dr; bills 300 − on-account 50 = 250)
    assert _trial_balance_of(client, party) == Decimal("250.00")


def test_advance_receipt_opens_a_credit_bill(client, fresh_db):
    """Money before an invoice: a new ref with cr opens an advance — the
    payable side of the same party, netting honestly."""
    party = "Advance Co"
    _ensure_ledgers(client, (party, "Sundry Debtors"), ("Bank", "Bank Accounts"))
    r = post_voucher(
        client,
        type_="RCT",
        meta=_bill_meta(party, [_new("ADV-1", "500.00", cr=True)]),
        entries=[
            {"ledger_name": "Bank", "dc": "D", "amount": "500.00"},
            *_party_lines(party, cr="500.00"),
        ],
    )
    assert r.status_code == 201, r.text
    data = _json(client, f"bills-open?party={party.replace(' ', '%20')}&format=json")
    assert data["bills"][0]["ref"] == "ADV-1"
    assert data["bills"][0]["side"] == "cr"
    assert data["bills"][0]["outstanding"] == "-500.00"
    # payable reports carry it; receivable reports do not
    rec = _json(client, "bills-receivable?format=json")
    assert all(b["party"] != party for b in rec["bills"])
    pay = _json(client, f"bills-payable?party={party.replace(' ', '%20')}&format=json")
    assert pay["total"] == "-500.00"


def test_purchase_and_payment_cover_the_payable_side(client, fresh_db):
    """The supplier mirror: a purchase opens a payable bill, a payment
    settles it — same fold, sign decides the report."""
    party = "Zen Supplies"
    _ensure_ledgers(
        client,
        (party, "Sundry Creditors"),
        ("Bank", "Bank Accounts"),
        ("Purchases", "Purchase Accounts"),
    )
    r = _post(
        client,
        party,
        [_new("BILL-1", "2000.00", due_days=15, cr=True)],
        type_="PUR",
        cr="2000.00",
        counter="Purchases",
    )
    assert r.status_code == 201, r.text
    # Purchases ledger needed for the counter line
    pay = post_voucher(
        client,
        type_="PYT",
        meta=_bill_meta(party, [_against("BILL-1", "2000.00")]),
        entries=[
            {"ledger_name": "Bank", "dc": "C", "amount": "2000.00"},
            *_party_lines(party, dr="2000.00"),
        ],
    )
    assert pay.status_code == 201, pay.text
    data = _json(client, f"bills-open?party={party.replace(' ', '%20')}&format=json")
    assert data["bills"] == []
    assert data["on_account"] == "0.00"
    assert _trial_balance_of(client, party) == Decimal("0.00")


def test_netting_journal_applies_advance_to_a_bill(client, fresh_db):
    """Dr party / Cr party nets to zero on the ledger; the refs move the
    advance onto the invoice. Both bills close; nothing else changes."""
    party = "Netting Co"
    _ensure_ledgers(
        client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"), ("Bank", "Bank Accounts")
    )
    assert _post(client, party, [_new("INV-1", "1000.00")], dr="1000.00").status_code == 201
    assert (
        post_voucher(
            client,
            type_="RCT",
            meta=_bill_meta(party, [_new("ADV-1", "400.00", cr=True)]),
            entries=[
                {"ledger_name": "Bank", "dc": "D", "amount": "400.00"},
                *_party_lines(party, cr="400.00"),
            ],
        ).status_code
        == 201
    )
    netting = post_voucher(
        client,
        type_="JV",
        meta=_bill_meta(party, [_against("ADV-1", "400.00"), _against("INV-1", "400.00", cr=True)]),
        entries=_party_lines(party, dr="400.00", cr="400.00"),
    )
    assert netting.status_code == 201, netting.text

    data = _json(client, f"bills-open?party={party.replace(' ', '%20')}&format=json")
    by_ref = {b["ref"]: b for b in data["bills"]}
    assert by_ref["INV-1"]["outstanding"] == "600.00"
    assert "ADV-1" not in by_ref  # closed bills leave the open list
    assert data["on_account"] == "0.00"
    # the ledger never moved
    assert _trial_balance_of(client, party) == Decimal("600.00")


# ------------------------------------------------------------- the guards
def test_conservation_violation_refused(client, fresh_db):
    party = "Conservation Co"
    _ensure_ledgers(client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"))
    r = _post(client, party, [_new("INV-1", "100.00")], dr="90.00")
    assert r.status_code == 422
    assert "must agree exactly" in r.json()["detail"]


def test_settling_an_unopened_bill_refused(client, fresh_db):
    party = "Ghost Co"
    _ensure_ledgers(client, (party, "Sundry Debtors"), ("Bank", "Bank Accounts"))
    r = post_voucher(
        client,
        type_="RCT",
        meta=_bill_meta(party, [_against("GHOST", "10.00", cr=True)]),
        entries=[
            {"ledger_name": "Bank", "dc": "D", "amount": "10.00"},
            *_party_lines(party, cr="10.00"),
        ],
    )
    assert r.status_code == 422
    assert "no prior document opened" in r.json()["detail"]


def test_over_settlement_refused_the_excess_belongs_on_account(client, fresh_db):
    """TallyPrime's discipline: settle at most to zero; the excess is an
    on-account allocation, which the error says."""
    party = "Oversettle Co"
    _ensure_ledgers(
        client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"), ("Bank", "Bank Accounts")
    )
    assert _post(client, party, [_new("INV-1", "100.00")], dr="100.00").status_code == 201
    r = post_voucher(
        client,
        type_="RCT",
        meta=_bill_meta(party, [_against("INV-1", "150.00", cr=True)]),
        entries=[
            {"ledger_name": "Bank", "dc": "D", "amount": "150.00"},
            *_party_lines(party, cr="150.00"),
        ],
    )
    assert r.status_code == 422
    assert "over-settles" in r.json()["detail"]
    assert "on_account" in r.json()["detail"]


def test_settling_a_closed_or_growing_bill_refused(client, fresh_db):
    party = "Closed Co"
    _ensure_ledgers(
        client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"), ("Bank", "Bank Accounts")
    )
    assert _post(client, party, [_new("INV-1", "100.00")], dr="100.00").status_code == 201
    assert (
        post_voucher(
            client,
            type_="RCT",
            meta=_bill_meta(party, [_against("INV-1", "100.00", cr=True)]),
            entries=[
                {"ledger_name": "Bank", "dc": "D", "amount": "100.00"},
                *_party_lines(party, cr="100.00"),
            ],
        ).status_code
        == 201
    )
    # closed: nothing left to settle
    r = post_voucher(
        client,
        type_="RCT",
        meta=_bill_meta(party, [_against("INV-1", "10.00", cr=True)]),
        entries=[
            {"ledger_name": "Bank", "dc": "D", "amount": "10.00"},
            *_party_lines(party, cr="10.00"),
        ],
    )
    assert r.status_code == 422
    assert "already fully settled" in r.json()["detail"]

    # re-opening the ref while closed is fine (a new bill of the same name)
    assert _post(client, party, [_new("INV-1", "50.00")], dr="50.00").status_code == 201
    # but settling away from zero (adding) is refused
    r = post_voucher(
        client,
        type_="RCT",
        meta=_bill_meta(party, [_against("INV-1", "10.00")]),
        entries=[
            {"ledger_name": "Bank", "dc": "C", "amount": "10.00"},
            *_party_lines(party, dr="10.00"),
        ],
    )
    assert r.status_code == 422
    assert "never add" in r.json()["detail"]


def test_duplicate_open_ref_refused(client, fresh_db):
    party = "Dup Co"
    _ensure_ledgers(client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"))
    assert _post(client, party, [_new("INV-1", "100.00")], dr="100.00").status_code == 201
    r = _post(client, party, [_new("INV-1", "50.00")], dr="50.00")
    assert r.status_code == 422
    assert "collides" in r.json()["detail"]


def test_unknown_party_and_unmoved_party_refused(client, fresh_db):
    _ensure_ledgers(client, ("Unmoved Co", "Sundry Debtors"), ("Sales", "Sales Accounts"))
    r = _post(client, "Nobody", [_new("INV-1", "10.00")], dr="10.00")
    assert r.status_code == 404
    assert "does not exist" in r.json()["detail"]

    r = post_voucher(
        client,
        meta=_bill_meta("Unmoved Co", [_new("INV-1", "10.00")]),
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "10.00"},
            {"ledger_name": "Sales", "dc": "C", "amount": "10.00"},
        ],
    )
    assert r.status_code == 422
    assert "does not move party ledger" in r.json()["detail"]


def test_payroll_conflict_and_empty_document_refused(client, fresh_db):
    party = "Payroll Co"
    _ensure_ledgers(client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"))
    r = post_voucher(
        client,
        meta={
            "payroll": {"mode": "run", "period": f"{TODAY:%Y-%m}", "employees": []},
            **_bill_meta(party, []),
        },
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "0.01"},
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
        ],
    )
    assert r.status_code == 422
    assert "payroll" in r.json()["detail"]

    r = post_voucher(
        client,
        meta=_bill_meta(party, []),
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "0.01"},
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
        ],
    )
    assert r.status_code == 422
    assert "does nothing" in r.json()["detail"]


def test_default_interest_declaration_rides_any_voucher(client, fresh_db):
    """A config-only bills document (no refs) needs no party movement —
    the fold reads declarations from whatever carries them."""
    party = "Declare Co"
    _ensure_ledgers(client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"))
    r = post_voucher(
        client,
        meta=_bill_meta(party, [], default_interest={"annual": "18", "grace_days": "7"}),
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "0.01"},
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
        ],
    )
    assert r.status_code == 201, r.text


# ------------------------------------------------------------- the reports
def test_ageing_buckets(client, fresh_db):
    """Bills due 10/40/70/100 days ago land in 0-30/31-60/61-90/90+; a
    future-due bill is 'not due'."""
    party = "Ageing Co"
    _ensure_ledgers(client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"))
    specs = [
        ("B-10", -10, "0-30"),
        ("B-40", -40, "31-60"),
        ("B-70", -70, "61-90"),
        ("B-100", -100, "90+"),
        ("B-FUTURE", 10, "not due"),
    ]
    for ref, due_offset, _bucket in specs:
        r = _post(
            client,
            party,
            [_new(ref, "100.00", due_on=(TODAY + timedelta(days=due_offset)).isoformat())],
            dr="100.00",
            date_=(TODAY - timedelta(days=120)).isoformat(),
        )
        assert r.status_code == 201, (ref, r.text)

    data = _json(client, f"bills-receivable?party={party.replace(' ', '%20')}&format=json")
    by_ref = {b["ref"]: b for b in data["bills"]}
    for ref, _offset, bucket in specs:
        assert by_ref[ref]["bucket"] == bucket, (ref, by_ref[ref]["bucket"])
    assert data["ageing"] == {
        "not due": "100.00",
        "0-30": "100.00",
        "31-60": "100.00",
        "61-90": "100.00",
        "90+": "100.00",
    }
    assert data["total"] == "500.00"
    # the master theorem against the trial balance
    assert Decimal(data["total"]) == _trial_balance_of(client, party)


def test_report_formats_pdf_and_csv(client, fresh_db):
    """The same fold through the ADR-013/014 bridges: a real PDF and a CSV."""
    party = "Print Co"
    _ensure_ledgers(client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"))
    assert (
        _post(client, party, [_new("INV-1", "1180.00", due_days=30)], dr="1180.00").status_code
        == 201
    )
    q = f"party={party.replace(' ', '%20')}"
    pdf = client.get(f"/api/v1/reports/pack/bills-receivable?{q}")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content[:5] == b"%PDF-"

    csv = client.get(f"/api/v1/reports/pack/bills-receivable?{q}&format=csv")
    assert csv.status_code == 200
    assert f"{party},INV-1" in csv.text
    assert "TOTAL,-,-,-,1180.00" in csv.text


def test_ledger_statement_bill_linked(client, fresh_db):
    """The statement walks every party entry with its bill allocations, and
    its closing balance equals the trial-balance figure."""
    party = "Statement Co"
    _ensure_ledgers(
        client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"), ("Bank", "Bank Accounts")
    )
    assert _post(client, party, [_new("INV-1", "1000.00")], dr="1000.00").status_code == 201
    assert (
        post_voucher(
            client,
            type_="RCT",
            meta=_bill_meta(party, [_against("INV-1", "400.00", cr=True)]),
            entries=[
                {"ledger_name": "Bank", "dc": "D", "amount": "400.00"},
                *_party_lines(party, cr="400.00"),
            ],
        ).status_code
        == 201
    )
    data = _json(client, f"ledger-statement?party={party.replace(' ', '%20')}&format=json")
    assert data["opening"] == "0.00"
    assert data["closing"] == "600.00"
    assert len(data["rows"]) == 2
    first, second = data["rows"]
    assert first["bills"] == "INV-1 Dr 1000.00"
    assert first["balance"] == "1000.00"
    assert second["bills"] == "INV-1 Cr 400.00"
    assert second["balance"] == "600.00"
    assert Decimal(data["closing"]) == _trial_balance_of(client, party)

    # a windowed statement: opening balance before the window
    future = (TODAY + timedelta(days=1)).isoformat()
    data = _json(
        client, f"ledger-statement?party={party.replace(' ', '%20')}&from={future}&format=json"
    )
    assert data["opening"] == "600.00"
    assert data["rows"] == []

    # CSV carries the same facts
    csv = client.get(
        f"/api/v1/reports/pack/ledger-statement?party={party.replace(' ', '%20')}&format=csv"
    )
    assert csv.status_code == 200
    assert "INV-1 Dr 1000.00" in csv.text
    assert "CLOSING" in csv.text

    # PDF prints through the bridge
    pdf = client.get(f"/api/v1/reports/pack/ledger-statement?party={party.replace(' ', '%20')}")
    assert pdf.status_code == 200
    assert pdf.content[:5] == b"%PDF-"


# ---------------------------------------------------------------- interest
def test_interest_arithmetic_and_rate_resolution(client, fresh_db):
    """1000 @ 18% for 37 days overdue = 18.25 (exact division, rounded
    once); the bill's own rate wins over the party default, which wins over
    the request's rate; grace shifts the clock; a bill with no resolvable
    rate is listed unpriced."""
    party = "Rate Co"
    no_rate_party = "NoRate Co"  # a party with no default declaration
    _ensure_ledgers(
        client,
        (party, "Sundry Debtors"),
        (no_rate_party, "Sundry Debtors"),
        ("Sales", "Sales Accounts"),
    )
    # the party default, declared before the bills open
    post_voucher(
        client,
        date_=(TODAY - timedelta(days=90)).isoformat(),
        meta=_bill_meta(party, [], default_interest={"annual": "12"}),
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": "0.01"},
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
        ],
    )
    overdue = (TODAY - timedelta(days=37)).isoformat()
    r = _post(
        client,
        party,
        [
            _new("OWN-RATE", "1000.00", due_on=overdue, interest={"annual": "18"}),
            _new("DEFAULT-RATE", "1000.00", due_on=overdue),
            _new(
                "GRACED", "1000.00", due_on=overdue, interest={"annual": "18", "grace_days": "10"}
            ),
        ],
        dr="3000.00",
        date_=(TODAY - timedelta(days=60)).isoformat(),
    )
    assert r.status_code == 201, r.text
    assert (
        _post(
            client,
            no_rate_party,
            [_new("NO-RATE", "1000.00", due_on=overdue)],
            dr="1000.00",
            date_=(TODAY - timedelta(days=60)).isoformat(),
        ).status_code
        == 201
    )

    data = _json(client, "bills-interest?format=json")
    by_ref = {row["ref"]: row for row in data["rows"]}
    # 1000 × 18% × 37 / 365 = 18.2465... → 18.25
    assert by_ref["OWN-RATE"]["rate"] == "18"
    assert by_ref["OWN-RATE"]["interest"] == "18.25"
    # the party default (12%) applies where the bill declares none
    assert by_ref["DEFAULT-RATE"]["rate"] == "12"
    assert by_ref["DEFAULT-RATE"]["interest"] == "12.16"  # 1000×12%×37/365
    # grace 10 → 27 days countable: 1000×18%×27/365 = 13.3150... → 13.32
    assert by_ref["GRACED"]["interest"] == "13.32"
    # no rate resolves → listed, unpriced, zero interest, named in the payload
    assert by_ref["NO-RATE"]["rate"] is None
    assert by_ref["NO-RATE"]["interest"] == "0.00"
    assert "NO-RATE" in data["unpriced_refs"]
    # OWN + DEFAULT + GRACED
    priced = sum(
        (Decimal(row["interest"]) for row in data["rows"] if row["rate"] is not None),
        Decimal("0.00"),
    )
    assert priced == Decimal("43.73")

    # an explicit rate prices the unpriced bill; the declared rates keep winning
    data = _json(client, "bills-interest?rate=24&format=json")
    by_ref = {row["ref"]: row for row in data["rows"]}
    assert by_ref["NO-RATE"]["rate"] == "24"
    assert by_ref["NO-RATE"]["interest"] == "24.33"  # 1000×24%×37/365
    assert by_ref["OWN-RATE"]["rate"] == "18"
    assert by_ref["DEFAULT-RATE"]["rate"] == "12"

    # CSV and PDF carry the computation
    csv = client.get("/api/v1/reports/pack/bills-interest?format=csv")
    assert csv.status_code == 200
    assert "party,ref,due_on,outstanding,days_overdue,rate,interest" in csv.text
    pdf = client.get("/api/v1/reports/pack/bills-interest")
    assert pdf.status_code == 200
    assert pdf.content[:5] == b"%PDF-"


def test_interest_charge_document_mode(client, fresh_db):
    """The charge posts Dr party / Cr Interest Income, opens a bill for its
    total, and annotates the computation; a repeat with the same ref while
    the first charge bill is open collides (the accountant renames)."""
    bills_pack.POSTED.clear()
    party = "Charge Co"
    _ensure_ledgers(client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"))
    assert (
        _post(
            client,
            party,
            [
                _new(
                    "INV-1",
                    "1000.00",
                    due_on=(TODAY - timedelta(days=30)).isoformat(),
                    interest={"annual": "18"},
                )
            ],
            dr="1000.00",
            date_=(TODAY - timedelta(days=60)).isoformat(),
        ).status_code
        == 201
    )

    r = post_voucher(
        client,
        type_="JV",
        meta={"bills": {"party": party, "interest_charge": {"ref": "INT-1"}}},
        entries=[
            {"ledger_name": party, "dc": "D", "amount": "0.01"},
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
        ],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # 1000 × 18% × 30 / 365 = 14.794520... → 14.79
    lines = {(e["ledger_name"], e["dc"]): e for e in body["entries"]}
    assert Decimal(lines[(party, "D")]["amount"]) == Decimal("14.79")
    assert Decimal(lines[("Interest Income", "C")]["amount"]) == Decimal("14.79")
    assert "interest 18% p.a. on INV-1, 30 days" in lines[(party, "D")]["narration"]

    computed = body["meta"]["bills"]["computed"]
    assert computed["total"] == "14.79"
    assert computed["charge_ref"] == "INT-1"
    assert computed["lines"][0]["ref"] == "INV-1"

    # the charge opened a bill for its total — the master theorem holds
    assert _trial_balance_of(client, party) == Decimal("1014.79")
    data = _json(client, f"bills-open?party={party.replace(' ', '%20')}&format=json")
    by_ref = {b["ref"]: b for b in data["bills"]}
    assert by_ref["INT-1"]["outstanding"] == "14.79"

    # a repeat with the same ref collides while the first is open
    r = post_voucher(
        client,
        type_="JV",
        meta={"bills": {"party": party, "interest_charge": {"ref": "INT-1"}}},
        entries=[
            {"ledger_name": party, "dc": "D", "amount": "0.01"},
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
        ],
    )
    assert r.status_code == 422
    assert "collides" in r.json()["detail"]

    # and the observability seam recorded the one posted charge
    charges = [p for p in bills_pack.POSTED if p["interest_charge"] and p["party"] == party]
    assert len(charges) == 1


def test_interest_charge_refuses_ambiguity(client, fresh_db):
    party = "Refuse Co"
    _ensure_ledgers(client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"))
    # as_of must equal the voucher date
    r = post_voucher(
        client,
        type_="JV",
        date_=(TODAY - timedelta(days=1)).isoformat(),
        meta={"bills": {"party": party, "interest_charge": {"as_of": TODAY.isoformat()}}},
        entries=[
            {"ledger_name": party, "dc": "D", "amount": "0.01"},
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
        ],
    )
    assert r.status_code == 422
    assert "must equal the voucher date" in r.json()["detail"]

    # no overdue bills → nothing to charge
    assert (
        _post(client, party, [_new("INV-1", "100.00", due_days=30)], dr="100.00").status_code == 201
    )
    r = post_voucher(
        client,
        type_="JV",
        meta={"bills": {"party": party, "interest_charge": {}}},
        entries=[
            {"ledger_name": party, "dc": "D", "amount": "0.01"},
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
        ],
    )
    assert r.status_code == 422
    assert "no overdue open bills" in r.json()["detail"]

    # overdue but unpriced, with no rate anywhere → refused, naming the bills
    assert (
        _post(
            client,
            party,
            [_new("INV-2", "100.00", due_on=(TODAY - timedelta(days=10)).isoformat())],
            dr="100.00",
            date_=(TODAY - timedelta(days=60)).isoformat(),
        ).status_code
        == 201
    )
    r = post_voucher(
        client,
        type_="JV",
        meta={"bills": {"party": party, "interest_charge": {}}},
        entries=[
            {"ledger_name": party, "dc": "D", "amount": "0.01"},
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
        ],
    )
    assert r.status_code == 422
    assert "INV-2" in r.json()["detail"]

    # explicit refs alongside the charge are ambiguous
    r = post_voucher(
        client,
        type_="JV",
        meta={
            "bills": {
                "party": party,
                "interest_charge": {"rate": "18"},
                "refs": [_new("X", "1.00")],
            }
        },
        entries=[
            {"ledger_name": party, "dc": "D", "amount": "0.01"},
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
        ],
    )
    assert r.status_code == 422
    assert "ambiguous" in r.json()["detail"]

    # and it refuses to share a draft with another document mode
    r = post_voucher(
        client,
        meta={
            "invoice": {
                "customer": party,
                "items": [{"description": "X", "qty": "1", "rate": "10.00", "tax_rate": "0"}],
            },
            "bills": {"party": party, "interest_charge": {"rate": "18"}},
        },
        entries=[
            {"ledger_name": party, "dc": "D", "amount": "0.01"},
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
        ],
    )
    assert r.status_code == 422
    assert "mutually exclusive" in r.json()["detail"]


# ------------------------------------------------------ reversals & isolation
def test_reversals_net_in_the_fold(client, fresh_db):
    """Undo an invoice → its bill vanishes; undo the receipt → the settled
    balance restores. The fold mirrors the mirrored entries."""
    party = "Reverse Co"
    _ensure_ledgers(
        client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"), ("Bank", "Bank Accounts")
    )
    inv = _post(client, party, [_new("INV-1", "1000.00")], dr="1000.00")
    assert inv.status_code == 201, inv.text
    rec = post_voucher(
        client,
        type_="RCT",
        meta=_bill_meta(party, [_against("INV-1", "600.00", cr=True)]),
        entries=[
            {"ledger_name": "Bank", "dc": "D", "amount": "600.00"},
            *_party_lines(party, cr="600.00"),
        ],
    )
    assert rec.status_code == 201, rec.text
    q = f"bills-open?party={party.replace(' ', '%20')}&format=json"
    assert _json(client, q)["bills"][0]["outstanding"] == "400.00"

    # undo the receipt: the settlement restores
    rev_rec = client.post(f"/api/v1/vouchers/{rec.json()['id']}/reverse")
    assert rev_rec.status_code == 201
    assert _json(client, q)["bills"][0]["outstanding"] == "1000.00"

    # undo the invoice: the bill vanishes entirely
    rev_inv = client.post(f"/api/v1/vouchers/{inv.json()['id']}/reverse")
    assert rev_inv.status_code == 201
    data = _json(client, q)
    assert data["bills"] == []
    assert data["on_account"] == "0.00"
    # and the trial balance agrees: both sides netted to zero
    assert _trial_balance_of(client, party) == Decimal("0.00")


def test_idempotent_replay_allocates_once(client, fresh_db):
    import uuid

    party = "Replay Co"
    _ensure_ledgers(client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"))
    token = uuid.uuid4()
    payload = dict(
        meta=_bill_meta(party, [_new("INV-1", "100.00")]),
        entries=_party_lines(party, dr="100.00")
        + [{"ledger_name": "Sales", "dc": "C", "amount": "100.00"}],
        token=token,
    )
    first = post_voucher(client, **payload)
    second = post_voucher(client, **payload)
    assert first.status_code == 201 and second.status_code == 200
    data = _json(client, f"bills-open?party={party.replace(' ', '%20')}&format=json")
    assert len(data["bills"]) == 1
    assert data["bills"][0]["outstanding"] == "100.00"


def test_opening_balance_carries_bills(client, fresh_db):
    """An opening journal with party lines opens bills for them — Law 1
    applies to openings, the conservation law applies to bills."""
    party = "Opening Co"
    _ensure_ledgers(client, (party, "Sundry Debtors"))
    r = post_voucher(
        client,
        type_="OPEN",
        meta=_bill_meta(party, [_new("OB-1", "5000.00")]),
        entries=_party_lines(party, dr="5000.00")
        + [{"ledger_name": "Profit & Loss A/c", "dc": "C", "amount": "5000.00"}],
    )
    assert r.status_code == 201, r.text
    data = _json(client, f"bills-open?party={party.replace(' ', '%20')}&format=json")
    assert data["bills"][0]["ref"] == "OB-1"
    assert data["bills"][0]["outstanding"] == "5000.00"
    assert _trial_balance_of(client, party) == Decimal("5000.00")


# the two book-creating tests run last: once a second book exists, the
# headerless default-book auto-resolution retires (Phase 1 W2) and every
# later request must carry its X-Book-Id explicitly
def test_books_are_strangers(client, fresh_db):
    """Same party name, two companies: two ledgers, two folds — ADR-009
    isolation inherited end to end."""
    party = "Stranger Co"
    r = client.post("/api/v1/books", json={"name": "Zeta Ventures", "base_currency": "INR"})
    assert r.status_code == 201, r.text
    zeta_id = r.json()["book"]["id"]
    books = client.get("/api/v1/books").json()
    default_id = next(b["id"] for b in books if b["slug"] == "default")
    headers = {"X-Book-Id": str(default_id)}
    zeta_headers = {"X-Book-Id": str(zeta_id)}

    for h in (headers, zeta_headers):
        for name, group in ((party, "Sundry Debtors"), ("Sales", "Sales Accounts")):
            r = client.post("/api/v1/ledgers", headers=h, json={"name": name, "group_name": group})
            # 422 = an earlier test already made this ledger in this book
            assert r.status_code in (201, 422), (h, name, r.text)
        r = client.post(
            "/api/v1/vouchers",
            headers=h,
            json={
                "voucher_type": "SAL",
                "voucher_date": TODAY.isoformat(),
                "meta": _bill_meta(party, [_new("INV-1", "100.00")]),
                "entries": [
                    {"ledger_name": party, "dc": "D", "amount": "100.00"},
                    {"ledger_name": "Sales", "dc": "C", "amount": "100.00"},
                ],
            },
        )
        assert r.status_code == 201, r.text

    for h in (headers, zeta_headers):
        data = _json(client, f"bills-open?party={party.replace(' ', '%20')}&format=json", headers=h)
        assert len(data["bills"]) == 1
        assert data["bills"][0]["outstanding"] == "100.00"


def test_ledger_entries_is_book_scoped(client, fresh_db):
    """Resolver.ledger_entries (plugin API 0.8) never crosses books: the
    statement of the same-named party is two different statements."""
    party = "Scoped Co"
    r = client.post("/api/v1/books", json={"name": "Zeta Ventures 2", "base_currency": "INR"})
    zeta_id = r.json()["book"]["id"]
    books = client.get("/api/v1/books").json()
    default_id = next(b["id"] for b in books if b["slug"] == "default")
    default_headers = {"X-Book-Id": str(default_id)}
    zeta_headers = {"X-Book-Id": str(zeta_id)}

    _ensure_ledgers(client, (party, "Sundry Debtors"), headers=default_headers)
    for name, group in ((party, "Sundry Debtors"), ("Sales", "Sales Accounts")):
        r = client.post(
            "/api/v1/ledgers", headers=zeta_headers, json={"name": name, "group_name": group}
        )
        assert r.status_code == 201, r.text
    client.post(
        "/api/v1/vouchers",
        headers=zeta_headers,
        json={
            "voucher_type": "SAL",
            "voucher_date": TODAY.isoformat(),
            "entries": [
                {"ledger_name": party, "dc": "D", "amount": "10.00"},
                {"ledger_name": "Sales", "dc": "C", "amount": "10.00"},
            ],
        },
    )

    # Zeta's statement, under Zeta's header: one row
    r = client.get(
        "/api/v1/reports/pack/ledger-statement",
        headers=zeta_headers,
        params={"party": party, "format": "json"},
    )
    assert r.status_code == 200
    assert len(r.json()["rows"]) == 1

    # the default book's same-named party has no entries — nothing to state
    r = client.get(
        "/api/v1/reports/pack/ledger-statement",
        headers=default_headers,
        params={"party": party, "format": "json"},
    )
    assert r.status_code == 404
