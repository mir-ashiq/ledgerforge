"""The native bill-allocation dialog (ADR-023, Phase 2).

The on-account / against-ref decision at entry time, in the native voucher
form: one party's open bills (the engine's ``bills-open`` report, plugin
API 0.8), an amount cell per bill, the remainder offered as on-account (the
TallyPrime flow — the excess always has somewhere honest to go) or as a new
ref. The dialog suggests; the engine decides — the conservation law re-checks
every allocation at posting time, and a refusal keeps the draft intact.

All arithmetic is exact ``Decimal`` (Law 3): the party's movement and the
allocated/remainder figures derive from the same 2-dp amounts the form will
post.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .client import ApiClient
from .tasks import run
from .widgets import error_detail

CENT = Decimal("0.01")

#: Seed groups whose ledgers are bill-managed parties (ADR-023).
PARTY_GROUPS = ("Sundry Debtors", "Sundry Creditors")


def to_cents(raw: str) -> Decimal:
    """A typed amount -> Decimal cents; invalid text -> 0.00 (never floats)."""
    try:
        return Decimal(raw.replace(",", "").strip() or "0").quantize(CENT, rounding=ROUND_HALF_UP)
    except ArithmeticError:
        return Decimal("0.00")


class BillAllocationDialog(QDialog):
    """Allocate a party's movement across its open bills, remainder to
    on-account or a new ref. ``refs()`` answers the meta.bills payload."""

    def __init__(
        self,
        client: ApiClient,
        party: str,
        movement: Decimal,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._client = client
        self._party = party
        #: signed: > 0 the voucher debits the party, < 0 it credits
        self._movement = movement
        self._refs: list[dict] = []
        self._rows: list[dict] = []  # the engine's open bills

        credit_moves = movement < 0
        side = "cr" if credit_moves else "dr"
        self._side = side

        self.setWindowTitle(f"Bills — {party}")
        self.setModal(True)
        self.setMinimumSize(640, 420)

        layout = QVBoxLayout(self)
        verb = "credits" if credit_moves else "debits"
        layout.addWidget(
            QLabel(
                f"This voucher <b>{verb} {party}</b> by "
                f"<b>{abs(movement).quantize(CENT):f}</b> — settle open bills; "
                "the remainder goes on account or opens a new ref."
            )
        )

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Ref", "Due", "Outstanding", "Ageing", "Allocate"])
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 90)
        self.table.setColumnWidth(4, 110)
        layout.addWidget(self.table, stretch=1)

        self.summary = QLabel("Reading open bills…")
        layout.addWidget(self.summary)

        remainder_row = QHBoxLayout()
        self.on_account_box = QCheckBox("Remainder on account")
        self.on_account_box.setChecked(True)
        self.new_ref_box = QCheckBox("Open new ref")
        self.new_ref_edit = QLineEdit()
        self.new_ref_edit.setPlaceholderText("ref (e.g. INV-42)")
        self.new_due_edit = QLineEdit()
        self.new_due_edit.setPlaceholderText("due days")
        self.new_due_edit.setMaximumWidth(70)
        remainder_row.addWidget(self.on_account_box)
        remainder_row.addWidget(self.new_ref_box)
        remainder_row.addWidget(self.new_ref_edit)
        remainder_row.addWidget(QLabel("due"))
        remainder_row.addWidget(self.new_due_edit)
        remainder_row.addStretch(1)
        layout.addLayout(remainder_row)

        self.on_account_box.toggled.connect(self._mode_changed)
        self.new_ref_box.toggled.connect(self._mode_changed)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        run(
            lambda: self._client.pack_json("bills-open", party=self._party),
            self._loaded,
            self._failed,
        )

    def _failed(self, error: object) -> None:
        self.summary.setText(f"Could not read open bills — {error_detail(error)}")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

    def _loaded(self, data: dict) -> None:
        self._rows = [b for b in data.get("bills", [])]
        # settleable bills sit opposite the movement; same-side rows ride
        # along, greyed, so the whole position stays visible
        settleable = [
            b for b in self._rows if (Decimal(b["outstanding"]) > 0) == (self._movement < 0)
        ]
        dimmed = [b for b in self._rows if b not in settleable]
        on_account = Decimal(str(data.get("on_account", "0")))

        self.table.setRowCount(len(settleable) + len(dimmed))
        for row, bill in enumerate(settleable + dimmed):
            editable = row < len(settleable)
            for col, value in enumerate(
                (bill["ref"], bill["due_on"], bill["outstanding"], bill["bucket"])
            ):
                item = QTableWidgetItem(value)
                if not editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(row, col, item)
            if editable:
                cell = QLineEdit()
                cell.setPlaceholderText("0.00")
                cell.setAlignment(Qt.AlignmentFlag.AlignRight)
                cell.textChanged.connect(self._recap)
                self.table.setCellWidget(row, 4, cell)
            else:
                dash = QTableWidgetItem("—")
                dash.setFlags(dash.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(row, 4, dash)

        self._settleable_count = len(settleable)
        self.summary.setText(
            f"{len(settleable)} settleable · on account today: {on_account.quantize(CENT):f}"
        )
        self._recap()

    # ----------------------------------------------------------------- recap
    def _allocated(self) -> Decimal:
        total = Decimal("0.00")
        for row in range(getattr(self, "_settleable_count", 0)):
            cell = self.table.cellWidget(row, 4)
            if isinstance(cell, QLineEdit):
                total += to_cents(cell.text())
        return total

    def _recap(self) -> None:
        allocated = self._allocated()
        remainder = abs(self._movement) - allocated
        ok = remainder >= 0
        self.summary.setText(
            f"Allocated {allocated:f} of {abs(self._movement).quantize(CENT):f} · "
            + (f"remainder {remainder:f}" if ok else f"over-allocated by {-remainder:f}")
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def _mode_changed(self) -> None:
        exclusive = self.on_account_box.isChecked() and self.new_ref_box.isChecked()
        if exclusive:
            # the toggle that just flipped wins; untick the other
            sender = self.sender()
            if sender is self.on_account_box:
                self.new_ref_box.setChecked(False)
            else:
                self.on_account_box.setChecked(False)

    # ---------------------------------------------------------------- accept
    def _accept(self) -> None:
        refs: list[dict] = []
        for row in range(getattr(self, "_settleable_count", 0)):
            cell = self.table.cellWidget(row, 4)
            if not isinstance(cell, QLineEdit):
                continue
            amount = to_cents(cell.text())
            if amount <= 0:
                continue
            bill = self._rows[row]
            outstanding = abs(Decimal(bill["outstanding"]))
            if amount > outstanding:
                QMessageBox.warning(
                    self,
                    "Bills",
                    f"Bill {bill['ref']}: {amount:f} exceeds its outstanding "
                    f"{outstanding:f} — the excess belongs to on-account.",
                )
                return
            refs.append({"kind": "against", "ref": bill["ref"], self._side: f"{amount:f}"})

        remainder = abs(self._movement) - self._allocated()
        if remainder < 0:
            QMessageBox.warning(self, "Bills", "Allocations exceed the party's movement")
            return
        if remainder > 0:
            if self.new_ref_box.isChecked():
                name = self.new_ref_edit.text().strip()
                if not name:
                    QMessageBox.warning(
                        self, "Bills", "Name the new bill (the ref) or choose on-account"
                    )
                    return
                due_days = int(to_cents(self.new_due_edit.text() or "0"))
                refs.append(
                    {"kind": "new", "ref": name, self._side: f"{remainder:f}", "due_days": due_days}
                )
            else:
                refs.append({"kind": "on_account", self._side: f"{remainder:f}"})

        self._refs = refs
        self.accept()

    # ------------------------------------------------------------------ read
    def refs(self) -> list[dict]:
        return self._refs
