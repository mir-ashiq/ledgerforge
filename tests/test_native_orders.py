"""The native order surfaces, proven offscreen — ADR-024, Phase 3.

The Orders page (kind filter, party search, the status column from the
pending fold), the New Order dialog (the five kinds, the items grid, the
against-picker fed by ``orders-open``), the order-allocation picker on the
voucher form (``meta.orders.settle`` armed on the post), and the Reports
page's pending-orders pair. A stub client stands in for the engine, the
same discipline as ``test_native_gui``; the real end-to-end is
``test_native_smoke``.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLineEdit  # noqa: E402
from test_native_gui import StubClient  # noqa: E402

from ledgerforge.nativeapp.models import Voucher  # noqa: E402
from ledgerforge.nativeapp.order_dialog import (  # noqa: E402
    NewOrderDialog,
    OrderAllocationDialog,
)
from ledgerforge.nativeapp.orders import OrdersPage  # noqa: E402
from ledgerforge.nativeapp.reports import PDF_PACKS, REPORTS, ReportsPage  # noqa: E402
from ledgerforge.nativeapp.voucher_dialog import NewVoucherDialog  # noqa: E402


def _order_voucher(
    vid: int,
    no: str,
    vtype: str,
    day: date,
    kind: str,
    party: str,
    ref: str,
    amount: str,
) -> Voucher:
    return Voucher(
        id=vid,
        uuid=f"u-{vid}",
        voucher_no=no,
        voucher_type=vtype,
        voucher_date=day,
        amount=Decimal(amount),
        narration=f"{kind} {ref}",
        reversal_of=None,
        posted_at=datetime(2026, 11, 7, 10, 0),
        client_token=f"t-{vid}",
        meta={"orders": {"kind": kind, "party": party, "ref": ref}},
        entries=(),
    )


class OrdersStubClient(StubClient):
    """The engine's answers for the order surfaces: five documents in the
    voucher lists, a pending fold with one partial SO, and the picker feed."""

    def __init__(self):
        super().__init__()
        day = date(2026, 11, 7)
        self.order_vouchers = [
            _order_voucher(
                1, "QT-2026-27-00001", "QT", day, "quotation", "Acme Traders", "QT-1", "500.00"
            ),
            _order_voucher(
                2, "SO-2026-27-00001", "SO", day, "sales_order", "Acme Traders", "SO-1", "500.00"
            ),
            _order_voucher(
                3, "DN-2026-27-00001", "DN", day, "delivery_note", "Acme Traders", "DN-1", "300.00"
            ),
            _order_voucher(
                4,
                "PO-2026-27-00001",
                "PO",
                day,
                "purchase_order",
                "Omega Supplies",
                "PO-1",
                "900.00",
            ),
            _order_voucher(
                5, "GRN-2026-27-00001", "GRN", day, "grn", "Omega Supplies", "GRN-1", "400.00"
            ),
        ]

    def vouchers(self, type_code="", **_kwargs):
        return [v for v in self.order_vouchers if not type_code or v.voucher_type == type_code]

    def pack_json(self, key, **params):
        if key == "pending-orders":
            if params.get("side") == "purchase":
                return {
                    "side": "purchase",
                    "rows": [
                        {
                            "party": "Omega Supplies",
                            "ref": "PO-1",
                            "voucher_no": "PO-2026-27-00001",
                            "date": "2026-11-07",
                            "item": "Gadgets",
                            "qty": "5",
                            "rate": "180.00",
                            "fulfilled": "2",
                            "invoiced": "2",
                            "pending": "3",
                            "pending_value": "540.00",
                            "status": "partial",
                        }
                    ],
                }
            return {
                "side": "sales",
                "rows": [
                    {
                        "party": "Acme Traders",
                        "ref": "SO-1",
                        "voucher_no": "SO-2026-27-00001",
                        "date": "2026-11-07",
                        "item": "Widgets",
                        "qty": "10",
                        "rate": "50.00",
                        "fulfilled": "6",
                        "invoiced": "8",
                        "pending": "2",
                        "pending_value": "100.00",
                        "status": "partial",
                    }
                ],
            }
        if key == "orders-open":
            kind = params.get("kind", "sales_order")
            return {
                "kind": kind,
                "party": params.get("party"),
                "as_of": "2026-11-07",
                "documents": [
                    {
                        "party": params.get("party") or "Acme Traders",
                        "ref": "SO-1",
                        "voucher_no": "SO-2026-27-00001",
                        "date": "2026-11-07",
                        "status": "partial",
                        "items": [
                            {
                                "item": "Widgets",
                                "qty": "10",
                                "rate": "50.00",
                                "fulfilled": "6",
                                "invoiced": "8",
                                "pending_deliver": "4",
                                "pending_bill": "2",
                            }
                        ],
                    }
                ],
            }
        return super().pack_json(key, **params)


@pytest.fixture()
def stub():
    return OrdersStubClient()


# --------------------------------------------------------------- orders page
def test_orders_page_renders_documents_and_status(qtbot, stub):
    page = OrdersPage(stub)
    qtbot.addWidget(page)
    page._loaded(stub.order_vouchers)
    page._status_loaded_sales(stub.pack_json("pending-orders", side="sales"))
    page._status_loaded_purchase(stub.pack_json("pending-orders", side="purchase"))

    assert page.table.rowCount() == 5

    # the SO row carries the fold's live status
    def _row_of(voucher_no: str) -> int:
        return next(
            r for r in range(page.table.rowCount()) if page.table.item(r, 0).text() == voucher_no
        )

    so_row = _row_of("SO-2026-27-00001")
    assert page.table.item(so_row, 3).text() == "Acme Traders"
    assert page.table.item(so_row, 4).text() == "SO-1"
    assert page.table.item(so_row, 6).text() == "partial"

    # the kind filter narrows to the purchase chain
    page.type_filter.setCurrentIndex(4)  # PO
    page._render()
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == "PO-2026-27-00001"

    # and the party search finds the supplier
    page.type_filter.setCurrentIndex(0)
    page.search_box.setText("omega")
    page._render()
    rows = {page.table.item(r, 0).text() for r in range(page.table.rowCount())}
    assert rows == {"PO-2026-27-00001", "GRN-2026-27-00001"}


def test_orders_page_pending_button_jumps_to_reports(qtbot, stub):
    page = OrdersPage(stub)
    qtbot.addWidget(page)
    fired = []
    page.pending_report_requested.connect(fired.append)
    page.type_filter.setCurrentIndex(4)  # PO → purchase side
    page._open_pending_report()
    assert fired == ["pending-purchase"]
    page.type_filter.setCurrentIndex(2)  # SO → sales side
    page._open_pending_report()
    assert fired[-1] == "pending-sales"


# ------------------------------------------------------------ new order dialog
def test_new_order_dialog_builds_the_document(qtbot, stub, monkeypatch):
    dialog = NewOrderDialog(stub, ["Acme Traders", "Sales"], None)
    qtbot.addWidget(dialog)

    # default kind: quotation → no against picker
    assert not dialog.against_box.isVisible()
    dialog.party_edit.setText("Acme Traders")
    dialog.ref_edit.setText("QT-9")
    dialog._rows[0].item.setText("Widgets")
    dialog._rows[0].qty.setText("10")
    dialog._rows[0].rate.setText("50.00")
    dialog._recap()
    assert dialog.total_label.text() == "Subtotal 500.00"
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()

    posted: list[dict] = []
    monkeypatch.setattr(
        stub,
        "create_voucher",
        lambda **payload: (posted.append(payload), stub.order_vouchers[0])[1],
    )
    dialog._submit()
    qtbot.waitUntil(lambda: len(posted) == 1, timeout=3000)
    payload = posted[0]
    assert payload["voucher_type"] == "QT"
    doc = payload["meta"]["orders"]
    assert doc["kind"] == "quotation"
    assert doc["party"] == "Acme Traders"
    assert doc["ref"] == "QT-9"
    assert doc["items"] == [{"item": "Widgets", "qty": "10", "rate": "50"}]
    # the placeholder entry: one line, replaced by the transformer
    assert len(payload["entries"]) == 1


def test_new_order_dialog_dn_shows_against_picker(qtbot, stub):
    dialog = NewOrderDialog(stub, ["Acme Traders"], None)
    qtbot.addWidget(dialog)
    dialog.kind_box.setCurrentIndex(2)  # delivery note
    # the loader runs on the pool; wait for the combo to carry the open SO
    qtbot.waitUntil(lambda: dialog.against_box.count() > 1, timeout=3000)
    labels = [dialog.against_box.itemText(i) for i in range(dialog.against_box.count())]
    assert any("SO-1" in label for label in labels)


def test_new_order_dialog_empty_party_blocks_post(qtbot, stub):
    dialog = NewOrderDialog(stub, [], None)
    qtbot.addWidget(dialog)
    dialog._rows[0].item.setText("Widgets")
    dialog._rows[0].qty.setText("1")
    dialog._rows[0].rate.setText("10.00")
    dialog._recap()
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


# ------------------------------------------------------ order allocation dialog
def test_order_allocation_dialog_arms_settle(qtbot, stub):
    dialog = OrderAllocationDialog(stub, "Acme Traders", "sales", None)
    qtbot.addWidget(dialog)
    dialog._loaded(stub.pack_json("orders-open", kind="sales_order"))

    assert dialog.table.rowCount() == 1  # one open line
    cell = dialog.table.cellWidget(0, 5)
    assert isinstance(cell, QLineEdit)
    cell.setText("2")
    dialog._recap()
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()

    dialog._accept()
    assert dialog.settle() == [{"order": "SO-1", "lines": [{"item": "Widgets", "qty": "2"}]}]


def test_order_allocation_dialog_caps_at_pending(qtbot, stub, monkeypatch):
    dialog = OrderAllocationDialog(stub, "Acme Traders", "sales", None)
    qtbot.addWidget(dialog)
    dialog._loaded(stub.pack_json("orders-open", kind="sales_order"))

    cell = dialog.table.cellWidget(0, 5)
    cell.setText("3")  # pending to bill is 2
    warned: list[str] = []
    monkeypatch.setattr(
        "ledgerforge.nativeapp.order_dialog.QMessageBox.warning",
        lambda *_a, **_k: warned.append("capped"),
    )
    dialog._accept()
    assert warned == ["capped"]
    assert dialog.result() != QDialog.DialogCode.Accepted


# ------------------------------------------------ voucher dialog integration
def test_voucher_dialog_arms_order_settle(qtbot, stub):
    dialog = NewVoucherDialog(
        stub,
        ["Acme Traders", "Sales"],
        None,
        party_names=["Acme Traders"],
    )
    qtbot.addWidget(dialog)

    # a party line the settlement follows
    dialog._lines[0].ledger.setCurrentText("Acme Traders")
    dialog._lines[0].amount.setText("100.00")
    dialog._lines[1].ledger.setCurrentText("Sales")
    dialog._lines[1].dc.setCurrentText("Credit")
    dialog._lines[1].amount.setText("100.00")
    dialog._update_balance()
    assert dialog.is_balanced()

    # arm the settlement (the picker itself is covered above) and post:
    # the armed settle rides the payload as meta.orders
    dialog._order_party = "Acme Traders"
    dialog._order_settle = [{"order": "SO-1", "lines": [{"item": "Widgets", "qty": "2"}]}]
    dialog._submit()
    qtbot.waitUntil(lambda: bool(stub.posts), timeout=3000)
    payload = stub.posts[-1]
    assert payload["meta"]["orders"] == {
        "settle": [{"order": "SO-1", "lines": [{"item": "Widgets", "qty": "2"}]}]
    }


# ------------------------------------------------------------- reports page
def test_reports_page_carries_the_pending_orders_pair(qtbot, stub):
    page = ReportsPage(stub)
    qtbot.addWidget(page)

    keys = [page.report_box.itemData(i) for i in range(page.report_box.count())]
    assert "pending-sales" in keys
    assert "pending-purchase" in keys
    assert "pending-sales" in PDF_PACKS and "pending-purchase" in PDF_PACKS
    assert REPORTS["pending-sales"] == ("Pending Orders (sales)", "as_of")
    assert REPORTS["pending-purchase"] == ("Pending Orders (purchase)", "as_of")

    # selecting through the helper (the Orders page's jump): the pending
    # pair needs the as-of date, not a party or a range
    page.select_report("pending-purchase")
    assert page.report_box.currentData() == "pending-purchase"
    assert not page.party_edit.isVisibleTo(page)
    assert not page.date_from.isVisibleTo(page)
    assert page.as_of.isEnabled()
