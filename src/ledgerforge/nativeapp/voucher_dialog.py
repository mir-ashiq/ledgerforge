"""Voucher dialogs — the read-only slip, and the keyboard-first entry form.

The entry form is Law 1 at the desk: OK stays disabled until debits equal
credits to the cent (the client-side check mirrors the engine's exact one —
same Decimal quantization, so what passes here passes there). Ledger names
autocomplete from the book's masters. F4–F9 switch the voucher type with
TallyPrime's muscle memory; a line may carry the FX trio (currency, FX
amount, rate) — the base amount derives exactly as the engine's DB check
computes it (``round(amount_fx × fx_rate, 8)``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
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
from .bill_dialog import BillAllocationDialog
from .client import ApiClient
from .models import Voucher, money
from .tasks import run
from .widgets import error_detail, fill_table, make_table

CENT = Decimal("0.01")
#: The engine's FX-consistency scale (NUMERIC(26,8) amounts, scale ≤ 8).
FX_SCALE = Decimal("0.00000001")

#: TallyPrime's F-key muscle memory — the same map the web keymap presets.
TYPE_KEYS = {"F4": "CN", "F5": "PYT", "F6": "RCT", "F7": "JV", "F8": "SAL", "F9": "PUR"}


def to_cents(raw: str) -> Decimal:
    """A typed amount -> Decimal cents; invalid text -> 0.00 (never floats)."""
    try:
        return Decimal(raw.replace(",", "").strip() or "0").quantize(CENT, rounding=ROUND_HALF_UP)
    except ArithmeticError:
        return Decimal("0.00")


def _parse_exact(raw: str) -> Decimal | None:
    """A typed FX value -> exact Decimal; blank/invalid -> None (never floats)."""
    try:
        return Decimal(raw.replace(",", "").strip())
    except ArithmeticError:
        return None


def _plain(value: Decimal) -> str:
    """A Decimal as plain fixed-point text without trailing-zero noise."""
    return format(value.normalize(), "f")


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

#: Reverse of TYPE_KEYS, for the combo labels ("CN — Contra (F4)").
_TYPE_HINT = {code: key for key, code in TYPE_KEYS.items()}


@dataclass(slots=True)
class _LineEditor:
    ledger: QComboBox
    dc: QComboBox
    amount: QLineEdit
    currency: QComboBox
    amount_fx: QLineEdit
    fx_rate: QLineEdit
    narration: QLineEdit

    # ------------------------------------------------------------ the FX trio
    def _fx_texts(self) -> tuple[str, str, str]:
        return (
            self.currency.currentText().strip().upper(),
            self.amount_fx.text().strip(),
            self.fx_rate.text().strip(),
        )

    def _fx_product(self) -> Decimal | None:
        """round(amount_fx × fx_rate, 8) — exactly the engine's DB check;
        None when the trio is absent or not yet parseable."""
        ccy, fx_raw, rate_raw = self._fx_texts()
        if not (ccy and fx_raw and rate_raw):
            return None
        fx = _parse_exact(fx_raw)
        rate = _parse_exact(rate_raw)
        if fx is None or rate is None or fx <= 0 or rate <= 0:
            return None
        return (fx * rate).quantize(FX_SCALE, rounding=ROUND_HALF_UP)

    def sync_fx_amount(self) -> None:
        """Derive the base amount from a complete trio when it is blank —
        the accountant types the FX facts, the base follows."""
        product = self._fx_product()
        if product is not None and not self.amount.text().strip():
            self.amount.setText(_plain(product))

    def problem(self) -> str | None:
        """Why this line cannot post as typed; None when it can (or is blank)."""
        if not self.ledger.currentText().strip():
            return None  # a blank-ledger line is skipped, not judged
        ccy, fx_raw, rate_raw = self._fx_texts()
        if not (ccy or fx_raw or rate_raw):
            return None  # plain line: the balance gate judges it
        if not (ccy and fx_raw and rate_raw):
            return "an FX line needs currency, FX amount and rate — all three or none"
        fx = _parse_exact(fx_raw)
        rate = _parse_exact(rate_raw)
        if fx is None or rate is None or fx <= 0 or rate <= 0:
            return "the FX amount and rate must be positive decimal numbers"
        product = (fx * rate).quantize(FX_SCALE, rounding=ROUND_HALF_UP)
        amount_raw = self.amount.text().strip()
        if amount_raw:
            amount = _parse_exact(amount_raw)
            if amount is None or amount != product:
                return (
                    f"amount {_plain(amount) if amount else amount_raw} ≠ "
                    f"{_plain(fx)} × {_plain(rate)} = {_plain(product)}"
                )
        return None

    # --------------------------------------------------------------- payload
    def payload(self) -> dict | None:
        """The engine's VoucherLineIn — None when the row is not usable."""
        name = self.ledger.currentText().strip()
        if not name:
            return None
        ccy, fx_raw, rate_raw = self._fx_texts()
        if ccy or fx_raw or rate_raw:
            # an FX line: the base amount is the product, exactly as the
            # engine's chk_entry_fx_consistent computes it
            product = self._fx_product() or Decimal("0")
            fx = _parse_exact(fx_raw) or Decimal("0")
            rate = _parse_exact(rate_raw) or Decimal("0")
            return {
                "ledger_name": name,
                "dc": self.dc.currentText()[0],  # "Debit" -> "D"
                "amount": _plain(product),
                "currency": ccy,
                "amount_fx": _plain(fx),
                "fx_rate": _plain(rate),
                "narration": self.narration.text().strip() or None,
            }
        return {
            "ledger_name": name,
            "dc": self.dc.currentText()[0],  # "Debit" -> "D"
            "amount": str(to_cents(self.amount.text())),
            "narration": self.narration.text().strip() or None,
        }


class NewVoucherDialog(QDialog):
    """Post a voucher: type, date, narration, and balanced D/C lines —
    with bill-wise allocation for party lines (ADR-023): Ctrl+L opens the
    allocation dialog, and the armed refs ride the post as meta.bills."""

    posted = Signal(object)  # the new Voucher

    def __init__(
        self,
        client: ApiClient,
        ledgers: list[str],
        parent: QWidget | None = None,
        party_names: list[str] | None = None,
    ):
        super().__init__(parent)
        self._client = client
        self._ledgers = ledgers
        self._party_names = set(party_names or [])
        self._bill_refs: list[dict] = []
        self._bill_party: str | None = None
        self._lines: list[_LineEditor] = []
        self.setWindowTitle("New Voucher")
        self.setModal(True)
        self.setMinimumSize(960, 480)

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Type"))
        self.type_box = QComboBox()
        for code, name in _ENTRY_TYPES:
            hint = _TYPE_HINT.get(code)
            self.type_box.addItem(f"{code} — {name}" + (f"  ({hint})" if hint else ""), code)
        top.addWidget(self.type_box)
        top.addWidget(QLabel("Date"))
        self.date_edit = QDateEdit()
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd-MM-yyyy")
        top.addWidget(self.date_edit)
        top.addStretch(1)
        self.fx_hint = QLabel("FX per line: currency · FX amount · rate")
        self.fx_hint.setStyleSheet("color: #52607a;")
        top.addWidget(self.fx_hint)
        layout.addLayout(top)

        # TallyPrime's F4–F9: switch the voucher type without touching the mouse
        for key, code in TYPE_KEYS.items():
            QShortcut(QKeySequence(key), self, lambda code=code: self._select_type(code))
        # bill-wise allocation (ADR-023): the same Alt+L the web entry screen binds
        QShortcut(QKeySequence("Ctrl+L"), self, self._open_bill_dialog)

        layout.addWidget(QLabel("Narration"))
        self.narration = QLineEdit()
        self.narration.setPlaceholderText("the voucher's story — shown in the day book")
        layout.addWidget(self.narration)

        layout.addWidget(
            QLabel("Entries — one line per ledger; debits must equal credits; FX columns optional")
        )
        self.lines_table = QTableWidget(0, 7)
        self.lines_table.setHorizontalHeaderLabels(
            ["Ledger", "D/C", "Amount", "Currency", "FX amount", "FX rate", "Line narration"]
        )
        self.lines_table.verticalHeader().setVisible(False)
        self.lines_table.setColumnWidth(0, 220)
        self.lines_table.setColumnWidth(1, 80)
        self.lines_table.setColumnWidth(2, 120)
        self.lines_table.setColumnWidth(3, 80)
        self.lines_table.setColumnWidth(4, 110)
        self.lines_table.setColumnWidth(5, 100)
        header = self.lines_table.horizontalHeader()
        header.setStretchLastSection(True)
        layout.addWidget(self.lines_table, stretch=1)

        line_buttons = QHBoxLayout()
        add_button = QPushButton("Add line (+)")
        add_button.clicked.connect(lambda: self.add_line())
        remove_button = QPushButton("Remove last line")
        remove_button.clicked.connect(self.remove_last_line)
        self.bills_button = QPushButton("Allocate bills… (Ctrl+L)")
        self.bills_button.setToolTip(
            "Bill-wise details for a party line (Sundry Debtors / Creditors) — "
            "settle open bills; the remainder goes on account"
        )
        self.bills_button.clicked.connect(self._open_bill_dialog)
        line_buttons.addWidget(add_button)
        line_buttons.addWidget(remove_button)
        line_buttons.addWidget(self.bills_button)
        line_buttons.addStretch(1)
        self.balance_label = QLabel()
        font = QFont()
        font.setBold(True)
        self.balance_label.setFont(font)
        line_buttons.addWidget(self.balance_label)
        layout.addLayout(line_buttons)

        self.bills_status = QLabel("")
        self.bills_status.setStyleSheet("color: #52607a;")
        layout.addWidget(self.bills_status)

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
    def _select_type(self, code: str) -> None:
        index = self.type_box.findData(code)
        if index >= 0:
            self.type_box.setCurrentIndex(index)

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
        amount.textChanged.connect(self._on_amount_changed)

        currency = QComboBox()
        currency.setEditable(True)
        currency.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        currency.addItem("")
        for code in ("USD", "EUR", "GBP", "AED", "SGD", "JPY", "AUD", "CAD", "CHF", "CNY"):
            currency.addItem(code)
        currency.setCurrentIndex(0)
        currency.currentTextChanged.connect(self._on_fx_changed)

        amount_fx = QLineEdit()
        amount_fx.setPlaceholderText("FX")
        amount_fx.setAlignment(Qt.AlignmentFlag.AlignRight)
        amount_fx.textChanged.connect(self._on_fx_changed)

        fx_rate = QLineEdit()
        fx_rate.setPlaceholderText("rate")
        fx_rate.setAlignment(Qt.AlignmentFlag.AlignRight)
        fx_rate.textChanged.connect(self._on_fx_changed)

        narration = QLineEdit()

        self.lines_table.setCellWidget(row, 0, ledger)
        self.lines_table.setCellWidget(row, 1, dc)
        self.lines_table.setCellWidget(row, 2, amount)
        self.lines_table.setCellWidget(row, 3, currency)
        self.lines_table.setCellWidget(row, 4, amount_fx)
        self.lines_table.setCellWidget(row, 5, fx_rate)
        self.lines_table.setCellWidget(row, 6, narration)
        self.lines_table.setRowHeight(row, 32)
        self._lines.append(_LineEditor(ledger, dc, amount, currency, amount_fx, fx_rate, narration))

    def remove_last_line(self) -> None:
        if len(self._lines) <= 1:
            return
        self.lines_table.removeRow(len(self._lines) - 1)
        self._lines.pop()

    # -------------------------------------------------- FX amount derivation
    def _on_amount_changed(self) -> None:
        # a cleared amount on a complete FX line re-derives from the trio
        for line in self._lines:
            line.sync_fx_amount()
        self._update_balance()

    def _on_fx_changed(self) -> None:
        for line in self._lines:
            line.sync_fx_amount()
        self._update_balance()

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

    def _fx_problem(self) -> str | None:
        for line in self._lines:
            problem = line.problem()
            if problem is not None:
                return problem
        return None

    def is_balanced(self) -> bool:
        debit, credit, usable = self._totals()
        return self._fx_problem() is None and usable >= 2 and debit == credit and debit > 0

    def _update_balance(self) -> None:
        problem = self._fx_problem()
        if problem is not None:
            self.balance_label.setText(f"FX problem — {problem}")
            self.balance_label.setStyleSheet("color: #b42318;")
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return
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

    # ------------------------------------------------------ bill allocation
    def _party_movement(self) -> tuple[str, Decimal] | None:
        """The first party line's ledger and its signed net movement
        (Σ Dr − Σ Cr over that party's lines); None when no party line moves."""
        party = next(
            (
                ln.ledger.currentText().strip()
                for ln in self._lines
                if ln.ledger.currentText().strip() in self._party_names
            ),
            None,
        )
        if party is None:
            return None
        movement = Decimal("0.00")
        for line in self._lines:
            if line.ledger.currentText().strip() != party:
                continue
            payload = line.payload()
            if payload is None:
                continue
            amount = Decimal(payload["amount"])
            movement += amount if payload["dc"] == "D" else -amount
        return party, movement.quantize(CENT)

    def _open_bill_dialog(self) -> None:
        found = self._party_movement()
        if found is None:
            QMessageBox.information(
                self,
                "Bills",
                "No party line yet — add a line on a Sundry Debtors / Sundry "
                "Creditors ledger with its amount first.",
            )
            return
        party, movement = found
        if movement == 0:
            QMessageBox.information(
                self,
                "Bills",
                f"Enter {party}'s debit or credit first — the allocation follows the movement.",
            )
            return
        dialog = BillAllocationDialog(self._client, party, movement, self)
        if dialog.exec() and dialog.refs():
            self._bill_party = party
            self._bill_refs = dialog.refs()
            self.bills_status.setText(
                f"Bills armed for {party}: {len(self._bill_refs)} ref(s) — "
                "they ride the post as meta.bills"
            )
        elif dialog.refs() == [] and dialog.result() == QDialog.DialogCode.Accepted:
            # an empty allocation is a deliberate clear
            self._bill_party = None
            self._bill_refs = []
            self.bills_status.setText("")

    # ------------------------------------------------------------- posting
    def _submit(self) -> None:
        entries = [p for p in (line.payload() for line in self._lines) if p is not None]
        voucher_date: date = self.date_edit.date().toPython()
        voucher_type = self.type_box.currentData()
        client_token = str(uuid.uuid4())  # idempotency: a retry reposts nothing
        meta = None
        if self._bill_party and self._bill_refs:
            meta = {"bills": {"party": self._bill_party, "refs": self._bill_refs}}

        def post() -> Voucher:
            return self._client.create_voucher(
                voucher_type=voucher_type,
                voucher_date=voucher_date,
                entries=entries,
                narration=self.narration.text().strip() or None,
                client_token=client_token,
                meta=meta,
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
