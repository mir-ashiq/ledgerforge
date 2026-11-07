"""Periods — fiscal years with their lock/unlock/close lifecycle (issue #5).

Closing shows the sweep preview (the same lines ``reports/fy-close`` proves)
behind a native confirmation; locking is one click, reversible by unlock.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .client import ApiClient
from .models import Period, money
from .tasks import run
from .widgets import error_detail, fill_table, make_table


class PeriodsPage(QWidget):
    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self._periods: list[Period] = []

        self.table = make_table(["#", "Period", "From", "To", "Status"])
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 220)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 120)

        bar = QHBoxLayout()
        self.lock_button = QPushButton("Lock")
        self.lock_button.clicked.connect(self._lock)
        self.unlock_button = QPushButton("Unlock")
        self.unlock_button.clicked.connect(self._unlock)
        self.preview_button = QPushButton("Close preview…")
        self.preview_button.clicked.connect(self._preview)
        self.close_button = QPushButton("Close period…")
        self.close_button.clicked.connect(self._close)
        self.reload_button = QPushButton("Refresh")
        self.reload_button.clicked.connect(self.reload)
        for button in (
            self.lock_button,
            self.unlock_button,
            self.preview_button,
            self.close_button,
            self.reload_button,
        ):
            bar.addWidget(button)
        bar.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.table, stretch=1)

    def reload(self) -> None:
        run(self._client.periods, self._show, self._failed)

    def _show(self, periods: list[Period]) -> None:
        self._periods = periods
        fill_table(
            self.table,
            [
                [
                    str(p.id),
                    p.name,
                    p.starts_on.strftime("%d-%m-%Y"),
                    p.ends_on.strftime("%d-%m-%Y"),
                    p.status,
                ]
                for p in periods
            ],
        )

    def _selected(self) -> Period | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if rows:
            return self._periods[rows[0].row()] if rows[0].row() < len(self._periods) else None
        QMessageBox.information(self, "Periods", "Select a period row first.")
        return None

    def _lock(self) -> None:
        period = self._selected()
        if period:
            run(lambda: self._client.lock_period(period.id), lambda _p: self.reload(), self._failed)

    def _unlock(self) -> None:
        period = self._selected()
        if period:
            run(
                lambda: self._client.unlock_period(period.id),
                lambda _p: self.reload(),
                self._failed,
            )

    def _preview(self) -> None:
        period = self._selected()
        if period:
            run(
                lambda: self._client.close_preview(period.id),
                lambda data: self._show_preview(period, data),
                self._failed,
            )

    def _show_preview(self, period: Period, data: dict) -> None:
        lines = data.get("lines", [])
        if not lines:
            QMessageBox.information(
                self,
                "Close preview",
                f"Nothing to sweep for {period.name} — "
                "every income/expense ledger is already zero.",
            )
            return
        rows = "\n".join(
            f"{line['dc']}  {money(line['amount']):>14}  {line['ledger_name']}" for line in lines
        )
        QMessageBox.information(
            self,
            f"Close preview — {period.name}",
            f"The close will post {len(lines)} lines:\n\n{rows}\n\n"
            f"Balanced: {data.get('balanced', True)}",
        )

    def _close(self) -> None:
        period = self._selected()
        if period is None:
            return
        confirm = QMessageBox.question(
            self,
            "Close period",
            f"Close {period.name}?\n\nThe income/expense sweep posts for real and the "
            "period becomes read-only. This is the one action that cannot be undone "
            "from this window.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        run(lambda: self._client.close_period(period.id), lambda _p: self.reload(), self._failed)

    def _failed(self, error: object) -> None:
        QMessageBox.critical(self, "Periods", f"The engine refused:\n{error_detail(error)}")
