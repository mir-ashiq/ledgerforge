"""The Data Exchange page, proven offscreen — ADR-030, Phase 9.

The page (import pane with preview-then-commit, the Tally pane with the
verification table, the export pane over the engine's catalogue) against
a stub client that records what the GUI asked the engine — the
``test_native_payroll`` discipline: zero business logic in the GUI, every
number the engine's string, every action one API call. The real
end-to-end is ``test_native_smoke``.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from ledgerforge.nativeapp.dataexchange import DataExchangePage  # noqa: E402
from ledgerforge.nativeapp.main_window import PAGES  # noqa: E402

TALLY_RECEIPT = {
    "kind": "tally",
    "dry_run": False,
    "masters": {
        "groups_created": 1,
        "ledgers_created": 3,
        "existing": 0,
        "stock_items_created": 1,
        "stock_items_existing": 0,
    },
    "auto_ledgers": ["Round Ledger"],
    "stock_openings": [{"item": "Steel Rod", "qty": "120", "note": "re-key through Inventory"}],
    "openings": {
        "voucher_no": "OPN-1",
        "posted": True,
        "replayed": False,
        "total_debit": "200000.00",
        "total_credit": "200000.00",
        "balanced_to": None,
    },
    "vouchers": {
        "in_file": 2,
        "mapped": 2,
        "posted": 2,
        "replayed": 0,
        "skipped": [
            {
                "vchtype": "Physical Stock",
                "date": "2026-04-10",
                "identity": "3",
                "reason": "no LedgerForge mapping",
            }
        ],
    },
    "verification": {
        "source_total": "0.00",
        "imported_total": "0.00",
        "differences": 0,
        "rows": [
            {
                "ledger": "State Bank",
                "source": "150000",
                "imported": "150000",
                "difference": "0.00",
            },
            {"ledger": "Ravi Traders", "source": "3860", "imported": "3860", "difference": "0.00"},
        ],
    },
}


class StubClient:
    """A synchronous ApiClient stand-in that records the calls."""

    def __init__(self):
        self.book_id = 1
        self.import_calls: list[dict] = []
        self.tally_calls: list[dict] = []
        self.export_calls: list[tuple] = []

    def import_file(
        self, kind, fmt, content, *, source=None, dry_run=False, balance_to=None, ledger_id=None
    ):
        self.import_calls.append({"kind": kind, "fmt": fmt, "dry_run": dry_run})
        return {
            "kind": kind,
            "dry_run": dry_run,
            "ledgers_created": 2,
            "existing": 1,
            "posted": 0 if dry_run else 1,
            "replayed": 0,
        }

    def tally_import(self, xml, *, source=None, dry_run=False, type_map=None, balance_to=None):
        self.tally_calls.append({"dry_run": dry_run})
        return TALLY_RECEIPT

    def exports_catalog(self):
        return {
            "formats": ["csv", "xlsx"],
            "surfaces": [
                {
                    "key": "trial-balance",
                    "label": "Trial Balance",
                    "path": "/api/v1/reports/trial-balance",
                    "params": ["as_of"],
                    "group": "Reports",
                },
                {
                    "key": "day-book",
                    "label": "Day Book",
                    "path": "/api/v1/vouchers",
                    "params": [],
                    "group": "Lists",
                },
            ],
        }

    def export_bytes(self, path, params, fmt):
        self.export_calls.append((path, params, fmt))
        return b"ledger_name,group_name\nCash,Cash-in-Hand\n", "trial-balance.csv"


def _drain(qtbot, condition, timeout: int = 3000) -> None:
    """The tasks helper delivers on the Qt loop — wait for the condition."""
    qtbot.waitUntil(condition, timeout=timeout)


@pytest.fixture()
def page(qtbot):
    client = StubClient()
    widget = DataExchangePage(client)
    qtbot.addWidget(widget)
    return widget, client


def _answer_yes():
    """Make the preview's confirmation dialog answer Yes."""
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)


def test_the_page_joins_the_window_after_payroll():
    keys = [key for key, _label in PAGES]
    assert keys.index("dataexchange") == keys.index("payroll") + 1
    assert len(PAGES) == 11


def test_catalog_loads_into_the_export_pane(page):
    widget, client = page
    widget.reload()
    widget._catalog_loaded(client.exports_catalog())
    assert widget.surface_combo.count() == 2
    assert widget.surface_combo.itemText(0) == "Trial Balance"
    assert widget.params_edit.text().startswith("parameters: as_of")


def test_import_flow_previews_then_commits(page, qtbot, monkeypatch, tmp_path):
    widget, client = page
    source = tmp_path / "chart.csv"
    source.write_text("kind,name,parent\nledger,Stub Co,Sundry Debtors\n")

    monkeypatch.setattr(
        "ledgerforge.nativeapp.dataexchange.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(source), "")),
    )
    _answer_yes()

    widget._import_dialog()
    _drain(qtbot, lambda: len(client.import_calls) == 2)
    # the preview ran first, then the commit — dry_run False on the second
    assert [call["dry_run"] for call in client.import_calls] == [True, False]
    assert "imported" in widget.import_status.text()


def test_preview_discarded_writes_nothing(page, qtbot, monkeypatch, tmp_path):
    widget, client = page
    source = tmp_path / "chart.csv"
    source.write_text("kind,name,parent\nledger,Stub Co,Sundry Debtors\n")
    monkeypatch.setattr(
        "ledgerforge.nativeapp.dataexchange.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(source), "")),
    )
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    widget._import_dialog()
    _drain(qtbot, lambda: len(client.import_calls) == 1)
    assert len(client.import_calls) == 1  # the preview only
    assert client.import_calls[0]["dry_run"] is True
    assert "nothing written" in widget.import_status.text()


def test_tally_flow_shows_the_verification_table(page, qtbot, monkeypatch, tmp_path):
    widget, client = page
    source = tmp_path / "tally.xml"
    source.write_text("<ENVELOPE/>")
    monkeypatch.setattr(
        "ledgerforge.nativeapp.dataexchange.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(source), "")),
    )
    _answer_yes()

    widget._tally_dialog()
    _drain(qtbot, lambda: len(client.tally_calls) == 2)
    assert [call["dry_run"] for call in client.tally_calls] == [True, False]
    # the verification table carries the receipt's rows
    assert widget.verification.rowCount() == 2
    assert widget.verification.item(0, 0).text() == "State Bank"
    assert widget.verification.item(0, 3).text() == "0.00"


def test_export_flow_fetches_and_saves(page, qtbot, monkeypatch, tmp_path):
    widget, client = page
    widget.reload()
    widget._catalog_loaded(client.exports_catalog())
    target = tmp_path / "out.csv"
    monkeypatch.setattr(
        "ledgerforge.nativeapp.dataexchange.QFileDialog.getSaveFileName",
        staticmethod(lambda *a, **k: (str(target), "")),
    )
    # the surface asks for its parameter (as_of) — answer empty for the default
    from PySide6.QtWidgets import QInputDialog

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", True)))

    widget._export_dialog()
    _drain(qtbot, lambda: len(client.export_calls) == 1)
    assert client.export_calls == [("/api/v1/reports/trial-balance", {}, "csv")]
    assert target.read_bytes().startswith(b"ledger_name")
    assert "exported" in widget.import_status.text()
