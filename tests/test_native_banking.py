"""The native banking surfaces, proven offscreen — ADR-026, Phase 5.

The Banking page (bank picker, the line book with match state, the
suggestions pane, match/un-match), the import flow (format + text through
the client, the receipt surfaced), and the cheque register dialog (the
pack's fold rendered in a table). A stub client stands in for the engine,
the same discipline as ``test_native_gui``; the real end-to-end is
``test_native_smoke``.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from test_native_gui import StubClient  # noqa: E402

from ledgerforge.nativeapp.banking import (  # noqa: E402
    BankingPage,
    ImportDialog,
    RegisterDialog,
)


class BankingStubClient(StubClient):
    """The engine's banking answers, canned for the offscreen page."""

    def __init__(self):
        super().__init__()
        self.imports: list[dict] = []
        self.matches: list[dict] = []
        self.unmatches: list[int] = []
        self.lines_payload = {
            "ledger_id": 7,
            "ledger_name": "Stub Bank",
            "lines": [
                {
                    "id": 101,
                    "batch_id": 1,
                    "value_date": "2026-04-03",
                    "dc": "D",
                    "amount": "2500.00",
                    "reference": "UPI123456",
                    "narrative": "UPI/Payment to Ramesh",
                    "cheque_no": None,
                    "match": None,
                },
                {
                    "id": 102,
                    "batch_id": 1,
                    "value_date": "2026-04-05",
                    "dc": "C",
                    "amount": "15000.00",
                    "reference": "NEFT987",
                    "narrative": "NEFT Credit from Acme",
                    "cheque_no": None,
                    "match": {
                        "voucher_id": 11,
                        "voucher_no": "RCT-2026-27-00002",
                        "voucher_date": "2026-04-05",
                        "entry_id": 21,
                        "matched_at": "2026-11-08T09:00:00+00:00",
                        "matched_by": "anonymous",
                        "unmatched_at": None,
                        "live": True,
                    },
                },
            ],
        }
        self.suggestions_payload = {
            "ledger_id": 7,
            "window_days": 7,
            "lines": [
                {
                    "statement_line_id": 101,
                    "value_date": "2026-04-03",
                    "dc": "D",
                    "amount": "2500.00",
                    "reference": "UPI123456",
                    "narrative": "UPI/Payment to Ramesh",
                    "cheque_no": None,
                    "suggestions": [
                        {
                            "entry_id": 31,
                            "voucher_id": 12,
                            "voucher_no": "PYT-2026-27-00001",
                            "voucher_date": "2026-04-03",
                            "dc": "D",
                            "amount": "2500.00",
                            "narration": "UPI to Ramesh",
                            "score": 70,
                            "reasons": ["amount exact", "0d apart"],
                        }
                    ],
                }
            ],
        }
        self.reconciliation_payload = {
            "ledger_id": 7,
            "ledger_name": "Stub Bank",
            "as_of": "2026-04-30",
            "book_balance": "15000.00",
            "statement_balance": "12500.00",
            "statement_balance_source": "given",
            "reconciled_balance": "15000.00",
            "unmatched_statement_total": "-2500.00",
            "unmatched_book_total": "0.00",
            "pair_difference_total": "0.00",
            "rows": [
                {
                    "kind": "stmt_debit",
                    "label": "Payment per bank, not in books",
                    "date": "2026-04-03",
                    "amount": "2500.00",
                    "effect": "-2500.00",
                    "reference": "UPI123456",
                    "narration": "UPI/Payment to Ramesh",
                }
            ],
            "proof": {"lhs": "2500.00", "rhs": "2500.00", "ok": True},
        }
        self.register_payload = {
            "as_of": "2026-04-30",
            "counts": {"issued": 1, "presented": 0, "cleared": 0},
            "rows": [
                {
                    "bank_ledger": "Stub Bank",
                    "no": "448812",
                    "voucher_no": "PYT-2026-27-00005",
                    "voucher_date": "2026-04-01",
                    "issued_on": "2026-04-01",
                    "matures_on": None,
                    "amount": "5000.00",
                    "status": "issued",
                    "presented_on": None,
                    "cleared_by": None,
                    "pdc": False,
                    "matured_as_of": False,
                }
            ],
        }

    # ------------------------------------------------------------- banking
    def bank_ledgers(self):
        return [{"id": 7, "name": "Stub Bank", "balance": "15000.00"}]

    def bank_lines(self, ledger_id, status="all"):
        if status == "matched":
            kept = [ln for ln in self.lines_payload["lines"] if ln["match"]]
        elif status == "unmatched":
            kept = [ln for ln in self.lines_payload["lines"] if not ln["match"]]
        else:
            kept = self.lines_payload["lines"]
        return {**self.lines_payload, "lines": kept}

    def bank_suggestions(self, ledger_id, window=7):
        return self.suggestions_payload

    def bank_match(self, ledger_id, target):
        self.matches.append(target)
        return {"statement_line_id": target["statement_line_id"], "matched": True}

    def bank_unmatch(self, statement_line_id):
        self.unmatches.append(statement_line_id)
        return {"statement_line_id": statement_line_id, "unmatched": True}

    def bank_reconciliation(self, ledger_id, as_of, statement_balance=None):
        return self.reconciliation_payload

    def bank_import(self, ledger_id, fmt, text, dry_run=False, source=None, opening_balance=None):
        self.imports.append(
            {"format": fmt, "text": text, "dry_run": dry_run, "ledger_id": ledger_id}
        )
        return {
            "new": 2 if not dry_run else 0,
            "duplicates": 0,
            "parsed": 2,
            "ledger_name": "Stub Bank",
        }

    def pack_json(self, key, **params):
        if key == "banking-cheques":
            return self.register_payload
        return super().pack_json(key, **params)


@pytest.fixture()
def banking_page(qtbot):
    client = BankingStubClient()
    page = BankingPage(client)
    qtbot.addWidget(page)
    page.reload()
    qtbot.waitUntil(lambda: page.table.rowCount() == 2, timeout=3000)
    return page, client


def _drain(qtbot, condition, timeout: int = 3000) -> None:
    """Wait for the worker-thread answers to land on the GUI thread."""
    qtbot.waitUntil(condition, timeout=timeout)


def test_page_lists_lines_with_match_state(banking_page):
    page, _client = banking_page
    assert page.table.rowCount() == 2
    # withdrawal column carries the D line, deposit the C line
    assert page.table.item(0, 1).text() == "2500.00"
    assert page.table.item(1, 2).text() == "15000.00"
    # the matched line names its voucher
    assert "RCT-2026-27-00002" in page.table.item(1, 5).text()


def test_suggestions_pane_and_match_action(banking_page, qtbot):
    page, client = banking_page
    page.table.selectRow(0)  # the unmatched 2500 withdrawal
    assert page.suggestions.rowCount() == 1
    assert page.match_button.isEnabled()
    assert not page.unmatch_button.isEnabled()

    page._match_selected()
    _drain(qtbot, lambda: len(client.matches) == 1)
    assert client.matches == [
        {
            "statement_line_id": 101,
            "entry_id": 31,
            "voucher_id": 12,
            "voucher_date": "2026-04-03",
        }
    ]


def test_unmatch_action_names_the_line(banking_page, qtbot):
    page, client = banking_page
    page.table.selectRow(1)  # the matched credit
    assert page.unmatch_button.isEnabled()
    page._unmatch_selected()
    _drain(qtbot, lambda: len(client.unmatches) == 1)
    assert client.unmatches == [102]


def test_match_disabled_for_matched_lines(banking_page):
    page, _client = banking_page
    page.table.selectRow(1)
    assert not page.match_button.isEnabled()


def test_import_dialog_collects_values(banking_page):
    page, _client = banking_page
    dialog = ImportDialog(page)
    dialog.format_box.setCurrentIndex(1)  # MT940
    dialog.text_box.setPlainText(":20:X\n:61:260403D2500,00NTRF001\n")
    dialog.source_edit.setText("april.sta")
    fmt, text, source, opening = dialog.values()
    assert (fmt, source) == ("mt940", "april.sta")
    assert ":61:" in text


def test_register_dialog_renders_the_fold(banking_page, qtbot):
    page, _client = banking_page
    dialog = RegisterDialog(page, "Stub Bank")
    dialog._run()
    _drain(qtbot, lambda: dialog.table.rowCount() == 1)
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 1).text() == "448812"
    assert dialog.table.item(0, 6).text() == "issued"
