"""The Day Book — the native home screen, TallyPrime's day book made Qt.

Date/type filters, the engine-computed amount column (ADR-015 — the client
never sums), double-click or Enter to open a voucher, Ctrl+N to post one.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..seeding import VOUCHER_TYPES
from .client import ApiClient
from .models import Voucher, money
from .tasks import run
from .widgets import fill_table, make_table

#: The filter's type list: every seeded system type, code first (Tally habit).
_TYPE_CODES = [code for code, _name, *_rest in VOUCHER_TYPES]


class DayBookPage(QWidget):
    open_voucher_requested = Signal(int)  # voucher id
    new_voucher_requested = Signal()

    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self._vouchers: list[Voucher] = []

        # ---- filter bar -------------------------------------------------
        bar = QHBoxLayout()
        bar.addWidget(QLabel("From"))
        self.from_date = QDateEdit()
        self.from_date.setCalendarPopup(True)
        self.from_date.setDisplayFormat("dd-MM-yyyy")
        self.from_date.setDate(QDate(QDate.currentDate().year(), 4, 1))  # the Indian FY
        bar.addWidget(self.from_date)
        bar.addWidget(QLabel("To"))
        self.to_date = QDateEdit()
        self.to_date.setCalendarPopup(True)
        self.to_date.setDisplayFormat("dd-MM-yyyy")
        self.to_date.setDate(QDate.currentDate())
        bar.addWidget(self.to_date)
        bar.addWidget(QLabel("Type"))
        self.type_filter = QComboBox()
        self.type_filter.setAccessibleName("Voucher type filter")
        self.type_filter.addItem("All", "")
        for code, name, *_rest in VOUCHER_TYPES:
            self.type_filter.addItem(f"{code} — {name}", code)
        bar.addWidget(self.type_filter)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search narration…")
        self.search_box.setClearButtonEnabled(True)
        bar.addWidget(self.search_box, stretch=1)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.reload)
        bar.addWidget(self.refresh_button)
        self.new_button = QPushButton("New Voucher…")
        self.new_button.clicked.connect(self.new_voucher_requested.emit)
        bar.addWidget(self.new_button)

        # ---- the table --------------------------------------------------
        self.table = make_table(["Voucher No", "Date", "Type", "Amount", "Narration"])
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 140)
        self.table.itemDoubleClicked.connect(self._open_selected)
        self.search_box.textChanged.connect(self._refilter)

        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.table, stretch=1)

    # ------------------------------------------------------------- loading
    def reload(self) -> None:
        def load() -> list[Voucher]:
            return self._client.vouchers(
                type_code=self.type_filter.currentData() or "",
                date_from=self.from_date.date().toPython(),
                date_to=self.to_date.date().toPython(),
            )

        run(load, self._show, self._failed)

    def _show(self, vouchers: list[Voucher]) -> None:
        self._vouchers = vouchers
        self._refilter(self.search_box.text())

    def _failed(self, error: object) -> None:
        from .widgets import error_box, error_detail

        error_box(self, "Day Book", f"Could not load the day book:\n{error_detail(error)}")

    def _refilter(self, needle: str) -> None:
        needle = (needle or "").casefold()
        rows = []
        for v in self._vouchers:
            hay = f"{v.voucher_no} {v.narration or ''}".casefold()
            if needle and needle not in hay:
                continue
            rows.append(
                [
                    v.voucher_no,
                    v.voucher_date.strftime("%d-%m-%Y"),
                    v.voucher_type,
                    money(v.amount),
                    v.narration or "",
                ]
            )
        fill_table(self.table, rows)
        header = self.table.horizontalHeader()
        header.resizeSections()
        self.table.sortItems(1, Qt.SortOrder.DescendingOrder)  # newest date first

    # -------------------------------------------------------------- opening
    def _open_selected(self, *_: object) -> None:
        selected = self.table.selectedItems()
        if not selected:
            return
        row = self.table.row(selected[0])
        if 0 <= row < len(self.table.selectedIndexes()):
            voucher = self._visible_voucher(row)
            if voucher is not None:
                self.open_voucher_requested.emit(voucher.id)

    def _visible_voucher(self, row: int) -> Voucher | None:
        voucher_no = self.table.item(row, 0).text()
        for v in self._vouchers:
            if v.voucher_no == voucher_no:
                return v
        return None

    def refresh(self) -> None:
        """The window's F5 lands here."""
        self.reload()


def current_fy_bounds(today: date | None = None) -> tuple[date, date]:
    """The Indian financial year around ``today``: Apr 1 – Mar 31."""
    today = today or date.today()
    year = today.year if today.month >= 4 else today.year - 1
    return date(year, 4, 1), date(year + 1, 3, 31)
