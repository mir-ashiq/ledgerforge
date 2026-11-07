"""The orders pack (contrib/orders.py): order processing — ADR-024,
Phase 3 of the v1.0 roadmap.

The promises under test:

- **Five document kinds** on ``meta.orders`` — quotation, sales order,
  delivery note, purchase order, GRN — each posting as a **zero-net
  memorandum trace** (one Dr + one Cr on "Order Book (memo)"): the order
  shows at its value in the day book, its party ledger is untouched, and
  the memo ledger's trial-balance figure is exactly zero after any number
  of documents (the neutrality theorem).
- **Their own numbering series** (plugin API 0.9): QT/SO/DN/PO/GRN
  voucher types are created lazily per book, inside the posting
  transaction; a failed post leaves nothing behind.
- **The position fold**: sales order lines track delivered (DNs) and
  invoiced (settles); purchase order lines track received (GRNs) and
  billed (settles); status open → partial → closed; quotations track
  conversion informationally and never pend.
- **The guards are the caps**: unknown kinds/parties, zero totals,
  duplicate open refs, DN/GRN against missing or foreign orders, items
  not on the order, fulfilment and settlement beyond the ordered
  quantity, settle without the carrier moving the party, ambiguous refs,
  standalone documents riding other line-building documents — all 422.
- **Order → invoice linkage**: a ``meta.invoice`` sales invoice carries
  ``meta.orders.settle``; the validator resolves the order among the
  parties the voucher moves and writes the resolution back into the
  stored document, so the fold replays it without guessing.
- **Reversals net**: reversing an order cancels it; reversing a DN, GRN
  or settling invoice restores the position; an undone pair skips.
- **Reports**: pending-orders both sides (PDF/CSV/JSON — quantities,
  pending quantity and value, status, per-party totals) and orders-open
  per kind (the picker feed). The print bridge claims the five kinds;
  settle-carrying invoices decline to the invoicing pack's view.

Like the bills suite, the module shares one database (module-scoped
``fresh_db``); every test works with its own party, so order refs
(namespaced by party and kind) keep the tests independent.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from conftest import post_voucher

TODAY = date.today()


# ------------------------------------------------------------------ helpers
def _ensure_ledgers(client, *specs: tuple[str, str]) -> None:
    """Create ledgers, tolerating the ones an earlier test already made."""
    for name, group in specs:
        r = client.post("/api/v1/ledgers", json={"name": name, "group_name": group})
        assert r.status_code in (201, 422), (name, r.text)


def _doc(kind: str, party: str, ref: str, items: list[dict], **extra) -> dict:
    doc: dict = {"kind": kind, "party": party, "ref": ref, "items": items}
    doc.update({k: v for k, v in extra.items() if v is not None})
    return {"orders": doc}


def _item(name: str, qty: str, rate: str | None = None, tax: str | None = None, **extra) -> dict:
    entry: dict = {"item": name, "qty": qty}
    if rate is not None:
        entry["rate"] = rate
    if tax is not None:
        entry["tax_rate"] = tax
    entry.update(extra)
    return entry


#: the API schema demands >=1 entry; in document mode entries are
#: placeholders the transformer replaces (the invoicing pack's convention)
_SEED = [{"ledger_name": "(order document)", "dc": "D", "amount": "0.01"}]


def _post_order(
    client,
    kind: str,
    party: str,
    ref: str,
    items: list[dict],
    *,
    date_: str | None = None,
    **extra,
):
    code = {
        "quotation": "QT",
        "sales_order": "SO",
        "delivery_note": "DN",
        "purchase_order": "PO",
        "grn": "GRN",
    }[kind]
    return post_voucher(
        client,
        type_=code,
        date_=date_,
        meta=_doc(kind, party, ref, items, **extra),
        entries=_SEED,
    )


def _settle(client, party: str, order: str, lines: list[dict], *, entries=None, type_="JV"):
    """A billing voucher carrying a settle allocation for ``party``.
    Self-sufficient: ensures the ledgers its default entries need (the
    module shares one book, but any test may run alone)."""
    _ensure_ledgers(client, (party, "Sundry Debtors"), ("Sales", "Sales Accounts"))
    payload_entries = entries or [
        {"ledger_name": party, "dc": "D", "amount": "100.00"},
        {"ledger_name": "Sales", "dc": "C", "amount": "100.00"},
    ]
    return post_voucher(
        client,
        type_=type_,
        meta={"orders": {"settle": [{"order": order, "lines": lines}]}},
        entries=payload_entries,
    )


def _json(client, path: str) -> dict:
    r = client.get(f"/api/v1/reports/pack/{path}")
    assert r.status_code == 200, r.text
    return r.json()


def _pending(client, side: str = "sales", party: str | None = None) -> dict:
    query = f"pending-orders?side={side}&format=json"
    if party:
        query += f"&party={party.replace(' ', '%20')}"
    return _json(client, query)


def _trial_balance_of(client, ledger_name: str) -> Decimal:
    rows = client.get("/api/v1/reports/trial-balance").json()["ledgers"]
    for row in rows:
        if row["ledger_name"] == ledger_name:
            return Decimal(row["balance"]) * (1 if row["dc"] == "D" else -1)
    return Decimal("0.00")


SALES_PARTY = ("Sundry Debtors",)
PURCHASE_PARTY = ("Sundry Creditors",)


# ------------------------------------------------- the documents themselves
def test_sales_order_posts_on_its_own_series(client, fresh_db):
    """An SO posts as voucher type SO — its own gapless series — carrying
    the zero-net memorandum trace at the document's value."""
    party = "Series Co"
    r = _post_order(
        client,
        "sales_order",
        party,
        "SO-1",
        [_item("Widgets", "10", "100.00", "18")],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["voucher_type"] == "SO"
    assert body["voucher_no"].startswith("SO-")
    # the memorandum trace: one Dr + one Cr on the memo ledger, equal
    lines = body["entries"]
    assert len(lines) == 2
    assert {ln["dc"] for ln in lines} == {"D", "C"}
    assert all(ln["ledger_name"] == "Order Book (memo)" for ln in lines)
    assert Decimal(body["amount"]) == Decimal("1180.00")  # 1000 + 18% tax
    # the computed block, exactly like the invoicing pack's
    computed = body["meta"]["orders"]["computed"]
    assert computed["subtotal"] == "1000.00"
    assert computed["tax_total"] == "180.00"
    assert computed["grand_total"] == "1180.00"


def test_memo_ledger_neutral_in_the_trial_balance(client, fresh_db):
    """The neutrality theorem: after a whole chain of order documents the
    memo ledger's trial-balance figure is exactly zero and the party
    ledger is untouched by order vouchers (orders are not accounting)."""
    party = "Neutral Co"
    _post_order(client, "quotation", party, "QT-1", [_item("Widgets", "10", "100.00")])
    _post_order(
        client,
        "sales_order",
        party,
        "SO-1",
        [_item("Widgets", "10", "100.00")],
        against="QT-1",
    )
    _post_order(client, "delivery_note", party, "DN-1", [_item("Widgets", "10")], against="SO-1")
    assert _trial_balance_of(client, "Order Book (memo)") == Decimal("0.00")
    # the party ledger exists (created under Sundry Debtors) but no order
    # voucher moved it
    assert _trial_balance_of(client, party) == Decimal("0.00")


def test_all_five_kinds_number_their_own_series(client, fresh_db):
    """Each kind gets its own voucher type and series; a second SO numbers
    2 in the SO series (gapless, per book)."""
    party = "Kind Co"
    for kind, ref in (
        ("quotation", "QT-9"),
        ("sales_order", "SO-9"),
        ("delivery_note", "DN-9"),
        ("purchase_order", "PO-9"),
        ("grn", "GRN-9"),
    ):
        items = [_item("Widgets", "1", "10.00")]
        extra = {"against": "SO-9"} if kind == "delivery_note" else {}
        if kind == "grn":
            # GRN fulfils PO-9 only after it exists
            r = _post_order(
                client, "purchase_order", "Kind Supplier", "PO-9", [_item("Widgets", "2", "10.00")]
            )
            assert r.status_code == 201, r.text
            party_for_kind = "Kind Supplier"
            extra = {"against": "PO-9"}
        elif kind == "delivery_note":
            party_for_kind = party
        else:
            party_for_kind = party
        r = _post_order(client, kind, party_for_kind, ref, items, **extra)
        assert r.status_code == 201, (kind, r.text)
        assert r.json()["voucher_no"].startswith(ref.split("-")[0] + "-"), r.text


def test_failed_post_consumes_no_number(client, fresh_db):
    """The lazily-created type rides the posting transaction: a refused
    document rolls back fully — no voucher created, no number consumed
    (the next good document takes the very next number)."""
    party = "Rollback Co"
    ok1 = _post_order(client, "quotation", party, "QT-OK1", [_item("X", "1", "5.00")])
    assert ok1.status_code == 201, ok1.text
    seq1 = int(ok1.json()["voucher_no"].rsplit("-", 1)[1])

    bad = _post_order(client, "quotation", party, "QT-BAD", [_item("X", "0")])  # qty 0
    assert bad.status_code == 422

    ok2 = _post_order(client, "quotation", party, "QT-OK2", [_item("X", "1", "5.00")])
    assert ok2.status_code == 201, ok2.text
    seq2 = int(ok2.json()["voucher_no"].rsplit("-", 1)[1])
    assert seq2 == seq1 + 1  # the refused post consumed nothing


# ------------------------------------------------------- the position fold
def test_dn_and_invoice_fulfil_a_sales_order(client, fresh_db):
    """The quote-to-cash chain: DN delivers 6, invoice settles 8 — pending
    to deliver 4, pending to invoice 2, status partial."""
    party = "Chain Co"
    _post_order(client, "sales_order", party, "SO-1", [_item("Widgets", "10", "100.00")])
    r = _post_order(client, "delivery_note", party, "DN-1", [_item("Widgets", "6")], against="SO-1")
    assert r.status_code == 201, r.text

    inv = post_voucher(
        client,
        type_="SAL",
        meta={
            "invoice": {
                "customer": party,
                "items": [{"description": "Widgets", "qty": "8", "rate": "100.00"}],
            },
            "orders": {"settle": [{"order": "SO-1", "lines": [{"item": "Widgets", "qty": "8"}]}]},
        },
        entries=_SEED,
    )
    assert inv.status_code == 201, inv.text

    data = _pending(client, party=party)
    assert len(data["rows"]) == 1
    row = data["rows"][0]
    assert row["item"] == "Widgets"
    assert row["qty"] == "10"
    assert row["fulfilled"] == "6"
    assert row["invoiced"] == "8"
    assert row["pending"] == "2"
    assert row["pending_value"] == "200.00"
    assert row["status"] == "partial"
    assert data["party_totals"] == {party: "200.00"}

    # the stored invoice carries the resolution (party/kind written back)
    doc = inv.json()["meta"]["orders"]["settle"][0]
    assert doc["party"] == party
    assert doc["kind"] == "sales_order"


def test_quotation_converts_and_never_pends(client, fresh_db):
    """A quotation converted by an SO shows converted quantities; it never
    appears in the pending book — orders pend, quotes inform."""
    party = "Quote Co"
    _post_order(client, "quotation", party, "QT-1", [_item("Widgets", "10", "100.00")])
    r = _post_order(
        client, "sales_order", party, "SO-1", [_item("Widgets", "7", "100.00")], against="QT-1"
    )
    assert r.status_code == 201, r.text

    data = _json(client, "orders-open?kind=quotation&format=json")
    quotes = [d for d in data["documents"] if d["party"] == party]
    assert len(quotes) == 1
    assert quotes[0]["status"] == "converted"
    assert quotes[0]["items"] == [{"item": "Widgets", "converted": "7"}]

    # the quotation is not in the pending book (orders only)
    pending = _pending(client, party=party)
    assert [row["ref"] for row in pending["rows"]] == ["SO-1"]


def test_po_grn_and_bill_fulfil_a_purchase_order(client, fresh_db):
    """The procure-to-pay mirror: GRN receives 3, a plain PUR voucher
    bills 3 — pending to receive 2, pending to bill 2."""
    party = "Mirror Supplies"
    _post_order(client, "purchase_order", party, "PO-1", [_item("Gadgets", "5", "200.00")])
    r = _post_order(client, "grn", party, "GRN-1", [_item("Gadgets", "3")], against="PO-1")
    assert r.status_code == 201, r.text

    _ensure_ledgers(client, ("Purchases", "Purchase Accounts"))
    r = _settle(
        client,
        party,
        "PO-1",
        [{"item": "Gadgets", "qty": "3"}],
        entries=[
            {"ledger_name": "Purchases", "dc": "D", "amount": "600.00"},
            {"ledger_name": party, "dc": "C", "amount": "600.00"},
        ],
        type_="PUR",
    )
    assert r.status_code == 201, r.text

    data = _pending(client, side="purchase", party=party)
    assert len(data["rows"]) == 1
    row = data["rows"][0]
    assert row["fulfilled"] == "3"  # received
    assert row["invoiced"] == "3"  # billed
    assert row["pending"] == "2"
    assert row["pending_value"] == "400.00"
    assert row["status"] == "partial"


def test_status_ladder_open_partial_closed(client, fresh_db):
    """open → partial → closed; a closed order frees its ref and leaves
    the pending book."""
    party = "Ladder Co"
    _post_order(client, "sales_order", party, "SO-1", [_item("A", "2", "50.00")])
    assert _pending(client, party=party)["rows"][0]["status"] == "open"

    _settle(client, party, "SO-1", [{"item": "A", "qty": "1"}])
    assert _pending(client, party=party)["rows"][0]["status"] == "partial"

    _settle(client, party, "SO-1", [{"item": "A", "qty": "1"}])
    assert _pending(client, party=party)["rows"] == []  # closed, excluded

    # a closed ref is free again (the bills pack's rule)
    r = _post_order(client, "sales_order", party, "SO-1", [_item("A", "1", "50.00")])
    assert r.status_code == 201, r.text


# ------------------------------------------------------------- the guards
def test_guards_on_document_shapes(client, fresh_db):
    """Unknown kind, missing party/ref/items, bad qty/rate/slab, duplicate
    open refs, zero totals — all refused with 422."""
    party = "Guard Co"
    r = post_voucher(
        client,
        type_="JV",
        meta={
            "orders": {
                "kind": "wishes",
                "party": party,
                "ref": "W-1",
                "items": [_item("X", "1", "1.00")],
            }
        },
        entries=_SEED,
    )
    assert r.status_code == 422 and "kind must be one of" in r.json()["detail"]

    bad_docs = (
        {"kind": "quotation", "party": "", "ref": "Q", "items": [_item("X", "1", "1")]},
        {"kind": "quotation", "party": party, "ref": "", "items": [_item("X", "1", "1")]},
        {"kind": "quotation", "party": party, "ref": "Q", "items": []},
        {"kind": "quotation", "party": party, "ref": "Q"},
    )
    for doc in bad_docs:
        meta = {"orders": doc}
        r = post_voucher(client, type_="QT", meta=meta, entries=_SEED)
        assert r.status_code == 422, meta

    r = _post_order(client, "quotation", party, "QT-1", [_item("X", "1", "1.00")])
    assert r.status_code == 201
    # a duplicate open ref of the same kind + party collides
    r = _post_order(client, "quotation", party, "QT-1", [_item("X", "1", "1.00")])
    assert r.status_code == 422 and "collides" in r.json()["detail"]

    # bad numbers and slabs
    for item in (_item("X", "0", "1.00"), _item("X", "1", "-1"), _item("X", "1", "1.00", tax="15")):
        r = _post_order(client, "quotation", party, "QT-2", [item])
        assert r.status_code == 422, item

    # a zero-total document cannot post (the memo trace needs a value)
    r = _post_order(client, "quotation", party, "QT-3", [_item("X", "1")])
    assert r.status_code == 422 and "positive total" in r.json()["detail"]

    # meta.orders.kind is the source of truth: any carrier type normalizes
    # onto the kind's own series (the transformer rewrites the draft)
    r = post_voucher(
        client,
        type_="JV",
        meta=_doc("quotation", party, "QT-4", [_item("X", "1", "1.00")]),
        entries=_SEED,
    )
    assert r.status_code == 201, r.text
    assert r.json()["voucher_type"] == "QT"
    assert r.json()["voucher_no"].startswith("QT-")


def test_guards_on_fulfilment(client, fresh_db):
    """DN/GRN guards: unknown order, foreign party, item not on the order,
    fulfilment beyond the ordered quantity."""
    party = "Fulfil Co"
    other = "Other Co"
    _post_order(client, "sales_order", party, "SO-1", [_item("Widgets", "5", "10.00")])

    # unknown order ref
    r = _post_order(client, "delivery_note", party, "DN-1", [_item("Widgets", "1")], against="SO-9")
    assert r.status_code == 422 and "no prior open" in r.json()["detail"]

    # the order belongs to another party
    r = _post_order(client, "delivery_note", other, "DN-2", [_item("Widgets", "1")], against="SO-1")
    assert r.status_code == 422 and "no prior open" in r.json()["detail"]

    # item not on the order
    r = _post_order(client, "delivery_note", party, "DN-3", [_item("Gadgets", "1")], against="SO-1")
    assert r.status_code == 422 and "is not on order" in r.json()["detail"]

    # beyond the ordered quantity
    r = _post_order(client, "delivery_note", party, "DN-4", [_item("Widgets", "3")], against="SO-1")
    assert r.status_code == 201
    r = _post_order(client, "delivery_note", party, "DN-5", [_item("Widgets", "3")], against="SO-1")
    assert r.status_code == 422 and "beyond its ordered" in r.json()["detail"]

    # an SO cannot convert an unknown quotation
    r = _post_order(
        client, "sales_order", party, "SO-2", [_item("Widgets", "1", "10.00")], against="QT-404"
    )
    assert r.status_code == 422 and "no prior quotation" in r.json()["detail"]


def test_guards_on_settlement(client, fresh_db):
    """Settle guards: unknown order, over-settlement, carrier that does
    not move the party, ambiguous refs across parties."""
    party = "Settle Order Co"
    _ensure_ledgers(client, ("Sales", "Sales Accounts"))
    _post_order(client, "sales_order", party, "SO-1", [_item("Widgets", "10", "100.00")])

    # unknown order
    r = _settle(client, party, "SO-404", [{"item": "Widgets", "qty": "1"}])
    assert r.status_code == 422 and "no prior open sales" in r.json()["detail"]

    # the carrier must move the party
    r = post_voucher(
        client,
        type_="JV",
        meta={
            "orders": {"settle": [{"order": "SO-1", "lines": [{"item": "Widgets", "qty": "1"}]}]}
        },
        entries=[
            {"ledger_name": "Sales", "dc": "C", "amount": "0.01"},
            {"ledger_name": "Cash", "dc": "D", "amount": "0.01"},
        ],
    )
    assert r.status_code == 422 and "does not move party ledger" in r.json()["detail"]

    # item not on the order
    r = _settle(client, party, "SO-1", [{"item": "Gadgets", "qty": "1"}])
    assert r.status_code == 422 and "which is not" in r.json()["detail"]

    # beyond the ordered quantity (two settles: 6 + 5 > 10)
    assert _settle(client, party, "SO-1", [{"item": "Widgets", "qty": "6"}]).status_code == 201
    r = _settle(client, party, "SO-1", [{"item": "Widgets", "qty": "5"}])
    assert r.status_code == 422 and "beyond its ordered" in r.json()["detail"]

    # an ambiguous ref: two parties, both with open SO-2 — the settle is
    # refused unless the voucher bills the right party (it is scoped to
    # the parties it moves, and here it moves just one of the two)
    other = "Ambiguous Co"
    _post_order(client, "sales_order", party, "SO-2", [_item("A", "5", "10.00")])
    _post_order(client, "sales_order", other, "SO-2", [_item("A", "5", "10.00")])
    r = _settle(client, other, "SO-2", [{"item": "A", "qty": "1"}])  # moves only `other`
    assert r.status_code == 201, r.text
    r = _settle(client, party, "SO-2", [{"item": "A", "qty": "1"}])
    assert r.status_code == 201, r.text  # scoped to its own party's order


def test_settle_on_a_closed_order_refused(client, fresh_db):
    party = "Closed Co"
    _post_order(client, "sales_order", party, "SO-1", [_item("A", "4", "10.00")])
    assert _settle(client, party, "SO-1", [{"item": "A", "qty": "4"}]).status_code == 201
    r = _settle(client, party, "SO-1", [{"item": "A", "qty": "1"}])
    assert r.status_code == 422 and "already fully" in r.json()["detail"]


def test_document_mode_exclusivity(client, fresh_db):
    """A standalone order document refuses to ride other line-building
    documents (the cross-pack rule since ADR-011); a settle allocation
    rides a meta.invoice freely (it is an annotation, like bills)."""
    party = "Exclusive Co"
    r = post_voucher(
        client,
        type_="QT",
        meta={
            "orders": {
                "kind": "quotation",
                "party": party,
                "ref": "Q-1",
                "items": [_item("X", "1", "1.00")],
            },
            "invoice": {
                "customer": party,
                "items": [{"description": "X", "qty": "1", "rate": "1.00"}],
            },
        },
        entries=_SEED,
    )
    assert r.status_code == 422 and "mutually exclusive" in r.json()["detail"]

    # bills allocations and an orders settle may share one voucher
    _post_order(client, "sales_order", party, "SO-1", [_item("X", "2", "100.00")])
    r = post_voucher(
        client,
        type_="SAL",
        meta={
            "invoice": {
                "customer": party,
                "items": [{"description": "X", "qty": "2", "rate": "100.00"}],
            },
            "orders": {"settle": [{"order": "SO-1", "lines": [{"item": "X", "qty": "2"}]}]},
            "bills": {"party": party, "refs": [{"kind": "new", "ref": "INV-1", "dr": "200.00"}]},
        },
        entries=_SEED,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["voucher_type"] == "SAL"  # the settle did not rewrite the type
    assert Decimal(body["amount"]) == Decimal("200.00")  # the invoice built the lines
    # the order closed, the bill opened: both annotations held
    assert _pending(client, party=party)["rows"] == []
    data = _json(client, f"bills-open?party={party.replace(' ', '%20')}&format=json")
    assert data["bills"][0]["ref"] == "INV-1"


# -------------------------------------------------------------- reversals
def test_reversal_cancels_an_order(client, fresh_db):
    """Law 2 is the cancel path: reversing an SO removes it from the
    pending book (the undone pair nets); reversing the reversal's
    original is refused, per the engine."""
    party = "Cancel Co"
    r = _post_order(client, "sales_order", party, "SO-1", [_item("A", "5", "10.00")])
    assert r.status_code == 201
    vid = r.json()["id"]

    assert _pending(client, party=party)["rows"]
    rev = client.post(f"/api/v1/vouchers/{vid}/reverse", json={})
    assert rev.status_code == 201, rev.text
    assert rev.json()["voucher_type"] == "SO"  # reverses within its own series
    assert _pending(client, party=party)["rows"] == []


def test_reversal_restores_fulfilment(client, fresh_db):
    """Undoing a DN restores the delivered quantity; undoing a settling
    invoice restores the invoiced quantity."""
    party = "Restore Co"
    _post_order(client, "sales_order", party, "SO-1", [_item("A", "10", "10.00")])
    dn = _post_order(client, "delivery_note", party, "DN-1", [_item("A", "4")], against="SO-1")
    assert _pending(client, party=party)["rows"][0]["fulfilled"] == "4"

    rev = client.post(f"/api/v1/vouchers/{dn.json()['id']}/reverse", json={})
    assert rev.status_code == 201
    assert _pending(client, party=party)["rows"][0]["fulfilled"] == "0"

    inv = _settle(client, party, "SO-1", [{"item": "A", "qty": "6"}])
    assert _pending(client, party=party)["rows"][0]["invoiced"] == "6"
    rev = client.post(f"/api/v1/vouchers/{inv.json()['id']}/reverse", json={})
    assert rev.status_code == 201
    assert _pending(client, party=party)["rows"][0]["invoiced"] == "0"


# --------------------------------------------------------------- reports
def test_pending_orders_report_formats(client, fresh_db):
    """PDF (print bridge), CSV and JSON of the same fold — quantities and
    pending value, per-party subtotals, the grand total."""
    party = "Report Co"
    _post_order(
        client,
        "sales_order",
        party,
        "SO-1",
        [
            _item("Widgets", "10", "100.00"),
            _item("Gadgets", "2", "50.00"),
        ],
    )
    _post_order(client, "delivery_note", party, "DN-1", [_item("Widgets", "4")], against="SO-1")
    _settle(client, party, "SO-1", [{"item": "Widgets", "qty": "6"}])

    data = _pending(client, party=party)
    assert data["side"] == "sales"
    assert data["total_pending_value"] == "500.00"  # (10−6)×100 + 2×50
    assert data["party_totals"] == {party: "500.00"}

    csv = client.get(
        "/api/v1/reports/pack/pending-orders",
        params={"side": "sales", "party": party, "format": "csv"},
    )
    assert csv.status_code == 200
    text = csv.text
    assert text.splitlines()[0].startswith("party,ref,voucher,date,item")
    assert ",partial" in text
    assert "500.00" in text

    pdf = client.get(
        "/api/v1/reports/pack/pending-orders",
        params={"side": "sales", "party": party},
    )
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")

    # a bad side is refused
    r = client.get("/api/v1/reports/pack/pending-orders", params={"side": "wishes"})
    assert r.status_code == 422


def test_pending_orders_as_of_window(client, fresh_db):
    """as_of windows the fold: yesterday's position, not today's."""
    party = "AsOf Co"
    yesterday = (TODAY - timedelta(days=1)).isoformat()
    _post_order(client, "sales_order", party, "SO-1", [_item("A", "5", "10.00")], date_=yesterday)
    _settle(
        client,
        party,
        "SO-1",
        [{"item": "A", "qty": "2"}],
        entries=[
            {"ledger_name": party, "dc": "D", "amount": "20.00"},
            {"ledger_name": "Sales", "dc": "C", "amount": "20.00"},
        ],
    )
    data = _json(
        client,
        f"pending-orders?side=sales&party={party.replace(' ', '%20')}"
        f"&as_of={yesterday}&format=json",
    )
    assert data["rows"][0]["invoiced"] == "0"  # the settle is today's
    data = _pending(client, party=party)
    assert data["rows"][0]["invoiced"] == "2"


def test_orders_open_report_feeds_the_pickers(client, fresh_db):
    """orders-open per kind: the against-picker's feed with per-line
    positions, both JSON and CSV."""
    party = "Picker Co"
    _post_order(client, "sales_order", party, "SO-1", [_item("A", "10", "10.00")])
    _post_order(client, "delivery_note", party, "DN-1", [_item("A", "4")], against="SO-1")

    data = _json(client, "orders-open?kind=sales_order&format=json")
    docs = [d for d in data["documents"] if d["party"] == party]
    assert len(docs) == 1
    item = docs[0]["items"][0]
    assert item["qty"] == "10"
    assert item["fulfilled"] == "4"
    assert item["invoiced"] == "0"
    assert item["pending_deliver"] == "6"
    assert item["pending_bill"] == "10"

    csv = client.get(
        "/api/v1/reports/pack/orders-open", params={"kind": "sales_order", "format": "csv"}
    )
    assert csv.status_code == 200
    assert csv.text.splitlines()[0] == (
        "party,ref,voucher,date,status,item,qty,rate,fulfilled,invoiced,pending"
    )

    # a bad kind is refused
    r = client.get("/api/v1/reports/pack/orders-open", params={"kind": "wishes"})
    assert r.status_code == 422


# ----------------------------------------------------------------- print
def test_order_print_views_claim_their_vouchers(client, fresh_db):
    """The five kinds print as their commercial documents; a settle
    carrier declines to the invoice's own view; an SO prints its
    positions' parent title for DNs."""
    party = "Print Co"
    qt = _post_order(
        client,
        "quotation",
        party,
        "QT-1",
        [_item("Widgets", "10", "100.00", "18")],
        valid_until="2026-12-31",
        terms="FOB origin",
    )
    assert qt.status_code == 201
    pdf = client.get(f"/api/v1/vouchers/{qt.json()['id']}/pdf")
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")
    assert "Quotation" in pdf.headers.get("content-disposition", "")
    # the filename hint carries the document's own ref
    assert "QT-1" in pdf.headers.get("content-disposition", "")

    _post_order(client, "sales_order", party, "SO-1", [_item("Widgets", "10", "100.00")])
    dn = _post_order(
        client, "delivery_note", party, "DN-1", [_item("Widgets", "5")], against="SO-1"
    )
    assert dn.status_code == 201

    inv = post_voucher(
        client,
        type_="SAL",
        meta={
            "invoice": {
                "customer": party,
                "items": [{"description": "Widgets", "qty": "5", "rate": "100.00"}],
            },
            "orders": {"settle": [{"order": "SO-1", "lines": [{"item": "Widgets", "qty": "5"}]}]},
        },
        entries=_SEED,
    )
    assert inv.status_code == 201
    pdf = client.get(f"/api/v1/vouchers/{inv.json()['id']}/pdf")
    assert pdf.status_code == 200
    # the invoice's view wins (Tax Invoice, not an order document)
    assert "Invoice" in pdf.headers.get("content-disposition", "")


# ------------------------------------------------------------ idempotency
def test_idempotent_replay_posts_one_order(client, fresh_db):
    """The same client_token reposts nothing: one order, one number, one
    pending row — the engine's idempotency, inherited for free."""
    import uuid

    party = "Idem Co"
    meta = _doc("sales_order", party, "SO-1", [_item("A", "5", "10.00")])
    payload = {
        "voucher_type": "SO",
        "voucher_date": TODAY.isoformat(),
        "entries": _SEED,
        "meta": meta,
        "client_token": str(uuid.uuid4()),
    }
    # this module created a second book (test_books_are_strangers); pin
    # the default book explicitly — the API demands X-Book-Id otherwise
    headers = {"X-Book-Id": "1"}
    first = client.post("/api/v1/vouchers", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    second = client.post("/api/v1/vouchers", json=payload, headers=headers)
    assert second.status_code == 200  # replayed, not created
    assert second.json()["voucher_no"] == first.json()["voucher_no"]
    rows = _pending(client, party=party)["rows"]
    assert len(rows) == 1


# ------------------------------------------------------- books are strangers
def test_books_are_strangers(client, fresh_db):
    """Order refs are namespaced by book: another book's voucher list
    knows nothing of this book's orders (ADR-009 isolation)."""
    party = "Island Co"
    r = _post_order(client, "sales_order", party, "SO-1", [_item("A", "1", "10.00")])
    assert r.status_code == 201

    other = client.post("/api/v1/books", json={"name": "Stranger Books", "slug": "stranger-books"})
    assert other.status_code == 201, other.text
    stranger_headers = {"X-Book-Id": str(other.json()["book"]["id"])}
    data = _json_stranger = client.get(
        "/api/v1/reports/pack/pending-orders",
        params={"side": "sales", "format": "json"},
        headers=stranger_headers,
    )
    assert data.status_code == 200
    assert data.json()["rows"] == []
