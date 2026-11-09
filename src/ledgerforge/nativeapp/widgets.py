"""Shared widget helpers — tables, headers, message boxes, the app icon."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMessageBox, QTableWidget, QTableWidgetItem, QWidget


def icon_path() -> Path:
    """The .ico, whether running from source or a frozen bundle."""
    import sys

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "desktop" / "ledgerforge.ico"
        if candidate.is_file():
            return candidate
    # source checkout: src/ledgerforge/nativeapp/widgets.py -> repo root
    return Path(__file__).resolve().parents[3] / "desktop" / "ledgerforge.ico"


def app_icon() -> QIcon:
    return QIcon(str(icon_path()))


def error_detail(error: object) -> str:
    """A thread-carried error's human text (ApiError.detail or str)."""
    return str(getattr(error, "detail", None) or error)


def make_table(headers: list[str], *, stretch_last: bool = True) -> QTableWidget:
    """A read-only, keyboard-navigable table with a native header.

    The header list doubles as the table's accessible name (ADR-032): a
    screen reader announces what the grid holds before its cells."""
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setAccessibleName(f"table: {', '.join(headers)}")
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    header = table.horizontalHeader()
    header.setStretchLastSection(stretch_last)
    header.setSectionsMovable(False)
    return table


def text_item(value: str, *, align: Qt.AlignmentFlag | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(value)
    if align is not None:
        item.setTextAlignment(align.value if hasattr(align, "value") else int(align))
    return item


def fill_table(table: QTableWidget, rows: list[list[QTableWidgetItem | str]]) -> None:
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            item = cell if isinstance(cell, QTableWidgetItem) else QTableWidgetItem(str(cell))
            item.setData(Qt.ItemDataRole.UserRole, r)
            table.setItem(r, c, item)


def error_box(parent: QWidget, title: str, detail: str) -> None:
    QMessageBox.critical(parent, title, detail)


def info_box(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)
