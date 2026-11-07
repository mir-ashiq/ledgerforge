"""The native cost-allocation dialog (ADR-025, Phase 4).

The where-did-it-go decision at entry time, in the native voucher form:
every complete line of the draft with its allocated-so-far and its
remainder (always visible — the roadmap's law), and for the picked line a
split editor: one row per (centre, amount). A line is either fully
allocated or left unallocated — the dialog enforces it client-side with
the same exact Decimal arithmetic the engine re-proves at posting time,
and a refusal keeps the draft intact.

Centres can be created inline (the Alt+C master-creation philosophy): a
name, a parent, a category — no detour to the Masters page mid-entry.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .client import ApiClient
from .models import CostCategory, CostCentre
from .tasks import run
from .widgets import error_detail

CENT = Decimal("0.01")


def to_cents(raw: str) -> Decimal:
    """A typed amount -> Decimal cents; invalid text -> 0.00 (never floats)."""
    try:
        return Decimal(raw.replace(",", "").strip() or "0").quantize(CENT, rounding=ROUND_HALF_UP)
    except ArithmeticError:
        return Decimal("0.00")


class CostAllocationDialog(QDialog):
    """Split the draft's lines across cost centres.

    ``lines`` is the draft's complete lines as (ledger_name, dc, amount);
    ``allocations()`` answers a parallel list of per-line centre splits —
    ``[{"centre_id": int, "amount": "123.45"}, ...]`` — riding each line's
    ``cost_allocations`` on the post (ADR-025).
    """

    def __init__(
        self,
        client: ApiClient,
        lines: list[tuple[str, str, Decimal]],
        centres: list[CostCentre],
        categories: list[CostCategory],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._client = client
        self._lines = lines
        self._centres = [c for c in centres if not c.is_root]
        self._categories = [c for c in categories if not c.is_root]
        #: per line: list of (centre_id, centre_name, amount) — the draft's
        #: armed splits, edited in place
        self._splits: list[list[tuple[int, str, Decimal]]] = [[] for _ in lines]

        self.setWindowTitle("Cost centres")
        self.setModal(True)
        self.setMinimumSize(760, 520)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Every line is <b>fully allocated or left unallocated</b> — the "
                "remainder is always visible. The engine re-proves each split at "
                "posting time (exact decimals, no rounding)."
            )
        )

        # ------------------------------------------------ the draft's lines
        self.lines_table = QTableWidget(len(lines), 5)
        self.lines_table.setHorizontalHeaderLabels(
            ["Ledger", "D/C", "Amount", "Allocated", "Remainder"]
        )
        self.lines_table.verticalHeader().setVisible(False)
        self.lines_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.lines_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.lines_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.lines_table.setColumnWidth(0, 280)
        for row, (name, dc, amount) in enumerate(lines):
            self.lines_table.setItem(row, 0, QTableWidgetItem(name))
            self.lines_table.setItem(row, 1, QTableWidgetItem("Debit" if dc == "D" else "Credit"))
            self.lines_table.setItem(row, 2, QTableWidgetItem(f"{amount:,.2f}"))
            self.lines_table.setItem(row, 3, QTableWidgetItem("0.00"))
            self.lines_table.setItem(row, 4, QTableWidgetItem(f"{amount:,.2f}"))
        self.lines_table.currentCellChanged.connect(self._load_split_editor)
        layout.addWidget(self.lines_table, stretch=2)

        # --------------------------------------------- the split editor
        layout.addWidget(QLabel("Splits for the picked line — one row per centre"))
        self.split_table = QTableWidget(0, 2)
        self.split_table.setHorizontalHeaderLabels(["Cost centre", "Amount"])
        self.split_table.verticalHeader().setVisible(False)
        header = self.split_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.split_table.setColumnWidth(1, 140)
        self.split_table.setMinimumHeight(120)
        layout.addWidget(self.split_table, stretch=1)

        split_buttons = QHBoxLayout()
        self.add_button = QPushButton("Add split (+)")
        self.add_button.clicked.connect(self._add_split_row)
        self.remove_button = QPushButton("Remove split (−)")
        self.remove_button.clicked.connect(self._remove_split_row)
        self.clear_button = QPushButton("Leave line unallocated")
        self.clear_button.clicked.connect(self._clear_splits)
        split_buttons.addWidget(self.add_button)
        split_buttons.addWidget(self.remove_button)
        split_buttons.addWidget(self.clear_button)
        split_buttons.addStretch(1)
        self.remainder_label = QLabel()
        self.remainder_label.setStyleSheet("color: #52607a;")
        split_buttons.addWidget(self.remainder_label)
        layout.addLayout(split_buttons)

        # --------------------------------------------- inline centre creation
        new_box = QHBoxLayout()
        new_box.addWidget(QLabel("New centre:"))
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("e.g. Mumbai branch")
        new_box.addWidget(self.new_name)
        self.new_parent = QComboBox()
        for centre in centres:
            self.new_parent.addItem(centre.name, centre.name)
        new_box.addWidget(self.new_parent)
        self.new_category = QComboBox()
        for category in categories:
            if not category.is_root:
                self.new_category.addItem(category.name, category.name)
        new_box.addWidget(self.new_category)
        self.create_button = QPushButton("Create (Alt+C)")
        self.create_button.clicked.connect(self._create_centre)
        new_box.addWidget(self.create_button)
        new_box.addStretch(1)
        layout.addLayout(new_box)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Arm allocations")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        if lines:
            self.lines_table.selectRow(0)
        else:
            self._load_split_editor(-1, -1)

    # ---------------------------------------------------------- the editor
    def _current(self) -> int:
        return max(self.lines_table.currentRow(), 0)

    def _load_split_editor(self, row: int, _col: int = -1) -> None:
        """Show the picked line's splits; stash any edits on the old row."""
        self._sync_split_editor()
        self._editing = row if row >= 0 else 0
        self.split_table.setRowCount(0)
        for _centre_id, _name, amount in self._splits[self._editing]:
            self._append_split_row("", f"{amount:,.2f}")
        self._update_remainder()

    def _append_split_row(self, centre_name: str, amount: str) -> None:
        row = self.split_table.rowCount()
        self.split_table.insertRow(row)
        combo = QComboBox()
        for centre in self._centres:
            combo.addItem(centre.label, centre.id)
        if centre_name:
            # match by label text (the split's stashed centre name)
            for i in range(combo.count()):
                if centre_name in combo.itemText(i):
                    combo.setCurrentIndex(i)
                    break
        combo.currentIndexChanged.connect(lambda _i: self._update_remainder())
        self.split_table.setCellWidget(row, 0, combo)
        edit = QLineEdit(amount)
        edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        edit.textChanged.connect(lambda _t: self._update_remainder())
        self.split_table.setCellWidget(row, 1, edit)
        self.split_table.setRowHeight(row, 30)

    def _add_split_row(self) -> None:
        if not self._centres:
            QMessageBox.information(
                self,
                "Cost centres",
                "No cost centre exists yet — create one below (a name, a parent, "
                "a category) or on the Masters page first.",
            )
            return
        remainder = self._line_amount() - self._editor_total()
        self._append_split_row(
            self._centres[0].label, f"{remainder.quantize(CENT):f}" if remainder > 0 else "0.00"
        )
        self._update_remainder()

    def _remove_split_row(self) -> None:
        row = self.split_table.currentRow()
        if row < 0 and self.split_table.rowCount():
            row = self.split_table.rowCount() - 1
        if row >= 0:
            self.split_table.removeRow(row)
        self._update_remainder()

    def _clear_splits(self) -> None:
        """The explicit 'leave unallocated' — the roadmap's other honest state."""
        self.split_table.setRowCount(0)
        self._update_remainder()

    # ------------------------------------------------------------ arithmetic
    def _line_amount(self) -> Decimal:
        index = self._editing
        if 0 <= index < len(self._lines):
            return self._lines[index][2]
        return Decimal("0.00")

    def _editor_total(self) -> Decimal:
        total = Decimal("0.00")
        for row in range(self.split_table.rowCount()):
            edit = self.split_table.cellWidget(row, 1)
            if isinstance(edit, QLineEdit):
                total += to_cents(edit.text())
        return total.quantize(CENT)

    def _update_remainder(self) -> None:
        amount = self._line_amount()
        allocated = self._editor_total()
        remainder = (amount - allocated).quantize(CENT)
        if remainder == 0:
            self.remainder_label.setText("Fully allocated")
            self.remainder_label.setStyleSheet("color: #1a7f37;")
        elif allocated == 0:
            self.remainder_label.setText(f"Unallocated — {remainder:,.2f}")
            self.remainder_label.setStyleSheet("color: #52607a;")
        else:
            self.remainder_label.setText(f"Remainder — {remainder:,.2f}")
            self.remainder_label.setStyleSheet("color: #b42318;")

    def _sync_split_editor(self) -> None:
        """Stash the editor's rows back into ``_splits`` for the edited line."""
        if not hasattr(self, "_editing"):
            return
        splits: list[tuple[int, str, Decimal]] = []
        for row in range(self.split_table.rowCount()):
            combo = self.split_table.cellWidget(row, 0)
            edit = self.split_table.cellWidget(row, 1)
            if not isinstance(combo, QComboBox) or not isinstance(edit, QLineEdit):
                continue
            amount = to_cents(edit.text())
            if amount <= 0 or combo.currentData() is None:
                continue
            splits.append((int(combo.currentData()), combo.currentText(), amount))
        if 0 <= self._editing < len(self._splits):
            self._splits[self._editing] = splits

    # ------------------------------------------------------ centre creation
    def _create_centre(self) -> None:
        name = self.new_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Cost centres", "The new centre needs a name.")
            return
        parent = self.new_parent.currentData() or "Primary"
        category = self.new_category.currentData()

        def create() -> CostCentre:
            return self._client.create_cost_centre(name, parent_name=parent, category_name=category)

        run(
            create,
            self._centre_created,
            lambda error: QMessageBox.critical(
                self, "Cost centres", f"The engine refused:\n{error_detail(error)}"
            ),
        )

    def _centre_created(self, centre: CostCentre) -> None:
        self._centres.append(centre)
        self.new_name.clear()
        # refresh every centre combo with the new entry
        for row in range(self.split_table.rowCount()):
            combo = self.split_table.cellWidget(row, 0)
            if isinstance(combo, QComboBox):
                blocked = combo.blockSignals(True)
                combo.addItem(centre.label, centre.id)
                combo.blockSignals(blocked)
        QMessageBox.information(self, "Cost centres", f"Created {centre.name!r}.")

    # ------------------------------------------------------------- accept
    def _accept(self) -> None:
        self._sync_split_editor()
        for index, (name, _dc, amount) in enumerate(self._lines):
            splits = self._splits[index]
            if not splits:
                continue  # explicitly left unallocated
            allocated = sum((s[2] for s in splits), Decimal("0.00")).quantize(CENT)
            if allocated != amount.quantize(CENT):
                QMessageBox.warning(
                    self,
                    "Cost centres",
                    f"Line {index + 1} ({name}): {allocated:,.2f} allocated against "
                    f"{amount:,.2f} — a line is fully allocated or left unallocated, "
                    "never partly.",
                )
                return
        self.accept()

    def allocations(self) -> list[list[dict]]:
        """The per-line splits as VoucherLineIn.cost_allocations payloads."""
        return [
            [{"centre_id": centre_id, "amount": f"{amount:f}"} for centre_id, _n, amount in splits]
            for splits in self._splits
        ]
