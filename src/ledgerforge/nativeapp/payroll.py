"""The Payroll page — the people workbench (ADR-029, roadmap Phase 8): key
the period's attendance register (a grid the engine validates — the
preview receipt shows what would land), issue staff loans (the posted
voucher's computed schedule is the confirmation — the engine's numbers,
never the GUI's), settle a relieving employee's dues in one voucher, and
walk every payroll document the book holds (runs, payouts, loans,
settlements) with per-employee payslip PDFs. Zero business logic in the
GUI — every number is the engine's string (Law 3), every action one API
call, the GST page's discipline.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .client import ApiClient
from .models import Voucher
from .tasks import run
from .widgets import error_box, error_detail, fill_table, make_table

#: The payroll document modes (ADR-012 run/payout, ADR-029 loan/settlement).
_MODES = ("run", "payout", "loan", "settlement")


def payroll_doc_of(voucher: Voucher) -> dict | None:
    """The voucher's payroll document, or None when it is not one."""
    meta = voucher.meta or {}
    doc = meta.get("payroll")
    if isinstance(doc, dict) and doc.get("mode") in _MODES:
        return doc
    return None


class PayrollPage(QWidget):
    """The people workbench: attendance, loans, settlements, payslips."""

    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client

        bar = QHBoxLayout()
        bar.addWidget(QLabel("period (YYYY-MM)"))
        self.period_edit = QLineEdit()
        self.period_edit.setPlaceholderText("e.g. 2026-10")
        bar.addWidget(self.period_edit)
        self.load_button = QPushButton("Load Register")
        self.load_button.clicked.connect(self._load_register)
        bar.addWidget(self.load_button)
        self.attendance_button = QPushButton("Key Attendance…")
        self.attendance_button.clicked.connect(self._attendance_dialog)
        bar.addWidget(self.attendance_button)
        self.loan_button = QPushButton("Issue Loan…")
        self.loan_button.clicked.connect(self._loan_dialog)
        bar.addWidget(self.loan_button)
        self.settle_button = QPushButton("Settle (F&F)…")
        self.settle_button.clicked.connect(self._settlement_dialog)
        bar.addWidget(self.settle_button)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.reload)
        bar.addWidget(self.refresh_button)
        bar.addStretch(1)

        self.attendance = make_table(
            ["Employee", "Present", "Absent", "Half", "Leave (paid)", "Payable days"]
        )
        self.attendance.setColumnWidth(0, 220)

        self.loans = make_table(["Employee", "Issued", "Recovered", "Outstanding", "Loans"])
        self.loans.setColumnWidth(0, 220)

        self.documents = make_table(["No", "Date", "Kind", "Employee / period", "Amount"])
        self.documents.setColumnWidth(0, 90)
        self.documents.setColumnWidth(1, 100)
        self.documents.setColumnWidth(3, 260)
        self.documents.itemSelectionChanged.connect(self._document_selected)

        self.payslip_button = QPushButton("Save Payslip PDF…")
        self.payslip_button.setEnabled(False)
        self.payslip_button.clicked.connect(self._save_payslip)
        pdf_bar = QHBoxLayout()
        pdf_bar.addWidget(self.payslip_button)
        pdf_bar.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(
            QLabel("Attendance — the live register (payable days = present + half × 0.5 + leave)")
        )
        layout.addWidget(self.attendance)
        layout.addWidget(QLabel("Loans & advances — the outstanding fold"))
        layout.addWidget(self.loans)
        layout.addWidget(QLabel("Payroll documents"))
        layout.addWidget(self.documents, stretch=1)
        layout.addLayout(pdf_bar)

    # ------------------------------------------------------------- loading
    def reload(self) -> None:
        self._load_register()
        run(
            lambda: (
                self._client.pack_json("payroll-loans"),
                [v for v in self._client.vouchers(limit=200) if payroll_doc_of(v) is not None],
            ),
            self._loans_loaded,
            self._failed,
        )

    def _period(self) -> str:
        return self.period_edit.text().strip()

    def _load_register(self) -> None:
        period = self._period()
        if not period:
            return
        run(
            lambda: self._client.pack_json("payroll-attendance", period=period),
            self._register_loaded,
            self._failed,
        )

    def _register_loaded(self, payload: dict) -> None:
        rows = payload.get("rows") or []
        fill_table(
            self.attendance,
            [
                [
                    row["employee"],
                    row["present"],
                    row["absent"],
                    row["half"],
                    row["leave_paid"],
                    row["payable_days"],
                ]
                for row in rows
            ],
        )

    def _loans_loaded(self, payload: tuple[dict, list[Voucher]]) -> None:
        loans, vouchers = payload
        fill_table(
            self.loans,
            [
                [
                    row["employee"],
                    row["issued"],
                    row["recovered"],
                    row["outstanding"],
                    str(len(row.get("loans") or [])),
                ]
                for row in loans.get("employees") or []
            ],
        )
        fill_table(
            self.documents,
            [
                [
                    v.voucher_no,
                    v.voucher_date.isoformat(),
                    _describe(payroll_doc_of(v)),
                    _subject(payroll_doc_of(v)),
                    str(v.amount),
                ]
                for v in reversed(vouchers)  # newest first
            ],
        )
        self._voucher_by_no = {v.voucher_no: v for v in vouchers}

    def _failed(self, error: object) -> None:
        error_box(self, "Payroll", error_detail(error))

    # ------------------------------------------------------------- dialogs
    def _attendance_dialog(self) -> None:
        dialog = AttendanceDialog(self._client, self._period(), self)
        dialog.exec()

    def _loan_dialog(self) -> None:
        dialog = LoanDialog(self._client, self)
        dialog.exec()

    def _settlement_dialog(self) -> None:
        dialog = SettlementDialog(self._client, self._period(), self)
        dialog.exec()

    # ------------------------------------------------------------ payslips
    def _document_selected(self) -> None:
        self.payslip_button.setEnabled(self._selected_voucher() is not None)

    def _selected_voucher(self) -> Voucher | None:
        row = self.documents.currentRow()
        if row < 0:
            return None
        item = self.documents.item(row, 0)
        if item is None:
            return None
        voucher = getattr(self, "_voucher_by_no", {}).get(item.text())
        if voucher is None:
            return None
        doc = payroll_doc_of(voucher)
        if doc is None or doc.get("mode") != "run":
            return None  # only runs print as payslips (payouts/loans are slips)
        return voucher

    def _save_payslip(self) -> None:
        voucher = self._selected_voucher()
        if voucher is None:
            return
        doc = payroll_doc_of(voucher) or {}
        names = [
            str(e.get("employee") or "").strip()
            for e in doc.get("employees") or []
            if isinstance(e, dict) and str(e.get("employee") or "").strip()
        ]
        if not names:
            return
        employee = names[0]
        if len(names) > 1:
            picked, ok = QInputDialog.getItem(self, "Payslip", "Employee", names, 0, False)
            if not ok:
                return
            employee = picked
        target, _ = QFileDialog.getSaveFileName(
            self, "Save payslip", f"Payslip {employee} {doc.get('period') or ''}.pdf"
        )
        if not target:
            return

        def fetch():
            return self._client.voucher_pdf(voucher.id, employee=employee)

        run(fetch, lambda pdf: self._payslip_saved(target, pdf), self._failed)

    def _payslip_saved(self, target: str, pdf: tuple[bytes, str]) -> None:
        with open(target, "wb") as handle:
            handle.write(pdf[0])
        QMessageBox.information(self, "Payroll", f"Payslip saved — {target}")


def _describe(doc: dict | None) -> str:
    mode = (doc or {}).get("mode")
    return {
        "run": "Run",
        "payout": "Payout",
        "loan": "Loan",
        "settlement": "Settlement",
    }.get(mode, mode or "")


def _subject(doc: dict | None) -> str:
    doc = doc or {}
    mode = doc.get("mode")
    if mode in ("payout", "loan", "settlement"):
        return f"{doc.get('employee') or ''} · {doc.get('period') or ''}".strip(" ·")
    names = ", ".join(
        str(e.get("employee") or "") for e in doc.get("employees") or [] if isinstance(e, dict)
    )
    return f"{names} · {doc.get('period') or ''}".strip(" ·")


# ------------------------------------------------------------- the dialogs
class AttendanceDialog(QDialog):
    """Key one period's register: a grid of employees × day counts, with a
    preview (the dry-run receipt) before anything lands. Half-days are 0.5."""

    def __init__(self, client: ApiClient, period: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self.setWindowTitle("Key attendance")
        self.resize(560, 360)

        self.period_edit = QLineEdit(period)
        form = QFormLayout()
        form.addRow("Period (YYYY-MM)", self.period_edit)

        self.grid = QTableWidget(0, 5)
        self.grid.setHorizontalHeaderLabels(
            ["Employee", "Present", "Absent", "Half", "Leave (paid)"]
        )
        self.grid.horizontalHeader().setStretchLastSection(True)

        buttons = QHBoxLayout()
        add = QPushButton("Add Row")
        add.clicked.connect(self._add_row)
        buttons.addWidget(add)
        remove = QPushButton("Remove Row")
        remove.clicked.connect(self._remove_row)
        buttons.addWidget(remove)
        buttons.addStretch(1)

        actions = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        actions.button(QDialogButtonBox.StandardButton.Ok).setText("Preview")
        actions.accepted.connect(self._preview)
        actions.rejected.connect(self.reject)

        # Import is its own button (not a button-box AcceptRole — that would
        # fire the preview too); enabled only after a clean preview.
        self.import_button = QPushButton("Import")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self._import)

        self.receipt = QLabel("")
        self.receipt.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.grid)
        layout.addLayout(buttons)
        layout.addWidget(self.receipt)
        layout.addWidget(actions)
        layout.addWidget(self.import_button)
        self._add_row()

    def _add_row(self) -> None:
        row = self.grid.rowCount()
        self.grid.insertRow(row)
        self.grid.setItem(row, 0, QTableWidgetItem(""))
        for column, default in ((1, "22"), (2, "0"), (3, "0"), (4, "0")):
            item = QTableWidgetItem(default)
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight)
            self.grid.setItem(row, column, item)

    def _remove_row(self) -> None:
        row = self.grid.currentRow()
        if row >= 0:
            self.grid.removeRow(row)

    def _entries(self) -> list[dict] | None:
        entries: list[dict] = []
        for row in range(self.grid.rowCount()):
            name = (self.grid.item(row, 0).text() if self.grid.item(row, 0) else "").strip()
            if not name:
                continue
            entry = {"employee": name}
            for column, key in ((1, "present"), (2, "absent"), (3, "half"), (4, "leave_paid")):
                item = self.grid.item(row, column)
                raw = (item.text() if item else "0").strip() or "0"
                entry[key] = float(raw)  # the engine validates (a bad cell → 422)
            entries.append(entry)
        return entries or None

    def _preview(self) -> None:
        entries = self._entries()
        if entries is None:
            self.receipt.setText("Key at least one employee row.")
            return

        def work():
            return self._client.attendance_import(
                self.period_edit.text().strip(), entries, dry_run=True
            )

        run(work, self._previewed, lambda e: error_box(self, "Attendance", error_detail(e)))

    def _previewed(self, receipt: dict) -> None:
        self.receipt.setText(
            f"Preview — period {receipt.get('period')}: {receipt.get('parsed')} rows, "
            f"{receipt.get('new')} new, {receipt.get('duplicates')} unchanged."
        )
        self.import_button.setEnabled(True)

    def _import(self) -> None:
        entries = self._entries()
        if entries is None:
            return

        def work():
            return self._client.attendance_import(self.period_edit.text().strip(), entries)

        def done(receipt: dict) -> None:
            QMessageBox.information(
                self,
                "Attendance",
                f"Register imported — revision {receipt.get('revision')}, "
                f"{receipt.get('new')} rows (batch {receipt.get('batch_id', '—')}).",
            )
            self.accept()

        run(work, done, lambda e: error_box(self, "Attendance", error_detail(e)))


class LoanDialog(QDialog):
    """Issue a staff loan or advance. The EMI schedule is the engine's —
    the confirmation shows the posted voucher's computed figures, never a
    GUI-side arithmetic."""

    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self.setWindowTitle("Issue a staff loan")
        self.resize(420, 260)

        self.employee = QLineEdit()
        self.principal = QDoubleSpinBox()
        self.principal.setDecimals(2)
        self.principal.setMaximum(999_999_999.0)
        self.principal.setValue(100000.0)
        self.rate = QDoubleSpinBox()
        self.rate.setDecimals(2)
        self.rate.setMaximum(100.0)
        self.rate.setValue(9.0)  # 0 = interest-free advance
        self.months = QSpinBox()
        self.months.setRange(1, 360)
        self.months.setValue(20)
        self.bank = QLineEdit("HDFC Current")
        self.first_period = QLineEdit()
        self.first_period.setPlaceholderText("default: the month after the voucher date")

        form = QFormLayout()
        form.addRow("Employee", self.employee)
        form.addRow("Principal", self.principal)
        form.addRow("Annual rate % (0 = interest-free)", self.rate)
        form.addRow("Months", self.months)
        form.addRow("Pay from bank", self.bank)
        form.addRow("First installment period", self.first_period)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Issue")
        buttons.accepted.connect(self._issue)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _issue(self) -> None:
        employee = self.employee.text().strip()
        if not employee or not self.bank.text().strip():
            error_box(self, "Loan", "The employee and the bank are required.")
            return
        doc: dict = {
            "mode": "loan",
            "employee": employee,
            "principal": f"{self.principal.value():.2f}",
            "rate": f"{self.rate.value():.2f}",
            "months": self.months.value(),
            "bank": self.bank.text().strip(),
        }
        if self.first_period.text().strip():
            doc["first_period"] = self.first_period.text().strip()

        def work():
            return self._client.create_voucher(
                voucher_type="JV",
                voucher_date=date.today(),
                entries=[
                    {"ledger_name": "Cash", "dc": "D", "amount": "0.01"},
                    {"ledger_name": "Cash", "dc": "C", "amount": "0.01"},
                ],
                meta={"payroll": doc},
            )

        def done(voucher: Voucher) -> None:
            computed = (payroll_doc_of(voucher) or {}).get("computed") or {}
            schedule = computed.get("schedule") or [{}]
            QMessageBox.information(
                self,
                "Loan issued",
                f"{voucher.voucher_no} — EMI {computed.get('emi', '?')} for "
                f"{computed.get('months', '?')} months\n"
                f"First installment {schedule[0].get('period', '?')}, last "
                f"{schedule[-1].get('period', '?')} (total interest "
                f"{computed.get('total_interest', '?')}).",
            )
            self.accept()

        run(work, done, lambda e: error_box(self, "Loan", error_detail(e)))


class SettlementDialog(QDialog):
    """The full & final settlement: leave encashment, one extra earning,
    and the dues straight to the bank or the payable ledger."""

    def __init__(self, client: ApiClient, period: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self.setWindowTitle("Full & final settlement")
        self.resize(420, 300)

        self.employee = QLineEdit()
        self.period = QLineEdit(period)
        self.days = QDoubleSpinBox()
        self.days.setDecimals(1)
        self.days.setMaximum(365.0)
        self.per_day = QDoubleSpinBox()
        self.per_day.setDecimals(2)
        self.per_day.setMaximum(999_999.0)
        self.earning_name = QLineEdit()
        self.earning_name.setPlaceholderText("e.g. Sales Incentive (optional)")
        self.earning_amount = QDoubleSpinBox()
        self.earning_amount.setDecimals(2)
        self.earning_amount.setMaximum(999_999_999.0)
        self.bank = QLineEdit()
        self.bank.setPlaceholderText("empty = accrue the dues to the payable ledger")

        form = QFormLayout()
        form.addRow("Employee", self.employee)
        form.addRow("Period (YYYY-MM)", self.period)
        form.addRow("Leave encashment (days)", self.days)
        form.addRow("Encashment rate per day", self.per_day)
        form.addRow("Extra earning", self.earning_name)
        form.addRow("Earning amount", self.earning_amount)
        form.addRow("Pay dues direct from bank", self.bank)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Settle")
        buttons.accepted.connect(self._settle)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _settle(self) -> None:
        employee = self.employee.text().strip()
        if not employee:
            error_box(self, "Settlement", "The employee is required.")
            return
        doc: dict = {
            "mode": "settlement",
            "employee": employee,
            "period": self.period.text().strip(),
        }
        if self.days.value() > 0:
            doc["encashment"] = {
                "days": f"{self.days.value():.1f}",
                "per_day": f"{self.per_day.value():.2f}",
            }
        if self.earning_name.text().strip() and self.earning_amount.value() > 0:
            doc["earnings"] = [
                {
                    "name": self.earning_name.text().strip(),
                    "amount": f"{self.earning_amount.value():.2f}",
                }
            ]
        if self.bank.text().strip():
            doc["bank"] = self.bank.text().strip()

        def work():
            return self._client.create_voucher(
                voucher_type="JV",
                voucher_date=date.today(),
                entries=[
                    {"ledger_name": "Cash", "dc": "D", "amount": "0.01"},
                    {"ledger_name": "Cash", "dc": "C", "amount": "0.01"},
                ],
                meta={"payroll": doc},
            )

        def done(voucher: Voucher) -> None:
            computed = (payroll_doc_of(voucher) or {}).get("computed") or {}
            QMessageBox.information(
                self,
                "Settlement posted",
                f"{voucher.voucher_no} — net dues {computed.get('net', '?')} "
                f"({'paid direct' if computed.get('paid_direct') else 'accrued to the payable'}). "
                "The employee is relieved: runs, loans and further settlements "
                "now refuse; payouts stay legal.",
            )
            self.accept()

        run(work, done, lambda e: error_box(self, "Settlement", error_detail(e)))
