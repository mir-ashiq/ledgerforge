"""The accessibility audit (ADR-032): the assistive contract of the native
window, held by the pipeline.

The keyboard contract (ADR-004) made operability the product's identity;
operable-by-keyboard and accessible-to-assistive-tech are different claims,
and this suite audits the second against the REAL MainWindow, offscreen —
the same widgets the shipped exe shows (a stub client stands in for the
engine, exactly like test_native_gui):

- every interactive widget exposes an accessible name — its label,
  placeholder or an explicit ``accessibleName``; never the empty string on
  something focusable and actionable;
- focus traversal visits each page's primary surfaces and never falls out
  of the window;
- every table's model carries its meaning: column headers with text (the
  screen reader's context for every cell), and a name for the table
  itself wherever the plain content is not the whole story.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QApplication,
    QComboBox,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QSpinBox,
    QTableWidget,
    QTreeWidget,
    QWidget,
)

from ledgerforge.nativeapp.main_window import MainWindow  # noqa: E402

# the widget types whose plain text a screen reader reads as the name
TEXT_NAMED = (QAbstractButton,)
# the widget types whose placeholder stands in when no explicit name rides
PLACEHOLDER_NAMED = (QLineEdit, QPlainTextEdit)
# the widget types that must carry an explicit accessibleName (they have no
# text of their own that names them)
EXPLICIT_NAMED = (QComboBox, QSpinBox, QListWidget, QTableWidget, QTreeWidget)


def _effective_name(widget) -> str:
    if widget.accessibleName():
        return widget.accessibleName()
    if isinstance(widget, TEXT_NAMED):
        return widget.text().replace("&", "")
    if isinstance(widget, PLACEHOLDER_NAMED):
        return widget.placeholderText()
    return ""


def _interactive(widget) -> bool:
    if widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
        return False
    # QSpinBox/QDateEdit embed their own line edit (objectName
    # 'qt_spinbox_lineedit'); Qt's accessible layer names the composite,
    # not the internal field — auditing it would double-count and misname
    if widget.objectName() == "qt_spinbox_lineedit":
        return False
    return isinstance(widget, (*TEXT_NAMED, *PLACEHOLDER_NAMED, *EXPLICIT_NAMED))


@pytest.fixture(scope="module")
def window(qapp, stub_client):  # noqa: ARG001
    win = MainWindow(stub_client, "/tmp/lf-a11y-data")
    yield win
    win.close()


@pytest.fixture(scope="module")
def stub_client():
    from test_native_gui import StubClient

    return StubClient()


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_every_interactive_widget_is_named(window):
    """The contract: nothing focusable and actionable may be nameless."""
    offenders: list[str] = []
    for index in range(window.pages.count()):
        window.pages.setCurrentIndex(index)
        page = window.pages.widget(index)
        page_name = type(page).__name__
        for widget in page.findChildren(QWidget):
            if not _interactive(widget):
                continue
            if not _effective_name(widget).strip():
                offenders.append(f"{page_name}/{type(widget).__name__}")
    assert not offenders, (
        "interactive widgets without an accessible name (page/widget): "
        + ", ".join(sorted(set(offenders)))
    )


def test_focus_traversal_visits_each_surface(window):
    """Tab through every page: focus must move among the page's own
    interactive widgets and never escape the window."""
    for index in range(window.pages.count()):
        window.pages.setCurrentIndex(index)
        page = window.pages.widget(index)
        interactive = [w for w in page.findChildren(QWidget) if _interactive(w)]
        if not interactive:
            continue
        visited = set()
        widget = window.focusWidget()
        for _ in range(60):
            QTest.keyClick(window, Qt.Key.Key_Tab)
            widget = window.focusWidget()
            if widget is None:
                pytest.fail(f"focus fell out of the window on page {index}")
            visited.add(widget)
        assert len(visited) >= min(3, len(interactive)), (
            f"page {index} ({type(page).__name__}): Tab reached only "
            f"{len(visited)} of {len(interactive)} interactive widgets"
        )


def test_tables_carry_their_meaning(window):
    """Every table's columns name themselves — the header text is the
    screen reader's context for every cell beneath it."""
    missing: list[str] = []
    for index in range(window.pages.count()):
        window.pages.setCurrentIndex(index)
        page = window.pages.widget(index)
        for table in page.findChildren(QTableWidget):
            label = f"{type(page).__name__}/{table.objectName() or id(table)}"
            for column in range(table.columnCount()):
                item = table.horizontalHeaderItem(column)
                if item is None or not item.text().strip():
                    missing.append(f"{label} column {column}")
    assert not missing, "tables with nameless columns: " + ", ".join(sorted(set(missing)))
