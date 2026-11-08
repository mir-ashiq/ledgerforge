"""The native payroll workbench, proven offscreen — ADR-029, Phase 8.

The Payroll page (the attendance register table with payable days, the
loans positions table, the payroll documents list naming every mode), the
attendance keying flow (grid → dry-run preview → import), the loan issue
flow (the confirmation quotes the engine's computed schedule), the
settlement flow, and the payslip save (a run's PDF, per employee). A stub
client stands in for the engine, the same discipline as
``test_native_gst``; the real end-to-end is ``test_native_smoke``.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QFileDialog, QMessageBox  # noqa: E402
from test_native_gui import StubClient  # noqa: E402

from ledgerforge.nativeapp.main_window import PAGES  # noqa: E402
from ledgerforge.nativeapp.models import Voucher  # noqa: E402
from ledgerforge.nativeapp.payroll import (  # noqa: E402
    AttendanceDialog,
    LoanDialog,
    PayrollPage,
    SettlementDialog,
)

ATTENDANCE = {
    "period": "2026-11",
    "rows": [
        {
            "employee": "Asha Verma",
            "present": "21.0",
            "absent": "1.0",
            "half": "2.0",
            "leave_paid": "1.0",
            "payable_days": "23.00",
            "note": None,
        }
    ],
}

LOANS = {
    "employees": [
        {
            "employee": "Asha Verma",
            "issued": "100000.00",
            "recovered": "5403.06",
            "outstanding": "94596.94",
            "loans": [
                {
                    "voucher_no": "JV-0042",
                    "principal": "100000.00",
                    "months": 20,
                    "emi": "5403.06",
                    "first_period": "2026-11",
                    "recovered": "5403.06",
                    "outstanding": "94596.94",
                    "schedule": [],
                }
            ],
        }
    ]
}


def _voucher(vid: int, no: str, doc: dict, amount: str = "100.00") -> Voucher:
    return Voucher(
        id=vid,
        uuid=f"u-{vid}",
        voucher_no=no,
        voucher_type="JV",
        voucher_date=date(2026, 11, 8),
        amount=Decimal(amount),
        narration=None,
        reversal_of=None,
        posted_at=datetime(2026, 11, 8, 10, 0),
        client_token="",
        meta={"payroll": doc},
        entries=(),
    )


RUN_DOC = {
    "mode": "run",
    "period": "2026-11",
    "employees": [{"employee": "Asha Verma", "components": []}],
}
LOAN_DOC = {
    "mode": "loan",
    "employee": "Asha Verma",
    "principal": "100000.00",
    "months": 20,
    "computed": {
        "emi": "5403.06",
        "months": 20,
        "total_interest": "8061.20",
        "schedule": [{"period": "2026-11"}, {"period": "2028-06"}],
    },
}
PAYOUT_DOC = {"mode": "payout", "employee": "Asha Verma", "amount": "5000.00"}


class PayrollStubClient(StubClient):
    """The engine's payroll answers: registers, loan positions, documents."""

    def __init__(self):
        super().__init__()
        self.attendance_imports: list[dict] = []
        self.vouchers_list = [
            _voucher(1, "JV-1", RUN_DOC, "34500.00"),
            _voucher(2, "JV-2", LOAN_DOC, "100000.00"),
            _voucher(3, "JV-3", PAYOUT_DOC, "5000.00"),
        ]
        self.pdfs: list[tuple[int, str]] = []

    def pack_json(self, key, **params):
        if key == "payroll-attendance":
            return ATTENDANCE
        if key == "payroll-loans":
            return LOANS
        raise AssertionError(f"unexpected pack report {key}")

    def attendance_import(self, period, entries, *, source=None, dry_run=False):
        self.attendance_imports.append({"period": period, "entries": entries, "dry_run": dry_run})
        return {
            "period": period,
            "parsed": len(entries),
            "new": len(entries),
            "duplicates": 0,
            "revision": 1,
            "dry_run": dry_run,
            "batch_id": None if dry_run else 7,
        }

    def voucher_pdf(self, voucher_id, *, employee=""):
        self.pdfs.append((voucher_id, employee))
        return b"%PDF-1.4 stub payslip", "Payslip.pdf"

    def create_voucher(self, **payload):
        self.posts.append(payload)
        doc = payload["meta"]["payroll"]
        if doc.get("mode") == "loan":
            return _voucher(
                9, "JV-9", {**doc, **{k: v for k, v in LOAN_DOC.items() if k == "computed"}}
            )
        return _voucher(8, "JV-8", {**doc, "computed": {"net": "26250.00", "paid_direct": False}})


def _drain(qtbot, condition, timeout: int = 3000) -> None:
    """Wait for the worker-thread answers to land on the GUI thread."""
    qtbot.waitUntil(condition, timeout=timeout)


@pytest.fixture()
def client():
    return PayrollStubClient()


@pytest.fixture()
def page(qtbot, client):
    page = PayrollPage(client)
    qtbot.addWidget(page)
    page.period_edit.setText("2026-11")
    page.reload()
    _drain(qtbot, lambda: page.attendance.rowCount() == 1 and page.loans.rowCount() == 1)
    return page


# ----------------------------------------------------------------- the page
def test_the_page_joins_the_window_after_gst():
    """Alt+7 lands on Payroll — the people workbench follows the compliance
    one (the window's own test walks the sidebar dynamically)."""
    assert PAGES[6] == ("payroll", "Payroll")
    assert len(PAGES) == 11  # +Data Exchange (ADR-030, after Payroll)


def test_the_register_and_positions_render(page):
    # the live register: raw counts and the payable-days arithmetic
    assert page.attendance.rowCount() == 1
    assert page.attendance.item(0, 0).text() == "Asha Verma"
    assert page.attendance.item(0, 5).text() == "23.00"

    # the loans fold: issued, recovered, outstanding
    assert page.loans.item(0, 1).text() == "100000.00"
    assert page.loans.item(0, 3).text() == "94596.94"

    # the documents list names every mode, newest first
    kinds = [page.documents.item(r, 2).text() for r in range(page.documents.rowCount())]
    assert kinds == ["Payout", "Loan", "Run"]
    assert page.documents.item(2, 3).text().startswith("Asha Verma · 2026-11")


def test_payslip_save_needs_a_run_selected(page, qtbot, monkeypatch, tmp_path):
    # a payout does not enable the payslip button — only runs print as payslips
    page.documents.selectRow(0)
    assert page.payslip_button.isEnabled() is False

    # the run does; the saved file is the engine's bytes
    notices = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: notices.append(a)))
    target = tmp_path / "Payslip Asha Verma 2026-11.pdf"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "PDF (*.pdf)"))
    )
    page.documents.selectRow(2)
    assert page.payslip_button.isEnabled()
    page._save_payslip()
    _drain(qtbot, lambda: client_pdfs(page))
    assert target.read_bytes() == b"%PDF-1.4 stub payslip"
    assert notices


def client_pdfs(page):
    return len(page._client.pdfs) == 1


# ------------------------------------------------------- the attendance flow
def test_attendance_dialog_previews_then_imports(qtbot, monkeypatch):
    client = PayrollStubClient()
    dialog = AttendanceDialog(client, "2026-11")
    qtbot.addWidget(dialog)

    dialog.grid.item(0, 0).setText("Asha Verma")
    dialog.grid.item(0, 1).setText("21")
    dialog.grid.item(0, 3).setText("2")  # half-days

    assert dialog.import_button.isEnabled() is False  # preview gates the import
    dialog._preview()
    _drain(qtbot, lambda: len(client.attendance_imports) == 1)
    assert client.attendance_imports[0]["dry_run"] is True
    dialog._previewed(client.attendance_imports[0])
    assert dialog.import_button.isEnabled()

    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    dialog._import()
    _drain(qtbot, lambda: len(client.attendance_imports) == 2)
    assert client.attendance_imports[1]["dry_run"] is False
    assert client.attendance_imports[1]["entries"] == [
        {"employee": "Asha Verma", "present": 21.0, "absent": 0.0, "half": 2.0, "leave_paid": 0.0}
    ]


# --------------------------------------------------------------- the loans
def test_loan_dialog_issues_and_quotes_the_engine(qtbot, monkeypatch):
    client = PayrollStubClient()
    dialog = LoanDialog(client)
    qtbot.addWidget(dialog)

    dialog.employee.setText("Asha Verma")
    dialog.bank.setText("HDFC Current")
    notices = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: notices.append(a)))

    dialog._issue()
    _drain(qtbot, lambda: len(client.posts) == 1)
    assert len(client.posts) == 1
    posted = client.posts[0]["meta"]["payroll"]
    assert posted["mode"] == "loan" and posted["employee"] == "Asha Verma"
    assert posted["principal"] == "100000.00" and posted["months"] == 20
    # the confirmation quotes the engine's schedule, never a GUI arithmetic
    assert "5403.06" in notices[0][2] and "2026-11" in notices[0][2]


# --------------------------------------------------------- the settlement
def test_settlement_dialog_posts_the_fnf(qtbot, monkeypatch):
    client = PayrollStubClient()
    dialog = SettlementDialog(client, "2026-11")
    qtbot.addWidget(dialog)

    dialog.employee.setText("Asha Verma")
    dialog.days.setValue(12.5)
    dialog.per_day.setValue(1500.0)
    dialog.earning_name.setText("Sales Incentive")
    dialog.earning_amount.setValue(10000.0)
    notices = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: notices.append(a)))

    dialog._settle()
    _drain(qtbot, lambda: len(client.posts) == 1)
    posted = client.posts[0]["meta"]["payroll"]
    assert posted["mode"] == "settlement"
    assert posted["encashment"] == {"days": "12.5", "per_day": "1500.00"}
    assert posted["earnings"] == [{"name": "Sales Incentive", "amount": "10000.00"}]
    assert "relieved" in notices[0][2]
