"""The native GST compliance workbench, proven offscreen — ADR-028, Phase 7.

The GST page (the downloads receipts table, the registration picker fed
from them, the reconciliation table with all four states, the ITC summary
line with its ineligible clause), the import flow (paste the adapter's
JSON → the preview receipt → the real import), the statutory PDF buttons,
and the Reports page's new GST entries with their year field. A stub
client stands in for the engine, the same discipline as
``test_native_banking``; the real end-to-end is ``test_native_smoke``.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox  # noqa: E402
from test_native_gui import StubClient  # noqa: E402

from ledgerforge.nativeapp.gst import GstPage, ImportDialog  # noqa: E402
from ledgerforge.nativeapp.main_window import PAGES, MainWindow  # noqa: E402
from ledgerforge.nativeapp.reports import PDF_PACKS, ReportsPage  # noqa: E402

DOWNLOADS = [
    {
        "kind": "2b",
        "period": "2026-10",
        "gstin": "29AAPFU0939A1ZV",
        "source": "2b-oct-ka.json",
        "line_count": 5,
        "imported_at": "2026-11-08T09:00:00",
    },
    {
        "kind": "2b",
        "period": "2026-10",
        "gstin": "27AAECS1234F1Z5",
        "source": None,
        "line_count": 1,
        "imported_at": None,
    },
]

RECON = {
    "period": "2026-10",
    "kind": "2b",
    "gstin": None,
    "counts": {"matched": 2, "mismatched": 1, "missing_books": 1, "missing_portal": 1},
    "unmatchable_books": 0,
    "itc": {
        "books": "360.00",
        "portal": "380.00",
        "difference": "-20.00",
        "portal_ineligible_rows": 1,
        "portal_ineligible_tax": "10.00",
    },
    "rows": [
        {
            "state": "matched",
            "supplier_gstin": "29AAPFU0939A1ZV",
            "party": "Acme Traders",
            "doc_type": "INV",
            "doc_no": "A-101",
            "doc_date": "2026-10-04",
            "books_taxable": "1000.00",
            "books_tax": "180.00",
            "portal_taxable": "1000.00",
            "portal_tax": "180.00",
            "reason": "",
        },
        {
            "state": "matched",
            "supplier_gstin": "29AAPFU0939A1ZV",
            "party": "Acme Traders",
            "doc_type": "DN",
            "doc_no": "A-102",
            "doc_date": "2026-10-12",
            "books_taxable": "500.00",
            "books_tax": "90.00",
            "portal_taxable": "500.00",
            "portal_tax": "90.00",
            "reason": "",
        },
        {
            "state": "mismatched",
            "supplier_gstin": "27AAECS1234F1Z5",
            "party": "Bombay Supply Co",
            "doc_type": "INV",
            "doc_no": "B-55",
            "doc_date": "2026-10-08",
            "books_taxable": "800.00",
            "books_tax": "144.00",
            "portal_taxable": "805.00",
            "portal_tax": "144.00",
            "reason": "taxable 800.00↔805.00",
        },
        {
            "state": "missing_books",
            "supplier_gstin": "07AABCU9603R1ZM",
            "party": "Delhi Vendor",
            "doc_type": "INV",
            "doc_no": "D-9",
            "doc_date": "2026-10-19",
            "books_taxable": None,
            "books_tax": None,
            "portal_taxable": "5000.00",
            "portal_tax": "10.00",
            "reason": "the portal drafted it, the books do not hold it",
        },
        {
            "state": "missing_portal",
            "supplier_gstin": "29AAPFU0939A1ZV",
            "party": "Acme Traders",
            "doc_type": "INV",
            "doc_no": "A-103",
            "doc_date": "2026-10-25",
            "books_taxable": "600.00",
            "books_tax": "108.00",
            "portal_taxable": None,
            "portal_tax": None,
            "reason": "the books claim it, the portal has not drafted it",
        },
    ],
}

RECEIPT = {
    "kind": "2b",
    "period": "2026-10",
    "gstin": "29AAPFU0939A1ZV",
    "source": "2b-oct-ka.json",
    "parsed": 5,
    "new": 5,
    "duplicates": 0,
    "revision": 1,
    "dry_run": False,
}


class GstStubClient(StubClient):
    """The engine's GST answers, canned for the offscreen page."""

    def __init__(self):
        super().__init__()
        self.imports: list[dict] = []
        self.recon_calls: list[tuple[str, dict]] = []
        self.pack_calls: list[tuple[str, dict]] = []

    def gst_downloads(self):
        return [dict(d) for d in DOWNLOADS]

    def gst_import(self, text, *, source=None, dry_run=False):
        self.imports.append({"text": text, "source": source, "dry_run": dry_run})
        return {**RECEIPT, "dry_run": dry_run}

    def pack_data(self, key, **params):
        self.recon_calls.append((key, dict(params)))
        if key == "gst-recon":
            return RECON
        return super().pack_json(key, **params)

    def pack(self, key, **params):
        self.pack_calls.append((key, dict(params)))
        return b"%PDF-1.4 stub", "application/pdf", "stub.pdf"


@pytest.fixture()
def gst_page(qtbot):
    client = GstStubClient()
    page = GstPage(client)
    qtbot.addWidget(page)
    page.reload()
    qtbot.waitUntil(lambda: page.downloads.rowCount() == 2, timeout=3000)
    return page, client


def _drain(qtbot, condition, timeout: int = 3000) -> None:
    """Wait for the worker-thread answers to land on the GUI thread."""
    qtbot.waitUntil(condition, timeout=timeout)


# --------------------------------------------------------------- the receipts
def test_page_loads_downloads_and_registrations(gst_page):
    page, _client = gst_page
    assert page.downloads.rowCount() == 2
    # row 0 is the newest: 2b, its period, the stamp trimmed to the minute
    assert page.downloads.item(0, 0).text() == "2b"
    assert page.downloads.item(0, 1).text() == "2026-10"
    assert page.downloads.item(0, 3).text() == "2b-oct-ka.json"
    assert page.downloads.item(0, 5).text() == "2026-11-08 09:00:00"
    # a receipt without a source label names the honest blank
    assert page.downloads.item(1, 3).text() == "—"
    assert page.downloads.item(1, 5).text() == "—"

    # the registration picker: all + one entry per distinct GSTIN, newest first
    assert page.registrations.count() == 3
    assert page.registrations.itemText(0) == "All registrations"
    assert page.registrations.itemData(0) == ""
    assert page.registrations.itemText(1) == "29AAPFU0939A1ZV"
    assert page.registrations.itemText(2) == "27AAECS1234F1Z5"
    assert page._registration() == ""


# ------------------------------------------------------------ the comparison
def test_compare_renders_every_state(gst_page, qtbot):
    page, client = gst_page
    page.period_edit.setText("2026-10")
    page._run_recon()
    _drain(qtbot, lambda: page.table.rowCount() == 5)

    # one worker call: the pack's JSON fold, period + statement kind
    assert client.recon_calls == [("gst-recon", {"period": "2026-10", "kind": "2b"})]

    states = [page.table.item(r, 0).text() for r in range(page.table.rowCount())]
    assert set(states) == {"matched", "mismatched", "missing_books", "missing_portal"}
    # the document column carries type and number together
    assert page.table.item(0, 3).text() == "INV A-101"
    # the missing-in-books row shows its books side as the honest blank
    missing = states.index("missing_books")
    assert page.table.item(missing, 5).text() == "—"
    assert page.table.item(missing, 6).text() == "5000.00"
    # the summary line counts every state and prices the ITC both sides
    summary = page.summary_label.text()
    assert "Matched 2" in summary and "Missing in portal 1" in summary
    assert "ITC:" in summary and "difference" in summary
    assert "₹-20.00" in summary
    assert "ITC-ineligible" in summary  # the portal's own verdict rides along


def test_compare_demands_a_period(gst_page, monkeypatch):
    page, _client = gst_page
    # the modal box is patched away — offscreen has no user to click OK
    boxed = []
    monkeypatch.setattr("ledgerforge.nativeapp.gst.error_box", lambda *a, **k: boxed.append(a))
    page.period_edit.setText("")
    page._run_recon()
    assert boxed and "period like 2026-10" in boxed[0][2]
    assert page.table.rowCount() == 0  # no worker fired, no rows rendered


# ---------------------------------------------------------------- the import
def test_import_dialog_flow(gst_page, qtbot, monkeypatch):
    page, client = gst_page

    # the dialog itself: paste the adapter JSON, name the source
    dialog = ImportDialog(page)
    dialog.text_edit.setPlainText('{"kind": "lf-gst-2b/1"}')
    dialog.source_edit.setText("2b-oct-ka.json")
    text, source = dialog.values()
    assert "lf-gst-2b/1" in text
    assert source == "2b-oct-ka.json"

    # the page's chain: the page drives the preview dry run, asks, imports
    class StubDialog:
        def __init__(self, parent):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def values(self):
            return '{"kind": "lf-gst-2b/1"}', "2b-oct-ka.json"

    asked = []
    notices = []
    monkeypatch.setattr("ledgerforge.nativeapp.gst.ImportDialog", StubDialog)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: asked.append(a) or QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        staticmethod(lambda *a, **k: notices.append(a)),
    )
    page._import_dialog()
    _drain(qtbot, lambda: len(client.imports) == 2)

    # first the preview, then the real thing — same text, same source
    assert client.imports[0]["dry_run"] is True
    assert client.imports[1]["dry_run"] is False
    assert client.imports[1]["text"] == '{"kind": "lf-gst-2b/1"}'
    assert client.imports[1]["source"] == "2b-oct-ka.json"
    assert asked, "the preview must ask before the import commits"
    assert notices, "the receipt must land in a box"


# --------------------------------------------------------- the statutory PDFs
def test_pdf_buttons_write_the_bytes(gst_page, qtbot, monkeypatch, tmp_path):
    page, client = gst_page
    notices = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: notices.append(a)))

    target = tmp_path / "GSTR3B 2026-10.pdf"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "PDF (*.pdf)"))
    )
    page.period_edit.setText("2026-10")
    page._save_pdf("gstr3b")
    _drain(qtbot, lambda: len(client.pack_calls) == 1)
    assert target.read_bytes() == b"%PDF-1.4 stub"
    assert client.pack_calls == [("gstr3b", {"period": "2026-10"})]
    assert notices

    # GSTR-9: the period's year, or the current one when the field is empty
    yearly = tmp_path / "GSTR9.pdf"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(yearly), "PDF (*.pdf)"))
    )
    page.period_edit.clear()
    page._save_pdf("gstr9", year=True)
    _drain(qtbot, lambda: len(client.pack_calls) == 2)
    assert client.pack_calls[1] == ("gstr9", {"period": str(date.today().year)})
    assert yearly.read_bytes() == b"%PDF-1.4 stub"


# -------------------------------------------------------- the reports entries
def test_reports_page_gst_entries(qtbot):
    page = ReportsPage(GstStubClient())
    qtbot.addWidget(page)

    labels = [page.report_box.itemText(i) for i in range(page.report_box.count())]
    assert "GSTR-3B Summary (GST pack)" in labels
    assert "GSTR-2A/2B Reconciliation (GST pack)" in labels
    assert "GSTR-9 Scaffolding (GST pack)" in labels
    assert {"gstr3b", "gst-recon", "gstr9"} <= PDF_PACKS

    # GSTR-9 needs the year, not the period — and the year must be typed
    page.select_report("gstr9")
    assert page.year_edit.isVisibleTo(page)
    assert not page.period_edit.isVisibleTo(page)
    with pytest.raises(ValueError):
        page._load_gstr9()
    page.year_edit.setText("2026")
    content, _media, _name = page._load_gstr9()()
    assert content == b"%PDF-1.4 stub"

    # the monthly GST reports need the period
    page.select_report("gstr3b")
    assert page.period_edit.isVisibleTo(page)
    assert not page.year_edit.isVisibleTo(page)


# ------------------------------------------------------------- the main window
def test_main_window_has_gst_page(qtbot):
    window = MainWindow(GstStubClient(), "/tmp/lf-native-test")
    qtbot.addWidget(window)

    assert PAGES[5] == ("gst", "GST")
    labels = [window.sidebar.item(i).text() for i in range(window.sidebar.count())]
    assert labels == [label for _key, label in PAGES]
    assert labels[5] == "GST"
    assert window.gst is window.pages.widget(5)
