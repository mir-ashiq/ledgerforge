"""The Orders page — the five order documents, one native browser.

Every QT/SO/DN/PO/GRN voucher of the book (their own series, ADR-024),
with kind, party, the document's own ref, the engine-computed value and
the live position status from the ``pending-orders`` fold. Filters by
kind and party search; *New Order…* posts any of the five kinds;
double-click opens the voucher view — whose *Save as PDF* prints the
commercial document through the orders pack's print view (ADR-013).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .client import ApiClient
from .models import Voucher, money
from .order_dialog import KIND_CHOICES
from .tasks import run
from .widgets import error_box, error_detail, fill_table, make_table

#: (series code, label) for the filter — the pack's five types.
_TYPE_FILTERS: list[tuple[str, str]] = [
    ("", "All order documents"),
    ("QT", "QT — Quotations"),
    ("SO", "SO — Sales Orders"),
    ("DN", "DN — Delivery Notes"),
    ("PO", "PO — Purchase Orders"),
    ("GRN", "GRN — Goods Receipt Notes"),
]

_KIND_LABEL = dict(KIND_CHOICES)


class OrdersPage(QWidget):
    open_voucher_requested = Signal(int)  # voucher id
    new_order_requested = Signal()
    pending_report_requested = Signal(str)  # the Reports page's report key

    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self._vouchers: list[Voucher] = []
        self._status: dict[str, str] = {}  # voucher_no -> status label

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Kind"))
        self.type_filter = QComboBox()
        for code, label in _TYPE_FILTERS:
            self.type_filter.addItem(label, code)
        bar.addWidget(self.type_filter)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search party or ref…")
        self.search_box.setClearButtonEnabled(True)
        bar.addWidget(self.search_box, stretch=1)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.reload)
        bar.addWidget(self.refresh_button)
        self.new_button = QPushButton("New Order…")
        self.new_button.clicked.connect(self.new_order_requested.emit)
        bar.addWidget(self.new_button)
        self.pending_button = QPushButton("Pending Orders…")
        self.pending_button.setToolTip(
            "The pending-orders pack report on this side — opens the Reports "
            "page ready to run (the PO/GRN filter switches to the purchase side)"
        )
        self.pending_button.clicked.connect(self._open_pending_report)
        bar.addWidget(self.pending_button)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)

        self.table = make_table(["Voucher No", "Date", "Kind", "Party", "Ref", "Value", "Status"])
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(3, 220)
        self.table.setColumnWidth(4, 120)
        self.table.setColumnWidth(5, 130)
        self.table.setColumnWidth(6, 100)
        self.table.itemDoubleClicked.connect(self._open_selected)
        self.search_box.textChanged.connect(lambda _t: self._render())
        layout.addWidget(self.table, stretch=1)

    # ------------------------------------------------------------- loading
    def reload(self) -> None:
        def load():
            rows: list[Voucher] = []
            for code in ("QT", "SO", "DN", "PO", "GRN"):
                rows.extend(self._client.vouchers(type_code=code, limit=200))
            rows.sort(key=lambda v: (v.voucher_date, v.id), reverse=True)
            return rows

        run(load, self._loaded, self._failed)
        run(
            lambda: self._client.pack_json("pending-orders", side="sales"),
            self._status_loaded_sales,
            lambda _e: None,
        )
        run(
            lambda: self._client.pack_json("pending-orders", side="purchase"),
            self._status_loaded_purchase,
            lambda _e: None,
        )

    def _status_loaded_sales(self, data: dict) -> None:
        self._absorb_status(data)

    def _status_loaded_purchase(self, data: dict) -> None:
        self._absorb_status(data)

    def _absorb_status(self, data: dict) -> None:
        for row in data.get("rows", []):
            key = row.get("voucher_no", "")
            label = row.get("status", "")
            current = self._status.get(key)
            if current in (None, "", "open") or label == "closed":
                self._status[key] = label
        self._render()

    def _failed(self, error: object) -> None:
        error_box(self, "Orders", f"Could not load the order documents:\n{error_detail(error)}")

    def _loaded(self, vouchers: list[Voucher]) -> None:
        self._vouchers = vouchers
        self._render()

    # ------------------------------------------------------------- rendering
    def _kind_of(self, voucher: Voucher) -> str:
        doc = (voucher.meta or {}).get("orders") or {}
        if isinstance(doc, dict) and doc.get("kind"):
            return _KIND_LABEL.get(doc["kind"], voucher.voucher_type)
        return voucher.voucher_type

    def _status_of(self, voucher: Voucher) -> str:
        doc = (voucher.meta or {}).get("orders") or {}
        if not isinstance(doc, dict):
            return ""
        if doc.get("kind") in ("sales_order", "purchase_order"):
            return self._status.get(voucher.voucher_no, "open")
        if doc.get("kind") == "quotation":
            return "quote"
        if doc.get("settle"):
            return "settles order"
        return ""

    def _render(self) -> None:
        needle = (self.search_box.text() or "").casefold()
        code_filter = self.type_filter.currentData() or ""
        rows = []
        for v in self._vouchers:
            doc = (v.meta or {}).get("orders") or {}
            party = str(doc.get("party", "")) if isinstance(doc, dict) else ""
            ref = str(doc.get("ref", "")) if isinstance(doc, dict) else ""
            if code_filter and v.voucher_type != code_filter:
                continue
            hay = f"{party} {ref} {v.voucher_no}".casefold()
            if needle and needle not in hay:
                continue
            rows.append(
                [
                    v.voucher_no,
                    v.voucher_date.strftime("%d-%m-%Y"),
                    self._kind_of(v),
                    party,
                    ref,
                    money(v.amount),
                    self._status_of(v),
                ]
            )
        fill_table(self.table, rows)
        self.table.sortItems(1, Qt.SortOrder.DescendingOrder)

    # -------------------------------------------------------------- opening
    def _open_selected(self, *_: object) -> None:
        selected = self.table.selectedItems()
        if not selected:
            return
        row = self.table.row(selected[0])
        voucher_no = self.table.item(row, 0).text()
        for v in self._vouchers:
            if v.voucher_no == voucher_no:
                self.open_voucher_requested.emit(v.id)
                return

    def refresh(self) -> None:
        """The window's F5 lands here."""
        self.reload()

    def _open_pending_report(self) -> None:
        side = (
            "pending-purchase"
            if self.type_filter.currentData() in ("PO", "GRN")
            else "pending-sales"
        )
        self.pending_report_requested.emit(side)
