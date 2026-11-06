"""The invoicing plugin pack (contrib/invoicing.py): the first complete
business document built purely on the plugin runtime.

The promises under test (ADR-003 + ADR-010):
- ``meta.invoice`` (customer, items with qty/rate/tax, due days) derives
  the exact TallyPrime GST-sale shape: one party debit for the grand
  total, one revenue credit per item (qty @ rate kept in the narration),
  CGST/SGST split lines per taxed item — with the split always summing to
  the tax exactly (remainder arithmetic, Law 3).
- The document persists with the voucher: ``GET /vouchers/{id}`` returns
  ``meta`` including the pack's ``computed`` annotations (ADR-010).
- Masters resolve/create through the book-scoped Resolver only — party
  under Sundry Debtors, revenue under Sales Accounts, tax under
  Duties & Taxes, settlement banks under Bank Accounts.
- ``meta.receipt`` settles AR: debit bank, credit customer.
- Fat-finger guards: non-slab tax rates, non-positive qty, gst+invoice
  double-tax, invoice+receipt ambiguity, future document dates — all 422.
- Idempotent replay does not re-issue the document; reversals are new
  documents with empty meta; books are strangers (same customer name in
  two books is two ledgers, numbering starts from 1 in each).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from conftest import create_ledger, post_voucher

from ledgerforge.contrib import invoicing

# ------------------------------------------------------------------ helpers


def _invoice_meta(
    customer: str = "Acme Traders",
    items: list[dict] | None = None,
    **extra,
) -> dict:
    doc = {
        "customer": customer,
        "items": items
        if items is not None
        else [
            {"description": "Widgets", "qty": "10", "rate": "100.00", "tax_rate": "18"},
            {"description": "Installation", "qty": "1", "rate": "250.00", "tax_rate": "0"},
        ],
        "due_days": 30,
    }
    doc.update(extra)
    return {"invoice": doc}


def _lines_by_ledger(body: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in body["entries"]:
        out.setdefault(e["ledger_name"], []).append(e)
    return out


def _ledger_groups(client) -> dict[str, str]:
    return {row["name"]: row["group_name"] for row in client.get("/api/v1/ledgers").json()}


def _seed_entry(amount: str = "0.01") -> list[dict]:
    """The API schema demands >=1 entry; in document mode entries are
    placeholders the transformer replaces. Both sides of the ledger so the
    example-gst transformer (loaded first) can also run on conflict probes."""
    return [
        {"ledger_name": "Acme Traders", "dc": "D", "amount": amount},
        {"ledger_name": "Sales", "dc": "C", "amount": amount},
    ]


# ------------------------------------------------------------------- tests
def test_invoice_builds_tally_shape(client, fresh_db):
    """Two items — 10×100 @ 18% GST and 1×250 exempt — become the exact
    TallyPrime GST-sale shape, and the document round-trips with computed
    totals (ADR-010)."""
    invoicing.ISSUED.clear()
    r = post_voucher(client, meta=_invoice_meta(), entries=_seed_entry())
    assert r.status_code == 201, r.text
    body = r.json()

    lines = _lines_by_ledger(body)
    assert len(lines["Acme Traders"]) == 1
    party = lines["Acme Traders"][0]
    assert party["dc"] == "D"
    assert Decimal(party["amount"]) == Decimal("1430.00")
    assert "due" in party["narration"]

    revenue = {e["narration"]: e for e in lines["Sales"]}
    assert Decimal(revenue["Widgets × 10 @ 100.00"]["amount"]) == Decimal("1000.00")
    assert Decimal(revenue["Installation × 1 @ 250.00"]["amount"]) == Decimal("250.00")

    assert Decimal(lines["CGST Payable"][0]["amount"]) == Decimal("90.00")
    assert Decimal(lines["SGST Payable"][0]["amount"]) == Decimal("90.00")

    computed = body["meta"]["invoice"]["computed"]
    posted_on = date.fromisoformat(body["voucher_date"])
    assert computed["subtotal"] == "1250.00"
    assert computed["tax_total"] == "180.00"
    assert computed["grand_total"] == "1430.00"
    assert computed["due_date"] == (posted_on + timedelta(days=30)).isoformat()

    # GET returns the same complete document (persistence, not echo)
    got = client.get(f"/api/v1/vouchers/{body['id']}").json()
    assert got["meta"]["invoice"]["computed"] == computed


def test_invoice_tax_split_is_exact(client, fresh_db):
    """5% on 333.33: tax 16.67 → CGST 8.34 + SGST 8.33 — the remainder
    split always sums exactly to the tax."""
    r = post_voucher(
        client,
        meta=_invoice_meta(
            customer="Odd Traders",
            items=[{"description": "Oddity", "qty": "1", "rate": "333.33", "tax_rate": "5"}],
        ),
        entries=_seed_entry(),
    )
    assert r.status_code == 201, r.text
    lines = _lines_by_ledger(r.json())
    cgst = Decimal(lines["CGST Payable"][0]["amount"])
    sgst = Decimal(lines["SGST Payable"][0]["amount"])
    assert cgst + sgst == Decimal("16.67")
    assert Decimal(lines["Odd Traders"][0]["amount"]) == Decimal("350.00")


def test_invoice_auto_creates_masters_in_seed_groups(client, fresh_db):
    """Party/revenue/tax ledgers appear — under the seed groups, not loose."""
    r = post_voucher(client, meta=_invoice_meta(), entries=_seed_entry())
    assert r.status_code == 201, r.text
    groups = _ledger_groups(client)
    assert groups["Acme Traders"] == "Sundry Debtors"
    assert groups["Sales"] == "Sales Accounts"
    assert groups["CGST Payable"] == "Duties & Taxes"
    assert groups["SGST Payable"] == "Duties & Taxes"


def test_receipt_settles_ar(client, fresh_db):
    """Invoice 1180, then a receipt for 1180: Dr Bank / Cr customer —
    and both documents carry their meta."""
    inv = post_voucher(
        client,
        meta=_invoice_meta(
            customer="Beta Buyer",
            items=[{"description": "Goods", "qty": "1", "rate": "1000.00", "tax_rate": "18"}],
        ),
        entries=_seed_entry(),
    )
    assert inv.status_code == 201, inv.text

    rec = post_voucher(
        client,
        type_="RCT",
        meta={
            "receipt": {
                "customer": "Beta Buyer",
                "amount": "1180.00",
                "bank_ledger": "HDFC Bank",
                "reference": "NEFT-42",
            }
        },
        entries=_seed_entry(),
    )
    assert rec.status_code == 201, rec.text
    body = rec.json()
    lines = _lines_by_ledger(body)
    assert len(body["entries"]) == 2
    assert Decimal(lines["HDFC Bank"][0]["amount"]) == Decimal("1180.00")
    assert lines["HDFC Bank"][0]["dc"] == "D"
    assert Decimal(lines["Beta Buyer"][0]["amount"]) == Decimal("1180.00")
    assert lines["Beta Buyer"][0]["dc"] == "C"
    assert body["meta"]["receipt"]["computed"] == {"settled": "1180.00"}

    groups = _ledger_groups(client)
    assert groups["HDFC Bank"] == "Bank Accounts"
    assert groups["Beta Buyer"] == "Sundry Debtors"


def test_meta_round_trips_for_every_voucher(client, fresh_db):
    """Even a plain gst-mode voucher keeps its meta now (ADR-010) — the
    stored document is what was posted."""
    create_ledger(client, "Gamma Traders", "Sundry Debtors")
    create_ledger(client, "Gamma Sales", "Sales Accounts")
    r = post_voucher(
        client,
        meta={"gst": {"rate": "18"}},
        entries=[
            {"ledger_name": "Gamma Traders", "dc": "D", "amount": "100.00"},
            {"ledger_name": "Gamma Sales", "dc": "C", "amount": "100.00"},
        ],
    )
    assert r.status_code == 201, r.text
    got = client.get(f"/api/v1/vouchers/{r.json()['id']}").json()
    assert got["meta"] == {"gst": {"rate": "18"}}


def test_invoice_rejects_non_slab_tax_rate(client, fresh_db):
    """15 is not a GST slab — wrong tax must not post silently."""
    r = post_voucher(
        client,
        meta=_invoice_meta(
            customer="Slip Traders",
            items=[{"description": "X", "qty": "1", "rate": "100.00", "tax_rate": "15"}],
        ),
        entries=_seed_entry(),
    )
    assert r.status_code == 422
    assert "GST slab" in r.json()["detail"]


def test_invoice_rejects_bad_qty_and_rate(client, fresh_db):
    for bad in ({"qty": "0"}, {"qty": "-2"}, {"rate": "-1"}):
        item = {"description": "X", "qty": "1", "rate": "100.00", "tax_rate": "0"}
        item.update(bad)
        r = post_voucher(
            client,
            meta=_invoice_meta(customer="Qty Traders", items=[item]),
            entries=_seed_entry(),
        )
        assert r.status_code == 422, (bad, r.text)


def test_document_mode_conflicts_are_rejected(client, fresh_db):
    """gst+invoice would double-tax; invoice+receipt is ambiguous."""
    r = post_voucher(
        client,
        meta={**_invoice_meta(), "gst": {"rate": "18"}},
        entries=_seed_entry(),
    )
    assert r.status_code == 422
    assert "double-tax" in r.json()["detail"]

    r = post_voucher(
        client,
        meta={**_invoice_meta(), "receipt": {"customer": "Acme Traders", "amount": "1.00"}},
        entries=_seed_entry(),
    )
    assert r.status_code == 422
    assert "mutually exclusive" in r.json()["detail"]


def test_future_document_date_blocked_unless_overridden(client, fresh_db):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = post_voucher(client, date_=tomorrow, meta=_invoice_meta(), entries=_seed_entry())
    assert r.status_code == 422
    assert "future" in r.json()["detail"]

    r = post_voucher(
        client,
        date_=tomorrow,
        meta={**_invoice_meta(), "policy:allow_future_document_dates": True},
        entries=_seed_entry(),
    )
    assert r.status_code == 201, r.text


def test_invoice_idempotent_replay_does_not_reissue(client, fresh_db):
    """Same client_token twice: one voucher, one ISSUED record, one set of
    masters — the offline-retry promise for documents too."""
    invoicing.ISSUED.clear()
    import uuid

    token = uuid.uuid4()
    first = post_voucher(client, meta=_invoice_meta(), entries=_seed_entry(), token=token)
    assert first.status_code == 201, first.text
    ledgers_after_first = client.get("/api/v1/ledgers").json()

    second = post_voucher(client, meta=_invoice_meta(), entries=_seed_entry(), token=token)
    assert second.status_code == 200, second.text  # replay answers 200, not 201
    assert second.json()["replayed"] is True
    assert second.json()["id"] == first.json()["id"]
    assert len(invoicing.ISSUED) == 1
    assert client.get("/api/v1/ledgers").json() == ledgers_after_first


def test_issued_records_observability(client, fresh_db):
    """The after-commit seam an e-invoicing bridge would attach to."""
    invoicing.ISSUED.clear()
    r = post_voucher(client, meta=_invoice_meta(), entries=_seed_entry())
    assert r.status_code == 201, r.text
    assert len(invoicing.ISSUED) == 1
    record = invoicing.ISSUED[0]
    assert record["kind"] == "invoice"
    assert record["customer"] == "Acme Traders"
    assert record["voucher_no"] == r.json()["voucher_no"]


def test_reversal_is_a_new_document(client, fresh_db):
    """Reversing an invoice mirrors the lines but starts from empty meta —
    the credit note is its own document (ADR-010)."""
    inv = post_voucher(client, meta=_invoice_meta(), entries=_seed_entry())
    assert inv.status_code == 201, inv.text
    rev = client.post(f"/api/v1/vouchers/{inv.json()['id']}/reverse")
    assert rev.status_code == 201, rev.text
    body = rev.json()
    assert body["meta"] == {}
    party_credit = next(
        e for e in body["entries"] if e["ledger_name"] == "Acme Traders" and e["dc"] == "C"
    )
    assert Decimal(party_credit["amount"]) == Decimal("1430.00")


def test_books_are_strangers(client, fresh_db):
    """Same customer, two companies: two party ledgers, numbering from 1
    in each book — the document pack inherits ADR-009 isolation."""
    r = client.post("/api/v1/books", json={"name": "Zeta Ventures", "base_currency": "INR"})
    assert r.status_code == 201, r.text
    zeta_id = r.json()["book"]["id"]
    books = client.get("/api/v1/books").json()
    default_id = next(b["id"] for b in books if b["slug"] == "default")

    inv_default = client.post(
        "/api/v1/vouchers",
        headers={"X-Book-Id": str(default_id)},
        json={
            "voucher_type": "SAL",
            "voucher_date": date.today().isoformat(),
            "entries": _seed_entry(),
            "meta": _invoice_meta(),
        },
    )
    assert inv_default.status_code == 201, inv_default.text

    inv_zeta = client.post(
        "/api/v1/vouchers",
        headers={"X-Book-Id": str(zeta_id)},
        json={
            "voucher_type": "SAL",
            "voucher_date": date.today().isoformat(),
            "entries": _seed_entry(),
            "meta": _invoice_meta(),
        },
    )
    assert inv_zeta.status_code == 201, inv_zeta.text

    default_ledgers = {
        row["name"]: row["id"]
        for row in client.get("/api/v1/ledgers", headers={"X-Book-Id": str(default_id)}).json()
    }
    zeta_ledgers = {
        row["name"]: row["id"]
        for row in client.get("/api/v1/ledgers", headers={"X-Book-Id": str(zeta_id)}).json()
    }
    assert "Acme Traders" in default_ledgers and "Acme Traders" in zeta_ledgers
    assert default_ledgers["Acme Traders"] != zeta_ledgers["Acme Traders"]
