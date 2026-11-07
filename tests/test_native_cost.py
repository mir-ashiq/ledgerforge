"""The native cost-centre surfaces, proven offscreen — ADR-025, Phase 4.

The split dialog (every line's allocated/remainder always visible, the
per-line split editor, the full-or-unallocated gate), the voucher form's
Ctrl+K arming (``cost_allocations`` riding the lines, stale armings dropped
honestly), the Masters page's cost tabs, and the Reports page's break-up
trio. A stub client stands in for the engine, the same discipline as
``test_native_orders``; the real end-to-end is ``test_native_smoke``.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QComboBox, QDialog, QLineEdit  # noqa: E402
from test_native_gui import StubClient  # noqa: E402

from ledgerforge.nativeapp.cost_dialog import CostAllocationDialog  # noqa: E402
from ledgerforge.nativeapp.masters import (  # noqa: E402
    MastersPage,
    NewCostCategoryDialog,
    NewCostCentreDialog,
)
from ledgerforge.nativeapp.models import CostCategory, CostCentre  # noqa: E402
from ledgerforge.nativeapp.reports import REPORTS, ReportsPage  # noqa: E402
from ledgerforge.nativeapp.voucher_dialog import NewVoucherDialog  # noqa: E402

CENTRES = [
    CostCentre(31, "Primary", None, None, None, None, True),
    CostCentre(32, "Mumbai", None, 31, 22, "Location", False),
    CostCentre(33, "Pune", None, 31, 22, "Location", False),
]
CATEGORIES = [
    CostCategory(21, "Primary", None, True),
    CostCategory(22, "Location", 21, False),
]


def _dialog(qtbot, lines):
    dialog = CostAllocationDialog(StubClient(), lines, CENTRES, CATEGORIES, None)
    qtbot.addWidget(dialog)
    return dialog


def _add_split(dialog, centre_id: int, amount: str) -> None:
    """Append one split row programmatically (Add + fill the last row)."""
    dialog._add_split_row()
    row = dialog.split_table.rowCount() - 1
    combo = dialog.split_table.cellWidget(row, 0)
    assert isinstance(combo, QComboBox)
    index = combo.findData(centre_id)
    assert index >= 0
    combo.setCurrentIndex(index)
    edit = dialog.split_table.cellWidget(row, 1)
    assert isinstance(edit, QLineEdit)
    edit.setText(amount)


# ---------------------------------------------------------------- the dialog
def test_dialog_opens_with_lines_and_remainder(qtbot):
    dialog = _dialog(
        qtbot,
        [("Rent", "D", Decimal("12000.00")), ("Cash", "C", Decimal("12000.00"))],
    )
    assert dialog.lines_table.rowCount() == 2
    # the first line is picked; its remainder starts as the full amount
    assert dialog._editing == 0
    assert dialog.remainder_label.text().startswith("Unallocated — 12,000.00")


def test_dialog_remainder_follows_splits(qtbot):
    dialog = _dialog(qtbot, [("Rent", "D", Decimal("250.00"))])
    _add_split(dialog, 32, "100.00")
    assert "150.00" in dialog.remainder_label.text()
    _add_split(dialog, 33, "150.00")
    assert dialog.remainder_label.text() == "Fully allocated"


def test_dialog_partial_split_cannot_be_armed(qtbot, monkeypatch):
    warned: list[str] = []
    monkeypatch.setattr(
        "ledgerforge.nativeapp.cost_dialog.QMessageBox.warning",
        lambda _parent, _title, text: warned.append(text),
    )
    dialog = _dialog(qtbot, [("Rent", "D", Decimal("250.00"))])
    _add_split(dialog, 32, "100.00")  # 150 short
    dialog._accept()
    # the warning explains the law and the dialog did NOT accept
    assert warned and "fully allocated" in warned[0]
    assert dialog.result() != QDialog.DialogCode.Accepted


def test_dialog_allocations_payload(qtbot):
    dialog = _dialog(qtbot, [("Rent", "D", Decimal("250.00")), ("Cash", "C", Decimal("250.00"))])
    _add_split(dialog, 32, "150.00")
    _add_split(dialog, 33, "100.00")
    dialog._accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    payload = dialog.allocations()
    assert payload == [
        [
            {"centre_id": 32, "amount": "150.00"},
            {"centre_id": 33, "amount": "100.00"},
        ],
        [],  # the credit line left unallocated, explicitly
    ]


def test_dialog_leave_unallocated(qtbot):
    dialog = _dialog(qtbot, [("Rent", "D", Decimal("250.00"))])
    _add_split(dialog, 32, "250.00")
    dialog._clear_splits()
    assert dialog.split_table.rowCount() == 0
    dialog._accept()
    assert dialog.allocations() == [[]]


# -------------------------------------------------------- the voucher form
def test_voucher_form_arms_splits_on_the_payload(qtbot):
    stub = StubClient()
    dialog = NewVoucherDialog(
        stub,
        ["Rent", "Cash"],
        None,
        party_names=[],
        cost_centres=CENTRES,
        cost_categories=CATEGORIES,
    )
    qtbot.addWidget(dialog)

    # two balanced lines: Rent 250 Dr, Cash 250 Cr
    dialog._lines[0].ledger.setCurrentText("Rent")
    dialog._lines[0].amount.setText("250.00")
    dialog.add_line()
    dialog._lines[1].ledger.setCurrentText("Cash")
    dialog._lines[1].dc.setCurrentText("Credit")
    dialog._lines[1].amount.setText("250.00")
    assert dialog.is_balanced()

    # arm a full split of the Rent line through the dialog's state directly
    dialog._cost_lines = dialog._complete_lines()
    dialog._cost_splits = [
        [{"centre_id": 32, "amount": "250.00"}],
        [],
    ]
    entries = [p for p in (line.payload() for line in dialog._lines) if p is not None]
    splits = dialog._armed_splits_for(entries)
    assert splits[0] == [{"centre_id": 32, "amount": "250.00"}]
    assert splits[1] == []

    # an edited amount drops the stale arming honestly (unallocated again)
    dialog._lines[0].amount.setText("300.00")
    entries = [p for p in (line.payload() for line in dialog._lines) if p is not None]
    assert dialog._armed_splits_for(entries)[0] == []


def test_voucher_form_submit_carries_allocations(qtbot):
    stub = StubClient()
    dialog = NewVoucherDialog(
        stub,
        ["Rent", "Cash"],
        None,
        party_names=[],
        cost_centres=CENTRES,
        cost_categories=CATEGORIES,
    )
    qtbot.addWidget(dialog)
    dialog._lines[0].ledger.setCurrentText("Rent")
    dialog._lines[0].amount.setText("250.00")
    dialog.add_line()
    dialog._lines[1].ledger.setCurrentText("Cash")
    dialog._lines[1].dc.setCurrentText("Credit")
    dialog._lines[1].amount.setText("250.00")

    dialog._cost_lines = dialog._complete_lines()
    dialog._cost_splits = [[{"centre_id": 32, "amount": "250.00"}], []]
    with qtbot.waitSignal(dialog.posted, timeout=2000):
        dialog._submit()

    assert len(stub.posts) == 1
    entries = stub.posts[0]["entries"]
    assert entries[0]["cost_allocations"] == [{"centre_id": 32, "amount": "250.00"}]
    assert "cost_allocations" not in entries[1]


# ------------------------------------------------------------- the masters
def test_masters_page_cost_tabs(qtbot):
    page = MastersPage(StubClient())
    qtbot.addWidget(page)
    page._show_centres(CENTRES)
    page._show_categories(CATEGORIES)
    assert page.centre_table.rowCount() == 3
    assert page.category_table.rowCount() == 2
    # the voucher form's feed comes from the same page
    assert [c.name for c in page.cost_centres()] == ["Primary", "Mumbai", "Pune"]
    assert page.cost_categories()[1].name == "Location"
    assert "3 cost centres · 2 categories" in page.count_label.text()


def test_new_centre_dialog_guards_empty_name(qtbot, monkeypatch):
    dialog = NewCostCentreDialog(StubClient(), CENTRES, CATEGORIES, None)
    qtbot.addWidget(dialog)
    # no name -> a warning, no post
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "ledgerforge.nativeapp.masters.QMessageBox.warning",
        lambda _parent, title, text: warned.append((title, text)),
    )
    dialog._submit()
    assert warned and "name" in warned[0][1].lower()


def test_new_category_dialog_posts(qtbot):
    stub = StubClient()
    created: list[CostCategory] = []
    stub.create_cost_category = lambda name, parent_name=None: (
        created.append(CostCategory(99, name, 21, False)) or created[-1]
    )
    dialog = NewCostCategoryDialog(stub, CATEGORIES, None)
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("Department")
    with qtbot.waitSignal(dialog.created, timeout=2000):
        dialog._submit()
    assert [c.name for c in created] == ["Department"]


# -------------------------------------------------------------- the reports
def test_reports_page_has_the_cost_trio():
    assert REPORTS["cost-summary"] == ("Cost Centre Summary", "range")
    assert REPORTS["cost-breakup"] == ("Cost Centre Break-up", "centre_range")
    assert REPORTS["cost-category"] == ("Cost Category Summary", "range")


def test_reports_page_renders_the_cost_summary(qtbot):
    page = ReportsPage(StubClient())
    qtbot.addWidget(page)
    payload = {
        "from": "2026-04-01",
        "to": "2027-03-31",
        "centres": [
            {
                "centre_id": 31,
                "centre_name": "Primary",
                "parent_id": None,
                "is_root": True,
                "category_id": None,
                "category_name": None,
                "dr": "12000",
                "cr": "0",
                "net": "12000",
            },
            {
                "centre_id": 32,
                "centre_name": "Mumbai",
                "parent_id": 31,
                "is_root": False,
                "category_id": 22,
                "category_name": "Location",
                "dr": "7000",
                "cr": "0",
                "net": "7000",
            },
        ],
        "unallocated": {"dr": "4000", "cr": "0", "net": "4000"},
    }
    page._render(("cost-summary", payload))
    # Mumbai nests under Primary; Unallocated rides at the top level
    assert page.tree.topLevelItemCount() == 2
    primary = page.tree.topLevelItem(0)
    assert primary.text(0).startswith("Primary")
    assert primary.childCount() == 1
    assert primary.child(0).text(0).startswith("Mumbai")
    assert page.csv_button.isEnabled()
    assert "whole entry mass" in page.note_label.text()


def test_reports_page_renders_the_breakup(qtbot):
    page = ReportsPage(StubClient())
    qtbot.addWidget(page)
    payload = {
        "from": "2026-04-01",
        "to": "2027-03-31",
        "centre_id": 32,
        "centre_name": "Mumbai",
        "ledgers": [
            {
                "ledger_id": 13,
                "ledger_name": "Rent",
                "group_name": "Indirect Expenses",
                "nature": "expense",
                "dr": "7000",
                "cr": "0",
                "net": "7000",
            }
        ],
        "totals": {"dr": "7000", "cr": "0", "net": "7000"},
    }
    page._render(("cost-breakup", payload))
    assert page.tree.topLevelItemCount() == 2  # Rent + TOTAL
    assert page.tree.topLevelItem(1).text(0) == "TOTAL"
    assert "Mumbai" in page.note_label.text()


def test_reports_page_renders_the_category_summary(qtbot):
    page = ReportsPage(StubClient())
    qtbot.addWidget(page)
    payload = {
        "from": "2026-04-01",
        "to": "2027-03-31",
        "categories": [
            {
                "category_id": 21,
                "category_name": "Primary",
                "parent_id": None,
                "is_root": True,
                "dr": "12000",
                "cr": "0",
                "net": "12000",
            },
            {
                "category_id": 22,
                "category_name": "Location",
                "parent_id": 21,
                "is_root": False,
                "dr": "12000",
                "cr": "0",
                "net": "12000",
            },
        ],
        "unallocated": {"dr": "4000", "cr": "0", "net": "4000"},
    }
    page._render(("cost-category", payload))
    # Location nests under Primary; Unallocated rides at the top level
    assert page.tree.topLevelItemCount() == 2
    primary = page.tree.topLevelItem(0)
    assert primary.childCount() == 1
    assert primary.child(0).text(0).startswith("Location")
