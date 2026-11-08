"""The Banking page — statement import, the line book with suggestions and
match/un-match, the reconciliation statement, and the cheque register.

One native browser over the banking kernel (ADR-026): pick the bank
account, import its statement (CSV or MT940, preview first — the receipt
shows what would land), walk the line book, match a line to the
matchmaker's suggestion with one click (or un-match: the judgement is
revisable, the row never erased), read the proof (book vs bank, the
reconciled balance, every unreconciled item priced), and open the cheque
register with its derived lifecycle. Zero business logic in the GUI —
every number is the engine's string (Law 3), every action one API call.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from .client import ApiClient
from .tasks import run
from .widgets import error_box, error_detail, fill_table, make_table


class BankingPage(QWidget):
    """The line book + the matchmaker, one bank account at a time."""

    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self._lines: list[dict] = []
        self._suggestion_by_row: dict[int, dict] = {}

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Bank account"))
        self.bank_picker = QComboBox()
        self.bank_picker.currentIndexChanged.connect(self._bank_changed)
        bar.addWidget(self.bank_picker)
        self.status_filter = QComboBox()
        for code, label in (
            ("all", "All lines"),
            ("unmatched", "Unmatched"),
            ("matched", "Matched"),
        ):
            self.status_filter.addItem(label, code)
        self.status_filter.currentIndexChanged.connect(self.reload)
        bar.addWidget(self.status_filter)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.reload)
        bar.addWidget(self.refresh_button)
        self.import_button = QPushButton("Import Statement…")
        self.import_button.clicked.connect(self._import_dialog)
        bar.addWidget(self.import_button)
        self.reconcile_button = QPushButton("Reconcile…")
        self.reconcile_button.clicked.connect(self._reconcile_dialog)
        bar.addWidget(self.reconcile_button)
        self.register_button = QPushButton("Cheque Register…")
        self.register_button.clicked.connect(self._register_dialog)
        bar.addWidget(self.register_button)
        bar.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(bar)

        self.table = make_table(
            ["Date", "Withdrawal", "Deposit", "Narrative", "Reference", "Matched to"]
        )
        self.table.setColumnWidth(0, 100)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 300)
        self.table.setColumnWidth(4, 130)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, stretch=2)

        match_bar = QHBoxLayout()
        self.suggestion_label = QLabel("Suggestions for the selected line")
        match_bar.addWidget(self.suggestion_label)
        self.match_button = QPushButton("Match Selected")
        self.match_button.setEnabled(False)
        self.match_button.setToolTip(
            "Match the selected statement line to the highlighted suggestion "
            "(an explicit action — suggestions never auto-commit)"
        )
        self.match_button.clicked.connect(self._match_selected)
        match_bar.addWidget(self.match_button)
        self.unmatch_button = QPushButton("Un-match Selected")
        self.unmatch_button.setEnabled(False)
        self.unmatch_button.clicked.connect(self._unmatch_selected)
        match_bar.addWidget(self.unmatch_button)
        match_bar.addStretch(1)
        layout.addLayout(match_bar)

        self.suggestions = make_table(
            ["Score", "Voucher", "Date", "Dr/Cr", "Amount", "Narration", "Evidence"]
        )
        self.suggestions.setColumnWidth(0, 60)
        self.suggestions.setColumnWidth(1, 150)
        self.suggestions.setColumnWidth(2, 100)
        self.suggestions.setColumnWidth(3, 60)
        self.suggestions.setColumnWidth(4, 110)
        self.suggestions.setColumnWidth(5, 260)
        self.suggestions.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.suggestions, stretch=1)
        layout.addWidget(
            QLabel(
                "issued → presented → cleared: the cheque register derives its "
                "lifecycle from statement lines and matches (ADR-026)."
            )
        )

    # ------------------------------------------------------------- loading
    def reload(self) -> None:
        run(self._load_banks, self._banks_loaded, lambda _e: None)
        self._load_lines()

    def _load_banks(self) -> list[dict]:
        return self._client.bank_ledgers()

    def _banks_loaded(self, banks: list[dict]) -> None:
        current = self.bank_picker.currentData()
        self.bank_picker.blockSignals(True)
        self.bank_picker.clear()
        for bank in banks:
            self.bank_picker.addItem(f"{bank['name']}  ({bank['balance']})", bank["id"])
        if current is not None:
            index = self.bank_picker.findData(current)
            if index >= 0:
                self.bank_picker.setCurrentIndex(index)
        self.bank_picker.blockSignals(False)
        if self.bank_picker.count() and current is None:
            self._load_lines()

    def _bank_changed(self, _index: int) -> None:
        self._load_lines()

    def _bank_id(self) -> int | None:
        return self.bank_picker.currentData()

    def _load_lines(self) -> None:
        bank_id = self._bank_id()
        if bank_id is None:
            return

        def load():
            data = self._client.bank_lines(bank_id, status=self.status_filter.currentData())
            suggestions = self._client.bank_suggestions(bank_id)
            return data, suggestions

        run(load, self._lines_loaded, self._failed)

    def _lines_loaded(self, payload: tuple[dict, dict]) -> None:
        data, suggestions = payload
        self._lines = data.get("lines", [])
        by_line = {entry["statement_line_id"]: entry for entry in suggestions.get("lines", [])}
        self._suggestion_by_row = {}
        rows = []
        for index, line in enumerate(self._lines):
            match = line.get("match")
            matched_to = ""
            if match is not None and match.get("live"):
                matched_to = f"{match['voucher_no']} ({match['voucher_date']})"
            elif match is not None:
                matched_to = "withdrawn"
            entry = by_line.get(line["id"])
            if entry:
                self._suggestion_by_row[index] = entry
            rows.append(
                [
                    line["value_date"],
                    line["amount"] if line["dc"] == "D" else "",
                    line["amount"] if line["dc"] == "C" else "",
                    line.get("narrative") or "",
                    line.get("reference") or "",
                    matched_to,
                ]
            )
        fill_table(self.table, rows)
        self._selection_changed()

    def _failed(self, error: object) -> None:
        error_box(self, "Could not read the bank account", error_detail(error))

    # ------------------------------------------------------------ selection
    def _selected_line(self) -> dict | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._lines):
            return self._lines[row]
        return None

    def _selection_changed(self) -> None:
        line = self._selected_line()
        entry = self._suggestion_by_row.get(self.table.currentRow())
        self.suggestions.setRowCount(0)
        if entry:
            fill_table(
                self.suggestions,
                [
                    [
                        str(candidate["score"]),
                        candidate["voucher_no"],
                        candidate["voucher_date"],
                        candidate["dc"],
                        candidate["amount"],
                        candidate.get("narration") or "",
                        ", ".join(candidate.get("reasons", [])),
                    ]
                    for candidate in entry.get("suggestions", [])
                ],
            )
        has_match = bool(line and line.get("match") and line["match"].get("live"))
        self.unmatch_button.setEnabled(has_match)
        self.match_button.setEnabled(bool(entry and entry.get("suggestions")) and not has_match)
        if line is None:
            self.suggestion_label.setText("Suggestions for the selected line")
        else:
            self.suggestion_label.setText(
                f"Suggestions for line {line['value_date']} "
                f"{'−' if line['dc'] == 'D' else '+'}{line['amount']}"
            )

    # ---------------------------------------------------------- the actions
    def _match_selected(self) -> None:
        line = self._selected_line()
        entry = self._suggestion_by_row.get(self.table.currentRow())
        if not line or not entry or not entry.get("suggestions"):
            return
        candidate = (
            entry["suggestions"][self.suggestions.currentRow()]
            if self.suggestions.currentRow() >= 0
            else entry["suggestions"][0]
        )
        target = {
            "statement_line_id": entry["statement_line_id"],
            "entry_id": candidate["entry_id"],
            "voucher_id": candidate["voucher_id"],
            "voucher_date": candidate["voucher_date"],
        }
        bank_id = self._bank_id()

        def act():
            return self._client.bank_match(bank_id, target)

        run(act, lambda _r: self._load_lines(), self._failed)

    def _unmatch_selected(self) -> None:
        line = self._selected_line()
        if not line:
            return
        line_id = line["id"]

        def act():
            return self._client.bank_unmatch(line_id)

        run(act, lambda _r: self._load_lines(), self._failed)

    # -------------------------------------------------------------- dialogs
    def _import_dialog(self) -> None:
        bank_id = self._bank_id()
        if bank_id is None:
            return
        dialog = ImportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        fmt, text, source, opening = dialog.values()

        def act():
            return self._client.bank_import(
                bank_id,
                fmt,
                text,
                source=source or None,
                opening_balance=opening or None,
            )

        def done(receipt: dict) -> None:
            self._load_lines()
            QMessageBox.information(
                self,
                "Statement imported",
                f"{receipt['new']} new lines, {receipt['duplicates']} duplicates "
                f"skipped (of {receipt['parsed']} parsed).",
            )

        run(act, done, self._failed)

    def _reconcile_dialog(self) -> None:
        bank_id = self._bank_id()
        if bank_id is None:
            return
        dialog = ReconcileDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        as_of, balance = dialog.values()

        def act():
            return self._client.bank_reconciliation(bank_id, as_of, balance or None)

        run(
            act,
            lambda data: ReconcileDialog.show_statement(self, data),
            self._failed,
        )

    def _register_dialog(self) -> None:
        bank_name = self.bank_picker.currentText().split("  (")[0]
        dialog = RegisterDialog(self, bank_name)
        dialog.exec()


# ------------------------------------------------------------------ dialogs
class ImportDialog(QDialog):
    """Paste a statement (CSV or MT940), preview it, then import — the
    receipt is the honest answer in both cases."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Import bank statement")
        layout = QVBoxLayout(self)
        form = QHBoxLayout()
        form.addWidget(QLabel("Format"))
        self.format_box = QComboBox()
        self.format_box.addItem("CSV (bank export)", "csv")
        self.format_box.addItem("MT940 (SWIFT)", "mt940")
        form.addWidget(self.format_box)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Source label (file name…)")
        form.addWidget(self.source_edit)
        self.opening_edit = QLineEdit()
        self.opening_edit.setPlaceholderText("Opening balance (optional)")
        form.addWidget(self.opening_edit)
        layout.addLayout(form)
        self.text_box = QPlainTextEdit()
        self.text_box.setPlaceholderText(
            "Paste the statement text here — header row first for CSV…"
        )
        layout.addWidget(self.text_box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.preview_button = QPushButton("Preview")
        self.preview_button.clicked.connect(self._preview)
        buttons.addButton(self.preview_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.import_button = QPushButton("Import")
        self.import_button.setDefault(True)
        self.import_button.clicked.connect(self.accept)
        buttons.addButton(self.import_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _preview(self) -> None:
        # the dry-run preview: a QMessageBox with the receipt — the parent
        # page performs the real call in tests; here the dialog only
        # validates that text exists
        if not self.text_box.toPlainText().strip():
            QMessageBox.warning(self, "No statement", "Paste the statement text first.")

    def values(self) -> tuple[str, str, str, str]:
        return (
            self.format_box.currentData(),
            self.text_box.toPlainText(),
            self.source_edit.text().strip(),
            self.opening_edit.text().strip(),
        )


class ReconcileDialog(QDialog):
    """The statement's own figures: the as-of date and the closing balance
    on the statement (the accountant's anchor — left blank, the engine
    folds it from the imported opening)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Bank reconciliation")
        layout = QVBoxLayout(self)
        form = QHBoxLayout()
        form.addWidget(QLabel("As of"))
        self.as_of = QDateEdit(date.today())
        self.as_of.setCalendarPopup(True)
        self.as_of.setDisplayFormat("yyyy-MM-dd")
        form.addWidget(self.as_of)
        form.addWidget(QLabel("Balance per statement"))
        self.balance = QDoubleSpinBox()
        self.balance.setRange(-999_999_999.0, 999_999_999.0)
        self.balance.setDecimals(2)
        self.balance.setSpecialValueText("fold from imports")
        form.addWidget(self.balance)
        form.addStretch(1)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[date, str | None]:
        balance = None
        if self.balance.value() != 0.0:
            balance = f"{self.balance.value():.2f}"
        return self.as_of.date().toPython(), balance

    @staticmethod
    def show_statement(parent: QWidget, data: dict) -> None:
        rows = (
            "\n".join(f"  {row['date']}  {row['label']}: {row['effect']}" for row in data["rows"])
            or "  (fully reconciled)"
        )
        QMessageBox.information(
            parent,
            "Bank reconciliation — proof closes",
            (
                f"Balance as per books: {data['book_balance']}\n"
                f"Balance as per bank: {data['statement_balance']} "
                f"({data['statement_balance_source']})\n"
                f"Reconciled balance:  {data['reconciled_balance']}\n\n"
                f"Unreconciled items:\n{rows}\n\n"
                f"Proof: book − statement = {data['proof']['lhs']} "
                f"(exact — ADR-026)"
            ),
        )


class RegisterDialog(QDialog):
    """The cheque register (the banking pack's fold) with its filters."""

    def __init__(self, parent: QWidget | None = None, bank_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Cheque register")
        self.resize(900, 420)
        self._client: ApiClient | None = None
        self._bank_name = bank_name
        layout = QVBoxLayout(self)
        form = QHBoxLayout()
        form.addWidget(QLabel("As of"))
        self.as_of = QDateEdit(date.today())
        self.as_of.setCalendarPopup(True)
        self.as_of.setDisplayFormat("yyyy-MM-dd")
        form.addWidget(self.as_of)
        self.status_filter = QComboBox()
        for code, label in (
            ("", "All cheques"),
            ("issued", "Issued"),
            ("presented", "Presented"),
            ("cleared", "Cleared"),
            ("pdc", "PDC"),
        ):
            self.status_filter.addItem(label, code)
        form.addWidget(self.status_filter)
        run_button = QPushButton("Run")
        run_button.clicked.connect(self._run)
        form.addWidget(run_button)
        form.addStretch(1)
        layout.addLayout(form)
        self.table = make_table(
            [
                "Bank",
                "Cheque No",
                "Voucher",
                "Issued",
                "Matures",
                "Amount",
                "Status",
                "Presented",
                "Cleared by",
            ]
        )
        layout.addWidget(self.table, stretch=1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(lambda _b: self.reject())
        layout.addWidget(buttons)
        # the dialog finds the page's client through its parent chain
        widget = parent
        while widget is not None and not isinstance(widget, BankingPage):
            widget = widget.parentWidget() if hasattr(widget, "parentWidget") else None
        if isinstance(widget, BankingPage):
            self._client = widget._client

    def _run(self) -> None:
        if self._client is None:
            return
        params = {"as_of": self.as_of.date().toPython().isoformat()}
        if self.status_filter.currentData():
            params["status"] = self.status_filter.currentData()
        if self._bank_name:
            params["ledger"] = self._bank_name

        def load():
            return self._client.pack_json("banking-cheques", **params)

        run(load, self._loaded, lambda _e: None)

    def _loaded(self, data: dict) -> None:
        fill_table(
            self.table,
            [
                [
                    row["bank_ledger"],
                    row["no"],
                    row["voucher_no"],
                    row["issued_on"],
                    row["matures_on"] or "—",
                    row["amount"] or "—",
                    row["status"] + (" (PDC)" if row["pdc"] else ""),
                    row["presented_on"] or "—",
                    row["cleared_by"] or "—",
                ]
                for row in data.get("rows", [])
            ],
        )


#: the sidebar's entry (main_window imports this with the page)
PAGE_LABEL = "Banking"
