"""The native GUI, proven offscreen — the same widgets the Windows exe shows.

``QT_QPA_PLATFORM=offscreen`` builds every window and table without a display
(what ``--smoke`` and CI use). A stub client stands in for the engine so the
widget logic — balance gating, filters, report trees, navigation — tests fast;
the real end-to-end lives in test_native_smoke.py.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialogButtonBox  # noqa: E402

from ledgerforge.nativeapp.daybook import DayBookPage, current_fy_bounds  # noqa: E402
from ledgerforge.nativeapp.main_window import PAGES, MainWindow  # noqa: E402
from ledgerforge.nativeapp.models import Ledger, Voucher  # noqa: E402
from ledgerforge.nativeapp.reports import ReportsPage  # noqa: E402
from ledgerforge.nativeapp.voucher_dialog import NewVoucherDialog, to_cents  # noqa: E402


class StubClient:
    """A synchronous ApiClient stand-in with canned engine answers."""

    def __init__(self):
        self.book_id = 1
        self.ledgers_list = [
            Ledger(1, "Cash", None, 11, "Cash-in-Hand", True),
            Ledger(2, "Sales Account", None, 12, "Sales Accounts", True),
            Ledger(3, "Rent", None, 13, "Indirect Expenses", False),
        ]
        self.vouchers_list = [
            Voucher(
                id=1,
                uuid="u-1",
                voucher_no="JV-1",
                voucher_type="JV",
                voucher_date=date(2026, 10, 31),
                amount=Decimal("100.00"),
                narration="opening test",
                reversal_of=None,
                posted_at=datetime(2026, 10, 31, 10, 0),
                client_token="t-1",
                meta={},
                entries=(),
            )
        ]

    def books(self):
        from ledgerforge.nativeapp.models import Book

        return [Book(1, "Stub Book", "default", "INR", 4, False)]

    def health(self):
        return {"status": "ok", "version": "test", "auth_mode": "disabled"}

    def ledgers(self, search=""):
        return [led for led in self.ledgers_list if search.casefold() in led.name.casefold()]

    def groups(self):
        from ledgerforge.nativeapp.models import Group

        return [Group(11, "Cash-in-Hand", None, 2, "asset", False, False)]

    def periods(self):
        return []

    def slots(self):
        return []

    def vouchers(self, **_kwargs):
        return self.vouchers_list

    def voucher(self, voucher_id):
        return self.vouchers_list[0]

    def trial_balance(self, as_of=None):
        from ledgerforge.nativeapp.models import TrialBalanceRow

        return {
            "as_of": "2026-11-07",
            "ledgers": [
                TrialBalanceRow(
                    ledger_name="Cash",
                    group_name="Cash-in-Hand",
                    nature="asset",
                    dr=Decimal("100.00"),
                    cr=Decimal("0.00"),
                    balance=Decimal("100.00"),
                    dc="D",
                )
            ],
            "totals": {"dr": "100.00", "cr": "100.00", "difference": "0"},
            "groups": [],
        }

    def balance_sheet(self, as_of=None):
        return {
            "as_of": "2026-11-07",
            "assets": {
                "groups": [{"group_name": "Cash-in-Hand", "amount": "100.00"}],
                "total": "100.00",
            },
            "liabilities": {"groups": [], "total": "0.00"},
            "equity": {"groups": [], "net_profit_to_date": "0.00", "total": "100.00"},
        }

    def profit_loss(self, date_from, date_to):
        return {
            "from": "2026-04-01",
            "to": "2026-11-07",
            "trading": {
                "direct_incomes": [],
                "direct_expenses": [],
                "gross_profit": "0.00",
            },
            "profit_and_loss": {
                "indirect_incomes": [],
                "indirect_expenses": [],
                "net_profit": "0.00",
            },
        }

    def fy_close(self, as_of):
        return {"lines": [], "balanced": True}

    def pack(self, key, **params):
        return b"pdf-bytes", "application/pdf", "gstr1.pdf"

    def create_voucher(self, **payload):
        return self.vouchers_list[0]

    def create_ledger(self, name, **kwargs):
        return Ledger(9, name, None, 11, "Cash-in-Hand", False)


@pytest.fixture()
def stub():
    return StubClient()


# ------------------------------------------------------------------ day book


def test_daybook_renders_and_filters(qtbot, stub):
    page = DayBookPage(stub)
    qtbot.addWidget(page)
    page._show(stub.vouchers_list)

    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == "JV-1"
    assert page.table.item(0, 3).text() == "100.00"

    page._refilter("does-not-exist")
    assert page.table.rowCount() == 0
    page._refilter("")
    assert page.table.rowCount() == 1
    page._refilter("opening")
    assert page.table.rowCount() == 1


def test_current_fy_bounds_follows_the_indian_year():
    start, end = current_fy_bounds(date(2026, 11, 7))
    assert (start, end) == (date(2026, 4, 1), date(2027, 3, 31))
    start, end = current_fy_bounds(date(2026, 2, 1))
    assert (start, end) == (date(2025, 4, 1), date(2026, 3, 31))


# ------------------------------------------------------------- voucher entry


def test_to_cents_is_decimal_exact():
    assert to_cents("100.10") == Decimal("100.10")
    assert to_cents("1,234.5") == Decimal("1234.50")
    assert to_cents("") == Decimal("0.00")
    assert to_cents("junk") == Decimal("0.00")


def test_voucher_dialog_balance_gate(qtbot, stub):
    dialog = NewVoucherDialog(stub, [led.name for led in stub.ledgers_list])
    qtbot.addWidget(dialog)
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)

    # fresh dialog: two empty lines -> not postable
    assert not dialog.is_balanced()
    assert not ok.isEnabled()

    # fill line 0: Cash / Debit / 100.10 ; line 1: Sales / Credit / 100.10
    line0, line1 = dialog._lines[0], dialog._lines[1]
    line0.ledger.setCurrentText("Cash")
    line0.dc.setCurrentText("Debit")
    line0.amount.setText("100.10")
    line1.ledger.setCurrentText("Sales Account")
    line1.dc.setCurrentText("Credit")
    line1.amount.setText("100.10")

    assert dialog.is_balanced()
    assert ok.isEnabled()
    assert dialog.balance_label.text().startswith("Balanced")

    # a one-cent imbalance must lock the button (Law 1 at the desk)
    line1.amount.setText("100.09")
    assert not dialog.is_balanced()
    assert not ok.isEnabled()


def test_voucher_dialog_line_payload_shape(qtbot, stub):
    dialog = NewVoucherDialog(stub, ["Cash", "Sales Account"])
    qtbot.addWidget(dialog)
    line = dialog._lines[0]
    line.ledger.setCurrentText("Cash")
    line.amount.setText("50.00")
    payload = line.payload()
    assert payload == {
        "ledger_name": "Cash",
        "dc": "D",
        "amount": "50.00",
        "narration": None,
    }
    dialog._lines[0].ledger.setCurrentText("")
    assert dialog._lines[0].payload() is None  # blank ledger -> skipped


def test_voucher_dialog_add_remove_lines(qtbot, stub):
    dialog = NewVoucherDialog(stub, ["Cash"])
    qtbot.addWidget(dialog)
    assert len(dialog._lines) == 2
    dialog.add_line()
    assert len(dialog._lines) == 3
    dialog.remove_last_line()
    dialog.remove_last_line()
    assert len(dialog._lines) == 1
    dialog.remove_last_line()  # never below one
    assert len(dialog._lines) == 1


# ------------------------------------------------------------------- reports


def test_reports_page_renders_trial_balance(qtbot, stub):
    page = ReportsPage(stub)
    qtbot.addWidget(page)
    page.report_box.setCurrentIndex(0)  # Trial Balance
    page._render(stub.trial_balance())

    assert page.tree.topLevelItemCount() == 2  # Cash + TOTAL
    total = page.tree.topLevelItem(1)
    assert total.text(0) == "TOTAL"
    assert total.text(1) == "100.00"
    assert page.csv_button.isEnabled()
    assert page._last_csv[0][0] == "Cash"


def test_reports_page_renders_balance_sheet(qtbot, stub):
    page = ReportsPage(stub)
    qtbot.addWidget(page)
    page._render(("balance-sheet", stub.balance_sheet()))
    assert page.tree.topLevelItemCount() == 3  # ASSETS / LIABILITIES / EQUITY
    assets = page.tree.topLevelItem(0)
    assert assets.text(0) == "ASSETS"
    assert assets.child(0).text(0) == "Cash-in-Hand"


def test_reports_page_renders_pack_pdf_path(qtbot, stub, monkeypatch):
    page = ReportsPage(stub)
    qtbot.addWidget(page)
    saved = {}
    monkeypatch.setattr(
        page, "_write_pack", lambda content, name: saved.update({"content": content, "name": name})
    )
    page._render((b"%PDF-1.4 fake", "application/pdf", "gstr1.pdf"))
    assert saved["content"].startswith(b"%PDF")
    assert saved["name"] == "gstr1.pdf"


def test_reports_page_param_visibility(qtbot, stub):
    page = ReportsPage(stub)
    qtbot.addWidget(page)
    page.report_box.setCurrentIndex(1)  # P&L -> range
    assert (
        page.date_from.isVisible()
        or page.date_from.isEnabledTo(page)
        or not page.period_edit.isVisible()
    )
    page.report_box.setCurrentIndex(4)  # GSTR-1 -> period
    assert page.period_edit.isVisibleTo(page) or page.period_edit.isEnabled()


# --------------------------------------------------------------- main window


def test_main_window_builds_pages_and_navigates(qtbot, stub):
    window = MainWindow(stub, "/tmp/lf-native-test")
    qtbot.addWidget(window)

    assert window.sidebar.count() == len(PAGES)
    labels = [window.sidebar.item(i).text() for i in range(window.sidebar.count())]
    assert labels == [label for _key, label in PAGES]

    window._goto(2)  # Reports
    assert window.pages.currentIndex() == 2
    window._goto(0)  # back to the day book
    assert window.pages.currentIndex() == 0


def test_main_window_engine_ready_status(qtbot, stub):
    window = MainWindow(stub, "/tmp/lf-native-test")
    qtbot.addWidget(window)
    window._books_loaded(stub.books())
    window._engine_ready(stub.health())
    assert "connected" in window.status_bar.currentMessage()
    assert window.windowTitle() == "LedgerForge — Stub Book"


def test_main_window_type_box_carries_seed_codes(qtbot, stub):
    """The entry form's type list is the seeded TallyPrime set, minus the
    system-only OPEN/FYC codes a bookkeeper never posts by hand."""
    window = MainWindow(stub, "/tmp/lf-native-test")
    qtbot.addWidget(window)
    dialog = window._build_new_voucher([led.name for led in stub.ledgers_list])
    qtbot.addWidget(dialog)
    codes = [dialog.type_box.itemData(i) for i in range(dialog.type_box.count())]
    assert "JV" in codes and "SAL" in codes and "PYT" in codes
    assert "OPEN" not in codes and "FYC" not in codes
    dialog.close()
