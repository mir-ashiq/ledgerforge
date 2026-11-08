"""The Inventory page — stock masters, the depth positions, and the
re-order/expiry reports (ADR-027, Phase 6).

Three tabs over one kernel: **Items** (the masters with their tracking
modes and re-order levels, their price lists a dialog away), **Positions**
(the pack's stock-position fold rendered as a table — every item with its
godown and batch split, serial counts, the average cost), and **BOMs**
(the recipes the manufacturing journal validates against). The reports
(expiry ageing, re-order shortfall, godown-wise, batch-wise) open as
read-only dialogs over the same JSON folds the web UI reads — one truth,
every surface. Zero business logic in the GUI: every number is the
engine's string (Law 3), every action one API call.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .client import ApiClient
from .tasks import run
from .widgets import error_box, error_detail, fill_table, make_table, text_item


def _plain(value: str | None) -> str:
    """A numeric string without its trailing zeros (the DB's NUMERIC scale
    is storage, not display)."""
    if value is None:
        return "—"
    try:
        from decimal import Decimal

        return f"{Decimal(value).normalize():f}"
    except ArithmeticError:
        return str(value)


class ItemDialog(QDialog):
    """Create or revise a stock-item master: unit, tracking modes, the
    re-order levels the shortfall report folds against."""

    def __init__(self, parent: QWidget | None = None, *, item: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Stock item" if item else "New stock item")
        form = QFormLayout(self)

        self.name = QLineEdit(item["name"] if item else "")
        form.addRow("Name", self.name)
        self.unit = QLineEdit((item or {}).get("unit") or "")
        form.addRow("Unit (of measure)", self.unit)
        self.track_batches = QCheckBox(
            "Maintain in batches (goods-in names its batch; issues FEFO)"
        )
        self.track_batches.setChecked(bool(item and item.get("track_batches")))
        form.addRow("", self.track_batches)
        self.track_serials = QCheckBox("Maintain serial numbers (serials are identity, never auto)")
        self.track_serials.setChecked(bool(item and item.get("track_serials")))
        form.addRow("", self.track_serials)

        self.reorder_level = QDoubleSpinBox()
        self.reorder_level.setRange(0.0, 1_000_000_000.0)
        self.reorder_level.setDecimals(2)
        self.reorder_level.setSpecialValueText("not set")
        if item and item.get("reorder_level") is not None:
            self.reorder_level.setValue(float(item["reorder_level"]))
        form.addRow("Re-order level", self.reorder_level)
        self.reorder_quantity = QDoubleSpinBox()
        self.reorder_quantity.setRange(0.0, 1_000_000_000.0)
        self.reorder_quantity.setDecimals(2)
        self.reorder_quantity.setSpecialValueText("not set")
        if item and item.get("reorder_quantity") is not None:
            self.reorder_quantity.setValue(float(item["reorder_quantity"]))
        form.addRow("Re-order quantity", self.reorder_quantity)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def payload(self) -> dict:
        body: dict = {
            "name": self.name.text().strip(),
            "unit": self.unit.text().strip() or None,
            "track_batches": self.track_batches.isChecked(),
            "track_serials": self.track_serials.isChecked(),
        }
        if self.reorder_level.value() > 0:
            body["reorder_level"] = f"{self.reorder_level.value():.2f}"
        if self.reorder_quantity.value() > 0:
            body["reorder_quantity"] = f"{self.reorder_quantity.value():.2f}"
        return body


class PriceDialog(QDialog):
    """One date-effective price row: the party is optional (blank = the
    list price for everyone)."""

    def __init__(self, parent: QWidget | None, item: dict, parties: list[dict]):
        super().__init__(parent)
        self.setWindowTitle(f"Price — {item['name']}")
        form = QFormLayout(self)

        self.party = QComboBox()
        self.party.addItem("(list price — everyone)", None)
        for party in parties:
            self.party.addItem(party["name"], party["id"])
        form.addRow("Party", self.party)

        self.effective_from = QLineEdit()
        from datetime import date

        self.effective_from.setText(date.today().isoformat())
        form.addRow("Effective from (YYYY-MM-DD)", self.effective_from)

        self.rate = QDoubleSpinBox()
        self.rate.setRange(0.01, 1_000_000_000.0)
        self.rate.setDecimals(2)
        form.addRow("Rate", self.rate)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def payload(self) -> dict:
        from datetime import date

        try:
            effective = date.fromisoformat(self.effective_from.text().strip())
        except ValueError:
            effective = date.today()
        return {
            "effective_from": effective.isoformat(),
            "party_ledger_id": self.party.currentData(),
            "rate": f"{self.rate.value():.2f}",
        }


class BomDialog(QDialog):
    """A recipe: the output item and per-run quantity, the components with
    their per-output quantities (one line per component, `item × qty`)."""

    def __init__(self, parent: QWidget | None, items: list[dict]):
        super().__init__(parent)
        self.setWindowTitle("New BOM")
        self._items = items
        form = QFormLayout(self)

        self.name = QLineEdit()
        form.addRow("Name", self.name)

        self.output = QComboBox()
        for item in items:
            self.output.addItem(item["name"], item["id"])
        form.addRow("Output item", self.output)

        self.output_qty = QDoubleSpinBox()
        self.output_qty.setRange(0.01, 1_000_000.0)
        self.output_qty.setDecimals(2)
        self.output_qty.setValue(1.0)
        form.addRow("Output qty (per run)", self.output_qty)

        self.components = QLineEdit()
        self.components.setPlaceholderText("Plank × 6; Screw × 20")
        self.components.setToolTip("Components as 'item × qty per output unit', ';'-separated")
        form.addRow("Components", self.components)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def payload(self) -> dict:
        by_name = {item["name"]: item["id"] for item in self._items}
        components = []
        for chunk in self.components.text().split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            name, _, qty = chunk.rpartition("×")
            components.append(
                {
                    "component_item_id": by_name[name.strip()],
                    "qty_per_output": qty.strip(),
                }
            )
        return {
            "name": self.name.text().strip(),
            "output_item_id": self.output.currentData(),
            "output_qty": f"{self.output_qty.value():.2f}",
            "components": components,
        }


class ReportDialog(QDialog):
    """A pack report's JSON fold rendered read-only — one truth, every
    surface (the PDF prints the same numbers)."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        columns: list[str],
        rows: list[list[str]],
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 420)
        layout = QVBoxLayout(self)
        table = make_table(columns)
        fill_table(table, rows)
        layout.addWidget(table)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class InventoryPage(QWidget):
    """Masters, positions, recipes — the stock depth's home (Alt+7)."""

    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self._items: list[dict] = []
        self._boms: list[dict] = []
        self._positions: list[dict] = []

        self.tabs = QTabWidget()

        # ---- Items -----------------------------------------------------
        items_page = QWidget()
        items_bar = QHBoxLayout()
        self.new_item_button = QPushButton("New Item…")
        self.new_item_button.clicked.connect(self._new_item)
        items_bar.addWidget(self.new_item_button)
        self.edit_item_button = QPushButton("Edit…")
        self.edit_item_button.clicked.connect(self._edit_item)
        items_bar.addWidget(self.edit_item_button)
        self.prices_button = QPushButton("Prices…")
        self.prices_button.clicked.connect(self._prices_dialog)
        items_bar.addWidget(self.prices_button)
        self.delete_item_button = QPushButton("Delete")
        self.delete_item_button.clicked.connect(self._delete_item)
        items_bar.addWidget(self.delete_item_button)
        items_bar.addStretch(1)
        items_layout = QVBoxLayout(items_page)
        items_layout.addLayout(items_bar)
        self.items_table = make_table(
            ["Item", "Unit", "Batches", "Serials", "Re-order level", "Re-order qty", "Prices"]
        )
        self.items_table.itemSelectionChanged.connect(self._item_selected)
        items_layout.addWidget(self.items_table)

        # ---- Positions -------------------------------------------------
        positions_page = QWidget()
        positions_bar = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.reload)
        positions_bar.addWidget(self.refresh_button)
        self.expiry_button = QPushButton("Expiry Ageing…")
        self.expiry_button.clicked.connect(
            lambda: self._report_dialog(
                "stock-expiry",
                "Expiry ageing",
                ["Item", "Batch", "Expiry", "Days", "Bucket", "Qty", "Value"],
                lambda row: [
                    row["item"],
                    row["batch"],
                    row["expiry"],
                    row["days_left"],
                    row["bucket"],
                    _plain(row["qty"]),
                    row["value"],
                ],
            )
        )
        positions_bar.addWidget(self.expiry_button)
        self.shortfall_button = QPushButton("Re-order Shortfall…")
        self.shortfall_button.clicked.connect(
            lambda: self._report_dialog(
                "stock-shortfall",
                "Re-order shortfall",
                ["Item", "Unit", "On hand", "Level", "Below by", "Order qty"],
                lambda row: [
                    row["item"],
                    row["unit"] or "—",
                    row["on_hand"],
                    row["reorder_level"],
                    row["below_by"],
                    row["suggested_order"],
                ],
            )
        )
        positions_bar.addWidget(self.shortfall_button)
        self.godown_button = QPushButton("Godown-wise…")
        self.godown_button.clicked.connect(
            lambda: self._report_dialog(
                "stock-godown",
                "Godown-wise stock",
                ["Godown", "Item", "Qty", "Value", "Avg cost"],
                lambda row: [
                    row["godown"],
                    row["item"],
                    _plain(row["qty"]),
                    row["value"],
                    row["avg_cost"] or "—",
                ],
            )
        )
        positions_bar.addWidget(self.godown_button)
        self.batch_button = QPushButton("Batch-wise…")
        self.batch_button.clicked.connect(
            lambda: self._report_dialog(
                "stock-batch",
                "Batch-wise stock",
                ["Item", "Batch", "Expiry", "Qty", "Value", "Godowns"],
                lambda row: [
                    row["item"],
                    row["batch"],
                    row["expiry"] or "—",
                    _plain(row["qty"]),
                    row["value"],
                    ", ".join(f"{g}: {_plain(q)}" for g, q in row["godowns"].items()),
                ],
            )
        )
        positions_bar.addWidget(self.batch_button)
        positions_bar.addStretch(1)
        positions_layout = QVBoxLayout(positions_page)
        positions_layout.addLayout(positions_bar)
        self.positions_table = make_table(
            ["Item", "Qty", "Value", "Avg cost", "Godowns", "Batches", "Serials"]
        )
        positions_layout.addWidget(self.positions_table)

        # ---- BOMs ------------------------------------------------------
        boms_page = QWidget()
        boms_bar = QHBoxLayout()
        self.new_bom_button = QPushButton("New BOM…")
        self.new_bom_button.clicked.connect(self._new_bom)
        boms_bar.addWidget(self.new_bom_button)
        self.delete_bom_button = QPushButton("Delete")
        self.delete_bom_button.clicked.connect(self._delete_bom)
        boms_bar.addWidget(self.delete_bom_button)
        boms_bar.addStretch(1)
        boms_layout = QVBoxLayout(boms_page)
        boms_layout.addLayout(boms_bar)
        self.boms_table = make_table(["BOM", "Output", "Per run", "Components"])
        self.boms_table.itemSelectionChanged.connect(self._bom_selected)
        boms_layout.addWidget(self.boms_table)

        self.tabs.addTab(items_page, "Items")
        self.tabs.addTab(positions_page, "Positions")
        self.tabs.addTab(boms_page, "BOMs")

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)

        self._item_selected()
        self._bom_selected()

    # ------------------------------------------------------------ loading
    def reload(self) -> None:
        run(self._client.inventory_items, self._items_loaded, self._failed)
        run(self._client.inventory_boms, self._boms_loaded, self._failed)
        run(
            lambda: self._client.pack_data("stock-position"),
            self._positions_loaded,
            self._failed,
        )

    def _items_loaded(self, items: list[dict]) -> None:
        self._items = items
        fill_table(
            self.items_table,
            [
                [
                    text_item(i["name"]),
                    i["unit"] or "—",
                    "✓" if i["track_batches"] else "—",
                    "✓" if i["track_serials"] else "—",
                    _plain(i["reorder_level"]),
                    _plain(i["reorder_quantity"]),
                    str(i["price_count"]),
                ]
                for i in items
            ],
        )
        self._item_selected()

    def _boms_loaded(self, boms: list[dict]) -> None:
        self._boms = boms
        fill_table(
            self.boms_table,
            [
                [
                    text_item(b["name"]),
                    b["output"],
                    _plain(b["output_qty"]),
                    "; ".join(
                        f"{c['component']} × {_plain(c['qty_per_output'])}" for c in b["components"]
                    ),
                ]
                for b in boms
            ],
        )
        self._bom_selected()

    def _positions_loaded(self, payload: dict) -> None:
        self._positions = payload.get("items", [])
        fill_table(
            self.positions_table,
            [
                [
                    text_item(p["item"]),
                    text_item(_plain(p["qty"]), align=Qt.AlignmentFlag.AlignRight),
                    text_item(p["value"], align=Qt.AlignmentFlag.AlignRight),
                    text_item(p["avg_cost"] or "—", align=Qt.AlignmentFlag.AlignRight),
                    ", ".join(f"{g}: {_plain(q)}" for g, q in p["godowns"].items()) or "—",
                    ", ".join(f"{b['batch']}: {_plain(b['qty'])}" for b in p["batches"]) or "—",
                    str(p["serial_count"]) if p["serial_count"] else "—",
                ]
                for p in self._positions
            ],
        )

    def _failed(self, error: Exception) -> None:
        error_box(self, "Inventory", error_detail(error))

    # -------------------------------------------------------------- items
    def _selected_item(self) -> dict | None:
        row = self.items_table.currentRow()
        return self._items[row] if 0 <= row < len(self._items) else None

    def _item_selected(self) -> None:
        selected = self._selected_item() is not None
        self.edit_item_button.setEnabled(selected)
        self.prices_button.setEnabled(selected)
        self.delete_item_button.setEnabled(selected)

    def _new_item(self) -> None:
        dialog = ItemDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.payload()
        run(
            lambda: self._client.inventory_create_item(payload),
            lambda _row: self.reload(),
            self._failed,
        )

    def _edit_item(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        dialog = ItemDialog(self, item=item)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.payload()
        item_id = item["id"]
        run(
            lambda: self._client.inventory_update_item(item_id, payload),
            lambda _row: self.reload(),
            self._failed,
        )

    def _delete_item(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        confirm = QMessageBox.question(
            self,
            "Delete stock item",
            f"Delete {item['name']!r}? Price rows cascade with it; BOM "
            "references refuse (the database's own verdict).",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        item_id = item["id"]
        run(
            lambda: self._client.inventory_delete_item(item_id),
            lambda _row: self.reload(),
            self._failed,
        )

    # ------------------------------------------------------------- prices
    def _prices_dialog(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        item_id = item["id"]
        run(
            lambda: self._client.inventory_prices(item_id),
            lambda rows: self._show_prices(item, rows),
            self._failed,
        )

    def _show_prices(self, item: dict, rows: list[dict]) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Prices — {item['name']}")
        dialog.resize(560, 340)
        layout = QVBoxLayout(dialog)
        table = make_table(["Party", "Effective from", "Rate"])
        fill_table(
            table,
            [[r["party"] or "(list price)", r["effective_from"], r["rate"]] for r in rows],
        )
        layout.addWidget(table)
        bar = QHBoxLayout()
        add = QPushButton("Add Price…")
        add.clicked.connect(lambda: self._add_price(dialog, item))
        bar.addWidget(add)
        remove = QPushButton("Delete Selected")
        remove.clicked.connect(lambda: self._delete_price(dialog, table, rows))
        bar.addWidget(remove)
        bar.addStretch(1)
        layout.addLayout(bar)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def _add_price(self, parent: QWidget, item: dict) -> None:
        run(
            lambda: self._client.ledgers(""),
            lambda parties: self._price_dialog(parent, item, parties),
            self._failed,
        )

    def _price_dialog(self, parent: QWidget, item: dict, parties: list) -> None:
        parties = [
            {"name": led.name, "id": led.id}
            for led in parties
            # prices name parties: customers and suppliers carry the rows
        ]
        dialog = PriceDialog(parent, item, parties)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = {"item_id": item["id"], **dialog.payload()}
        run(
            lambda: self._client.inventory_create_price(payload),
            lambda _row: None,
            self._failed,
        )

    def _delete_price(self, parent: QWidget, table: QTableWidget, rows: list[dict]) -> None:
        row = table.currentRow()
        if not 0 <= row < len(rows):
            return
        price_id = rows[row]["id"]
        run(
            lambda: self._client.inventory_delete_price(price_id),
            lambda _row: None,
            self._failed,
        )

    # --------------------------------------------------------------- BOMs
    def _selected_bom(self) -> dict | None:
        row = self.boms_table.currentRow()
        return self._boms[row] if 0 <= row < len(self._boms) else None

    def _bom_selected(self) -> None:
        self.delete_bom_button.setEnabled(self._selected_bom() is not None)

    def _new_bom(self) -> None:
        if not self._items:
            self._failed(RuntimeError("create stock items first — a BOM names them"))
            return
        dialog = BomDialog(self, self._items)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.payload()
        run(
            lambda: self._client.inventory_create_bom(payload),
            lambda _row: self.reload(),
            self._failed,
        )

    def _delete_bom(self) -> None:
        bom = self._selected_bom()
        if bom is None:
            return
        bom_id = bom["id"]
        run(
            lambda: self._client.inventory_delete_bom(bom_id),
            lambda _row: self.reload(),
            self._failed,
        )

    # ------------------------------------------------------------ reports
    def _report_dialog(self, key: str, title: str, columns: list[str], map_row) -> None:
        def opened(payload: dict) -> None:
            rows = [map_row(row) for row in payload.get("rows", [])]
            ReportDialog(self, title, columns, rows).exec()

        run(lambda: self._client.pack_data(key), opened, self._failed)
