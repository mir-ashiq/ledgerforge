"""Voucher dialogs — the read-only slip, and the keyboard-first entry form.

The entry form is Law 1 at the desk: OK stays disabled until debits equal
credits to the cent (the client-side check mirrors the engine's exact one —
same Decimal quantization, so what passes here passes there). Ledger names
autocomplete from the book's masters.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..seeding import VOUCHER_TYPES
from .client import ApiClient
from .models import Voucher, money
from .tasks import run
from .widgets import error_detail, fill_table, make_table

CENT = Decimal("0.01")


def to_cents(raw: str) -> Decimal:
    """A typed amount -> Decimal cents; invalid text -> 0.00 (never floats)."""
    try:
        return Decimal(raw.replace(",", "").strip() or "0").quantize(CENT, rounding=ROUND_HALF_UP)
    except ArithmeticError:
        return Decimal("0.00")


class VoucherViewDialog(QDialog):
    """A posted voucher: header facts, its entries, Save as PDF."""

    def __init__(self, client: ApiClient, voucher: Voucher, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self._voucher = voucher
        self.setWindowTitle(f"Voucher {voucher.voucher_no}")
        self.setModal(True)
        self.setMinimumSize(640, 420)

        layout = QVBoxLayout(self)
        header = QLabel(
            f"<b>{voucher.voucher_no}</b> &nbsp; {voucher.voucher_type} &nbsp; "
            f"{voucher.voucher_date.strftime('%d-%m-%Y')}"
            + (f" &nbsp; <i>reversal of #{voucher.reversal_of}</i>" if voucher.is_reversal else "")
        )
        layout.addWidget(header)
        if voucher.narration:
            narration = QLabel(voucher.narration)
            narration.setWordWrap(True)
            layout.addWidget(narration)

        self.table = make_table(["#", "Ledger", "D/C", "Amount", "FX", "Narration"])
        rows = []
        for e in voucher.entries:
            fx = ""
            if e.currency:
                fx = f"{money(e.amount_fx or 0)} {e.currency} @ {e.fx_rate or ''}"
            rows.append(
                [
                    str(e.line_no),
                    e.ledger_name,
                    "Debit" if e.dc == "D" else "Credit",
                    money(e.amount),
                    fx,
                    e.narration or "",
                ]
            )
        fill_table(self.table, rows)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 220)
        self.table.setColumnWidth(2, 70)
        self.table.setColumnWidth(3, 130)
        self.table.setColumnWidth(4, 170)
        self.table.setMinimumHeight(240)
        layout.addWidget(self.table, stretch=1)

        total = QLabel(f"Debit total: <b>{money(voucher.amount)}</b>")
        layout.addWidget(total)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.pdf_button = buttons.addButton("Save as PDF…", QDialogButtonBox.ButtonRole.ActionRole)
        self.pdf_button.clicked.connect(self._save_pdf)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)

    def _save_pdf(self) -> None:
        default = f"{self._voucher.voucher_no.replace('/', '-')}.pdf"

        def fetch() -> tuple[bytes, str]:
            return self._client.voucher_pdf(self._voucher.id)

        run(
            fetch,
            lambda result: self._write_pdf(result[0], result[1] or default),
            lambda error: QMessageBox.critical(
                self, "Print", f"Could not render the PDF:\n{error_detail(error)}"
            ),
        )

    def _write_pdf(self, content: bytes, filename: str) -> None:
        target, _filter = QFileDialog.getSaveFileName(
            self, "Save voucher PDF", filename, "PDF (*.pdf)"
        )
        if not target:
            return
        Path(target).write_bytes(content)
        QMessageBox.information(self, "Print", f"Saved {Path(target).name}")


#: The entry types: every seeded code except the system-only OPEN/FYC.
_ENTRY_TYPES = [(code, name) for code, name, _p, _r, is_system in VOUCHER_TYPES if not is_system]


@dataclass(slots=True)
class _LineEditor:
    ledger: QComboBox
    dc: QComboBox
    amount: QLineEdit
    narration: QLineEdit

    def payload(self) -> dict | None:
        """The engine's VoucherLineIn — None when the row is not usable."""
        name = self.ledger.currentText().strip()
        if not name:
            return None
        return {
            "ledger_name": name,
            "dc": self.dc.currentText()[0],  # "Debit" -> "D"
            "amount": str(to_cents(self.amount.text())),
            "narration": self.narration.text().strip() or None,
        }


class NewVoucherDialog(QDialog):
    """Post a voucher: type, date, narration, and balanced D/C lines."""

    posted = Signal(object)  # the new Voucher

    def __init__(self, client: ApiClient, ledgers: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self._ledgers = ledgers
        self._lines: list[_LineEditor] = []
        self.setWindowTitle("New Voucher")
        self.setModal(True)
        self.setMinimumSize(720, 480)

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Type"))
        self.type_box = QComboBox()
        for code, name in _ENTRY_TYPES:
            self.type_box.addItem(f"{code} — {name}", code)
        top.addWidget(self.type_box)
        top.addWidget(QLabel("Date"))
        self.date_edit = QDateEdit()
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd-MM-yyyy")
        top.addWidget(self.date_edit)
        top.addStretch(1)
        layout.addLayout(top)

        layout.addWidget(QLabel("Narration"))
        self.narration = QLineEdit()
        self.narration.setPlaceholderText("the voucher's story — shown in the day book")
        layout.addWidget(self.narration)

        layout.addWidget(QLabel("Entries — one line per ledger; debits must equal credits"))
        self.lines_table = QTableWidget(0, 4)
        self.lines_table.setHorizontalHeaderLabels(["Ledger", "D/C", "Amount", "Line narration"])
        self.lines_table.verticalHeader().setVisible(False)
        self.lines_table.setColumnWidth(0, 260)
        self.lines_table.setColumnWidth(1, 90)
        self.lines_table.setColumnWidth(2, 130)
        header = self.lines_table.horizontalHeader()
        header.setStretchLastSection(True)
        layout.addWidget(self.lines_table, stretch=1)

        line_buttons = QHBoxLayout()
        add_button = QPushButton("Add line (+)")
        add_button.clicked.connect(lambda: self.add_line())
        remove_button = QPushButton("Remove last line")
        remove_button.clicked.connect(self.remove_last_line)
        line_buttons.addWidget(add_button)
        line_buttons.addWidget(remove_button)
        line_buttons.addStretch(1)
        self.balance_label = QLabel()
        font = QFont()
        font.setBold(True)
        self.balance_label.setFont(font)
        line_buttons.addWidget(self.balance_label)
        layout.addLayout(line_buttons)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Post voucher")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.add_line()  # a payment opens Dr Cash / Cr Bank-shaped: two blanks
        self.add_line()

    # ------------------------------------------------------------ the lines
    def add_line(self) -> None:
        row = len(self._lines)
        self.lines_table.insertRow(row)

        ledger = QComboBox()
        ledger.setEditable(True)
        ledger.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        ledger.addItems(self._ledgers)
        ledger.completer().setCompletionMode(ledger.completer().CompletionMode.PopupCompletion)

        dc = QComboBox()
        dc.addItems(["Debit", "Credit"])

        amount = QLineEdit()
        amount.setPlaceholderText("0.00")
        amount.setAlignment(Qt.AlignmentFlag.AlignRight)
        amount.textChanged.connect(self._update_balance)

        narration = QLineEdit()

        self.lines_table.setCellWidget(row, 0, ledger)
        self.lines_table.setCellWidget(row, 1, dc)
        self.lines_table.setCellWidget(row, 2, amount)
        self.lines_table.setCellWidget(row, 3, narration)
        self.lines_table.setRowHeight(row, 32)
        self._lines.append(_LineEditor(ledger, dc, amount, narration))

    def remove_last_line(self) -> None:
        if len(self._lines) <= 1:
            return
        self.lines_table.removeRow(len(self._lines) - 1)
        self._lines.pop()

    # -------------------------------------------------------------- balance
    def _totals(self) -> tuple[Decimal, Decimal, int]:
        debit = Decimal("0.00")
        credit = Decimal("0.00")
        usable = 0
        for line in self._lines:
            payload = line.payload()
            if payload is None:
                continue
            usable += 1
            amount = Decimal(payload["amount"])
            if payload["dc"] == "D":
                debit += amount
            else:
                credit += amount
        return debit.quantize(CENT), credit.quantize(CENT), usable

    def is_balanced(self) -> bool:
        debit, credit, usable = self._totals()
        return usable >= 2 and debit == credit and debit > 0

    def _update_balance(self) -> None:
        debit, credit, usable = self._totals()
        if debit == credit and usable >= 2 and debit > 0:
            self.balance_label.setText(f"Balanced — {money(debit)}")
            self.balance_label.setStyleSheet("color: #1a7f37;")
        else:
            difference = (debit - credit).quantize(CENT)
            self.balance_label.setText(
                f"Dr {money(debit)} — Cr {money(credit)} (difference {money(difference)})"
                if usable
                else "Add at least two ledger lines"
            )
            self.balance_label.setStyleSheet("color: #b42318;")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.is_balanced())

    # ------------------------------------------------------------- posting
    def _submit(self) -> None:
        entries = [p for p in (line.payload() for line in self._lines) if p is not None]
        voucher_date: date = self.date_edit.date().toPython()
        voucher_type = self.type_box.currentData()
        client_token = str(uuid.uuid4())  # idempotency: a retry reposts nothing

        def post() -> Voucher:
            return self._client.create_voucher(
                voucher_type=voucher_type,
                voucher_date=voucher_date,
                entries=entries,
                narration=self.narration.text().strip() or None,
                client_token=client_token,
            )

        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        run(post, self._posted, self._post_failed)

    def _posted(self, voucher: Voucher) -> None:
        self.posted.emit(voucher)
        self.accept()

    def _post_failed(self, error: object) -> None:
        detail = getattr(error, "detail", str(error))
        QMessageBox.critical(
            self,
            "Voucher rejected",
            f"The engine refused the voucher:\n\n{detail}\n\n"
            "Nothing was posted — fix the entries and post again.",
        )
        self._update_balance()
