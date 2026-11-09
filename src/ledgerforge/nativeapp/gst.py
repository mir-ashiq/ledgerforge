"""The GST page — the compliance workbench (ADR-028, roadmap Phase 7):
import the portal's GSTR-2A/2B download (paste the adapter's JSON, preview
first — the receipt shows what would land), walk the reconciliation table
(matched / mismatched / missing in books / missing in portal, every reason
named), read the ITC both sides claim, and take the statutory PDFs (GSTR-1,
3B, 9, the comparison) home. Zero business logic in the GUI — every number
is the engine's string (Law 3), every action one API call.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .client import ApiClient
from .tasks import run
from .widgets import error_box, error_detail, fill_table, make_table, text_item

#: pack key -> the file-name title of its statutory PDF (the save button's
#: suggested name: "GSTR3B 2026-10.pdf").
_PDF_TITLES = {
    "gst-recon": "GSTR Comparison",
    "gstr3b": "GSTR3B",
    "gstr9": "GSTR9",
}


class GstPage(QWidget):
    """The compliance workbench: one registration, one period, one truth."""

    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Registration"))
        self.registrations = QComboBox()
        self.registrations.setAccessibleName("GST registration")
        self.registrations.addItem("All registrations", "")
        bar.addWidget(self.registrations)
        bar.addWidget(QLabel("Statement"))
        self.kind_box = QComboBox()
        self.kind_box.setAccessibleName("Statement kind")
        self.kind_box.addItem("GSTR-2B", "2b")
        self.kind_box.addItem("GSTR-2A", "2a")
        bar.addWidget(self.kind_box)
        bar.addWidget(QLabel("period (YYYY-MM)"))
        self.period_edit = QLineEdit()
        self.period_edit.setPlaceholderText("e.g. 2026-10")
        bar.addWidget(self.period_edit)
        self.run_button = QPushButton("Compare")
        self.run_button.clicked.connect(self._run_recon)
        bar.addWidget(self.run_button)
        self.import_button = QPushButton("Import Download…")
        self.import_button.clicked.connect(self._import_dialog)
        bar.addWidget(self.import_button)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.reload)
        bar.addWidget(self.refresh_button)
        bar.addStretch(1)

        self.downloads = make_table(["Kind", "Period", "GSTIN", "Source", "Lines", "Imported"])
        self.downloads.setColumnWidth(0, 70)
        self.downloads.setColumnWidth(1, 90)
        self.downloads.setColumnWidth(2, 170)
        self.downloads.setColumnWidth(3, 220)
        self.downloads.setColumnWidth(4, 70)
        self.downloads.setFixedHeight(140)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)

        self.table = make_table(
            [
                "State",
                "GSTIN",
                "Party",
                "Document",
                "Date",
                "Books taxable",
                "Portal taxable",
                "Books tax",
                "Portal tax",
                "Reason",
            ]
        )
        self.table.setColumnWidth(0, 120)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 170)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(9, 260)

        pdf_bar = QHBoxLayout()
        self.comparison_pdf_button = QPushButton("Save Comparison PDF…")
        self.comparison_pdf_button.clicked.connect(lambda: self._save_pdf("gst-recon"))
        pdf_bar.addWidget(self.comparison_pdf_button)
        self.gstr3b_pdf_button = QPushButton("Save GSTR-3B PDF…")
        self.gstr3b_pdf_button.clicked.connect(lambda: self._save_pdf("gstr3b"))
        pdf_bar.addWidget(self.gstr3b_pdf_button)
        self.gstr9_pdf_button = QPushButton("Save GSTR-9 PDF…")
        self.gstr9_pdf_button.clicked.connect(lambda: self._save_pdf("gstr9", year=True))
        pdf_bar.addWidget(self.gstr9_pdf_button)
        pdf_bar.addStretch(1)
        self.pdf_buttons = [
            self.comparison_pdf_button,
            self.gstr3b_pdf_button,
            self.gstr9_pdf_button,
        ]

        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(QLabel("Imported downloads"))
        layout.addWidget(self.downloads)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.table, stretch=1)
        layout.addLayout(pdf_bar)

    # ------------------------------------------------------------- loading
    def reload(self) -> None:
        run(lambda: self._client.gst_downloads(), self._downloads_loaded, self._failed)

    def _downloads_loaded(self, downloads: list[dict]) -> None:
        fill_table(
            self.downloads,
            [
                [
                    d["kind"],
                    d["period"],
                    d["gstin"],
                    d.get("source") or "—",
                    str(d["line_count"]),
                    (d.get("imported_at") or "")[:19].replace("T", " ") or "—",
                ]
                for d in downloads
            ],
        )
        current = self.registrations.currentData()
        self.registrations.blockSignals(True)
        self.registrations.clear()
        self.registrations.addItem("All registrations", "")
        seen: list[str] = []
        for d in downloads:  # newest first, the server's order
            gstin = d.get("gstin") or ""
            if gstin and gstin not in seen:
                seen.append(gstin)
                self.registrations.addItem(gstin, gstin)
        if current is not None:
            index = self.registrations.findData(current)
            if index >= 0:
                self.registrations.setCurrentIndex(index)
        self.registrations.blockSignals(False)

    def _registration(self) -> str:
        return self.registrations.currentData() or ""

    def _period(self) -> str:
        return self.period_edit.text().strip()

    def _failed(self, error: object) -> None:
        error_box(self, "GST", error_detail(error))

    # -------------------------------------------------------- the comparison
    def _run_recon(self) -> None:
        period = self._period()
        if len(period) != 7 or period[4] != "-" or not period.replace("-", "").isdigit():
            error_box(self, "GST", "The comparison needs a period like 2026-10 (YYYY-MM).")
            return
        params: dict = {"period": period, "kind": self.kind_box.currentData()}
        gstin = self._registration()
        if gstin:
            params["gstin"] = gstin
        run(
            lambda: self._client.pack_data("gst-recon", **params),
            self._recon_loaded,
            self._failed,
        )

    def _recon_loaded(self, data: dict) -> None:
        counts = data["counts"]
        itc = data["itc"]
        fill_table(
            self.table,
            [
                [
                    row["state"],
                    row.get("supplier_gstin") or "—",
                    row.get("party") or "—",
                    f"{row['doc_type']} {row['doc_no']}",
                    row["doc_date"],
                    text_item(row.get("books_taxable") or "—", align=Qt.AlignmentFlag.AlignRight),
                    text_item(row.get("portal_taxable") or "—", align=Qt.AlignmentFlag.AlignRight),
                    text_item(row.get("books_tax") or "—", align=Qt.AlignmentFlag.AlignRight),
                    text_item(row.get("portal_tax") or "—", align=Qt.AlignmentFlag.AlignRight),
                    row.get("reason") or "—",
                ]
                for row in data.get("rows", [])
            ],
        )
        summary = (
            f"Matched {counts['matched']} · Mismatched {counts['mismatched']} · "
            f"Missing in books {counts['missing_books']} · "
            f"Missing in portal {counts['missing_portal']} — "
            f"ITC: books ₹{itc['books']} / portal ₹{itc['portal']} / "
            f"difference ₹{itc['difference']}"
        )
        if itc.get("portal_ineligible_rows"):
            summary += (
                f" — {itc['portal_ineligible_rows']} portal row(s) marked ITC-ineligible "
                f"(₹{itc['portal_ineligible_tax']} of tax): reverse the claim before "
                "the 3B's Table 4."
            )
        self.summary_label.setText(summary)

    # ---------------------------------------------------------------- import
    def _import_dialog(self) -> None:
        dialog = ImportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        text, source = dialog.values()

        def preview() -> dict:
            return self._client.gst_import(text, source=source or None, dry_run=True)

        def previewed(receipt: dict) -> None:
            answer = QMessageBox.question(
                self,
                "Import the download?",
                f"Preview: {receipt['new']} new line(s), {receipt['duplicates']} "
                "duplicate(s). Import for real?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

            def act() -> dict:
                return self._client.gst_import(text, source=source or None)

            def done(final: dict) -> None:
                self.reload()
                QMessageBox.information(
                    self,
                    "Download imported",
                    f"Imported {final['new']} new line(s), {final['duplicates']} "
                    f"duplicate(s) — {final['kind'].upper()} {final['period']} for "
                    f"{final['gstin']} (revision {final['revision']}).",
                )

            run(act, done, self._failed)

        run(preview, previewed, self._failed)

    # ------------------------------------------------------- the statutory PDFs
    def _save_pdf(self, key: str, year: bool = False) -> None:
        raw = self._period()
        if year:  # GSTR-9: the period's year, or the current one
            period = raw[:4]
            if len(period) != 4 or not period.isdigit():
                period = str(date.today().year)
        else:
            period = raw
            if len(period) != 7 or period[4] != "-" or not period.replace("-", "").isdigit():
                error_box(
                    self,
                    "GST",
                    f"{_PDF_TITLES[key]} needs a period like 2026-10 (YYYY-MM).",
                )
                return
        params: dict = {"period": period}
        gstin = self._registration()
        if gstin:
            params["gstin"] = gstin
        suggested = f"{_PDF_TITLES[key]} {period}"
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save PDF", suggested + ".pdf", "PDF (*.pdf)"
        )
        if not path:
            return

        def fetch() -> tuple[bytes, str, str]:
            return self._client.pack(key, **params)

        def done(result: tuple[bytes, str, str]) -> None:
            content, _media, _name = result
            with open(path, "wb") as handle:
                handle.write(content)
            QMessageBox.information(self, "GST", f"Saved {path}")

        run(fetch, done, self._failed)


# ------------------------------------------------------------------ dialogs
class ImportDialog(QDialog):
    """Paste the portal download, then import — the preview is the page's
    dry run and the receipt is the honest answer in both cases."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Import GST portal download")
        layout = QVBoxLayout(self)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText(
            "Paste the portal download (the operator adapter's lf-gst-2a/1 JSON "
            "— the file the GST portal's download translates to)"
        )
        layout.addWidget(self.text_edit)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("a label for the receipt (file name…)")
        self.source_edit.setMaxLength(200)
        layout.addWidget(self.source_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.import_button = QPushButton("Import")
        self.import_button.setDefault(True)
        self.import_button.clicked.connect(self.accept)
        buttons.addButton(self.import_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str]:
        return self.text_edit.toPlainText(), self.source_edit.text().strip()
