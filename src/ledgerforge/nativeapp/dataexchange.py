"""The Data Exchange page — the migration workbench (ADR-030, roadmap
Phase 9): import CSV/Excel files (masters, opening balances, vouchers,
stock masters), walk the TallyPrime XML migration path with its
verification report, and export any report or list surface to CSV/XLSX.

Zero business logic in the GUI: every number is the engine's string
(Law 3), every action one API call, the payroll page's discipline. The
preview is the engine's own dry run — the page shows what the engine
says would land, never its own arithmetic.
"""

from __future__ import annotations

import base64
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .client import ApiClient
from .tasks import run
from .widgets import error_detail, fill_table, make_table

#: The import kinds with a file picker (bank statements keep their own
#: home on the Banking page — the reconciliation's line book).
KINDS = [
    ("masters", "Masters (groups & ledgers)"),
    ("openings", "Opening balances"),
    ("vouchers", "Vouchers"),
    ("stock", "Stock item masters"),
]

#: The export formats (ADR-030's seam).
FORMATS = ("csv", "xlsx")


def _read_for_api(path: str, fmt: str) -> str:
    """CSV travels as text; xlsx as base64 of its bytes (ADR-030's
    ``decode_tabular`` contract)."""
    if fmt == "xlsx":
        return base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return Path(path).read_text(encoding="utf-8-sig")


def _fmt_of(path: str) -> str:
    return "xlsx" if path.lower().endswith((".xlsx", ".xlsm")) else "csv"


def _receipt_text(receipt: dict) -> str:
    """The receipt, one honest paragraph per section."""
    parts: list[str] = []
    if receipt.get("kind") == "tally":
        masters = receipt.get("masters") or {}
        vouchers = receipt.get("vouchers") or {}
        openings = receipt.get("openings") or {}
        parts.append(
            "Masters: {g} groups + {l} ledgers + {s} stock items created "
            "({e} already existed)".format(
                g=masters.get("groups_created", 0),
                l=masters.get("ledgers_created", 0),
                s=masters.get("stock_items_created", 0),
                e=masters.get("existing", 0),
            )
        )
        if openings:
            parts.append(
                "Opening journal {no}: Dr {dr} = Cr {cr}{bal}".format(
                    no=openings.get("voucher_no", ""),
                    dr=openings.get("total_debit", "?"),
                    cr=openings.get("total_credit", "?"),
                    bal=(
                        f" (difference absorbed to {openings['balanced_to']})"
                        if openings.get("balanced_to")
                        else ""
                    ),
                )
            )
        skipped = vouchers.get("skipped") or []
        parts.append(
            "Vouchers: {m} mapped ({p} posted, {r} replayed), {s} skipped".format(
                m=vouchers.get("mapped", 0),
                p=vouchers.get("posted", 0),
                r=vouchers.get("replayed", 0),
                s=len(skipped),
            )
        )
        for skip in skipped[:5]:
            parts.append(
                "  skipped: {t} of {d} ({why})".format(
                    t=skip.get("vchtype"), d=skip.get("date"), why=skip.get("reason", "")
                )
            )
        verification = receipt.get("verification") or {}
        parts.append(
            "Verification: {d} ledger difference(s); source total {s}, imported total {i}".format(
                d=verification.get("differences", "?"),
                s=verification.get("source_total", "?"),
                i=verification.get("imported_total", "?"),
            )
        )
        auto = receipt.get("auto_ledgers") or []
        if auto:
            parts.append(f"  ledgers auto-created under Suspense A/c: {', '.join(auto)}")
        for opening in (receipt.get("stock_openings") or [])[:5]:
            parts.append(
                "  stock opening: {item} × {qty} — {note}".format(
                    item=opening.get("item"), qty=opening.get("qty"), note=opening.get("note")
                )
            )
        return "\n".join(parts)
    # the CSV kinds' receipts
    bits = []
    for key, label in (
        ("groups_created", "groups"),
        ("ledgers_created", "ledgers"),
        ("stock_items_created", "stock items"),
        ("created", "created"),
        ("existing", "already existed"),
        ("posted", "posted"),
        ("replayed", "replayed"),
        ("new", "new lines"),
        ("unchanged", "unchanged"),
        ("duplicate", "duplicates"),
    ):
        if key in receipt:
            bits.append(f"{receipt[key]} {label}")
    if "voucher_no" in receipt:
        bits.append(f"opening journal {receipt['voucher_no']}")
    if "total_debit" in receipt:
        bits.append(f"Dr {receipt['total_debit']} = Cr {receipt['total_credit']}")
    return "; ".join(bits) or str(receipt)


class DataExchangePage(QWidget):
    """The migration workbench: import, Tally, export."""

    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client

        # ---- the import pane ----------------------------------------------
        import_bar = QHBoxLayout()
        import_bar.addWidget(QLabel("Import"))
        self.kind_combo = QComboBox()
        self.kind_combo.setAccessibleName("Data kind")
        for key, label in KINDS:
            self.kind_combo.addItem(label, key)
        import_bar.addWidget(self.kind_combo)
        self.import_button = QPushButton("Pick File & Preview…")
        self.import_button.clicked.connect(self._import_dialog)
        import_bar.addWidget(self.import_button)
        import_bar.addStretch(1)
        self.import_status = QLabel("")

        # ---- the Tally pane ------------------------------------------------
        tally_bar = QHBoxLayout()
        tally_bar.addWidget(QLabel("TallyPrime"))
        self.tally_button = QPushButton("Pick Export XML & Preview…")
        self.tally_button.clicked.connect(self._tally_dialog)
        tally_bar.addWidget(self.tally_button)
        tally_bar.addStretch(1)

        self.verification = make_table(["Ledger", "Source (Dr+)", "Imported (Dr+)", "Difference"])
        self.verification.setColumnWidth(0, 260)

        # ---- the export pane ------------------------------------------------
        export_bar = QHBoxLayout()
        export_bar.addWidget(QLabel("Export"))
        self.surface_combo = QComboBox()
        self.surface_combo.setAccessibleName("Export surface")
        self.surface_combo.currentIndexChanged.connect(self._surface_changed)
        export_bar.addWidget(self.surface_combo)
        self.params_edit = QLabel("")
        export_bar.addWidget(self.params_edit)
        self.format_combo = QComboBox()
        self.format_combo.setAccessibleName("Export format")
        for fmt in FORMATS:
            self.format_combo.addItem(fmt.upper(), fmt)
        export_bar.addWidget(self.format_combo)
        self.export_button = QPushButton("Export…")
        self.export_button.clicked.connect(self._export_dialog)
        export_bar.addWidget(self.export_button)
        export_bar.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(import_bar)
        layout.addWidget(self.import_status)
        layout.addLayout(tally_bar)
        layout.addWidget(
            QLabel(
                "Verification — the source trial balance (folded from the XML) against "
                "the imported one: differences name what did not come over"
            )
        )
        layout.addWidget(self.verification)
        layout.addLayout(export_bar)
        layout.addStretch(1)

    # ------------------------------------------------------------- the loads
    def reload(self) -> None:
        run(self._client.exports_catalog, self._catalog_loaded, self._catalog_failed)

    def _catalog_loaded(self, catalog: dict) -> None:
        self.surface_combo.blockSignals(True)
        self.surface_combo.clear()
        for surface in catalog.get("surfaces") or []:
            self.surface_combo.addItem(surface["label"], surface)
        self.surface_combo.blockSignals(False)
        self._surface_changed()

    def _catalog_failed(self, error: object) -> None:
        self.import_status.setText(f"exports unavailable — {error_detail(error)}")

    def _surface_changed(self, *_args) -> None:
        surface = self.surface_combo.currentData()
        if not surface:
            self.params_edit.setText("")
            return
        params = surface.get("params") or []
        self.params_edit.setText(f"parameters: {', '.join(params)}" if params else "no parameters")

    # ------------------------------------------------------------- importing
    def _import_dialog(self) -> None:
        kind = self.kind_combo.currentData()
        path, _filter = QFileDialog.getOpenFileName(
            self, "Pick the import file", "", "Spreadsheets (*.csv *.xlsx *.xlsm);;All files (*)"
        )
        if not path:
            return
        fmt = _fmt_of(path)
        try:
            content = _read_for_api(path, fmt)
        except OSError as exc:
            QMessageBox.critical(self, "Import", f"Could not read the file:\n{exc}")
            return
        source = Path(path).name
        self.import_status.setText("previewing…")

        def preview():
            return self._client.import_file(kind, fmt, content, source=source, dry_run=True)

        run(
            preview,
            lambda r: self._import_previewed(kind, fmt, content, source, r),
            self._import_failed,
        )

    def _import_previewed(
        self, kind: str, fmt: str, content: str, source: str, receipt: dict
    ) -> None:
        text = _receipt_text(receipt)
        confirm = QMessageBox.question(
            self,
            "Import preview",
            f"The engine's preview (nothing written yet):\n\n{text}\n\nImport this file?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            self.import_status.setText("preview discarded — nothing written")
            return

        def commit():
            return self._client.import_file(kind, fmt, content, source=source, dry_run=False)

        run(commit, self._import_done, self._import_failed)

    def _import_done(self, receipt: dict) -> None:
        self.import_status.setText(f"imported — {_receipt_text(receipt)}")

    def _import_failed(self, error: object) -> None:
        self.import_status.setText(f"import failed — {error_detail(error)}")

    # ----------------------------------------------------------------- tally
    def _tally_dialog(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Pick the TallyPrime export", "", "XML (*.xml);;All files (*)"
        )
        if not path:
            return
        try:
            xml = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            QMessageBox.critical(self, "Tally import", f"Could not read the file:\n{exc}")
            return
        source = Path(path).name
        self.import_status.setText(f"previewing {source}…")

        def preview():
            return self._client.tally_import(xml, source=source, dry_run=True)

        run(preview, lambda r: self._tally_previewed(xml, source, r), self._import_failed)

    def _tally_previewed(self, xml: str, source: str, receipt: dict) -> None:
        self._show_verification(receipt)
        text = _receipt_text(receipt)
        confirm = QMessageBox.question(
            self,
            "Tally migration preview",
            f"The engine's preview (nothing written yet):\n\n{text}\n\nImport this export?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            self.import_status.setText("preview discarded — nothing written")
            return

        def commit():
            return self._client.tally_import(xml, source=source, dry_run=False)

        run(commit, self._tally_done, self._import_failed)

    def _tally_done(self, receipt: dict) -> None:
        self._show_verification(receipt)
        self.import_status.setText(f"imported — {_receipt_text(receipt)}")

    def _show_verification(self, receipt: dict) -> None:
        verification = receipt.get("verification") or {}
        rows = []
        for row in verification.get("rows") or []:
            rows.append(
                [
                    str(row.get("ledger", "")),
                    str(row.get("source", "")),
                    str(row.get("imported", "")),
                    str(row.get("difference", "")),
                ]
            )
        fill_table(self.verification, rows)

    # ---------------------------------------------------------------- exports
    def _export_dialog(self) -> None:
        surface = self.surface_combo.currentData()
        if not surface:
            return
        fmt = self.format_combo.currentData()
        params = surface.get("params") or []
        query: dict[str, str] = {}
        for param in params:
            value, ok = self._ask_param(param)
            if not ok:
                return
            if value:
                query[param] = value
        default_name = f"{surface.get('key', 'export')}.{fmt}"
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Save the export",
            default_name,
            f"{'Excel workbook' if fmt == 'xlsx' else 'CSV'} (*.{fmt});;All files (*)",
        )
        if not path:
            return

        def fetch():
            return self._client.export_bytes(surface["path"], query, fmt)

        def save(result: tuple[bytes, str]) -> None:
            content, _name = result
            Path(path).write_bytes(content)
            self.import_status.setText(f"exported — {path}")

        run(fetch, save, self._import_failed)

    def _ask_param(self, param: str) -> tuple[str, bool]:
        from PySide6.QtWidgets import QInputDialog

        value, ok = QInputDialog.getText(
            self,
            "Export parameter",
            f"Value for “{param}” (leave empty to use the surface's default):",
        )
        return value, ok
