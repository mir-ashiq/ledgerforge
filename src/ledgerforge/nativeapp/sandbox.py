"""Sandbox — the plugin UI slots (ADR-019), made visible in the native shell.

A slot is a plugin's dashboard surface, sandboxed by the host. In the native
app the list is informational today (what is installed, from where, trusted
how) — the same registry the web edition iframes, surfaced so a bookkeeper
can see which plugins are live without opening a network deployment.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .client import ApiClient
from .tasks import run
from .widgets import error_detail, fill_table, make_table


class SandboxPage(QWidget):
    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client

        self.table = make_table(["Slot", "Title", "Plugin", "Source", "Trust", "URL"])
        self.table.setColumnWidth(0, 200)
        self.table.setColumnWidth(1, 240)
        self.table.setColumnWidth(2, 160)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(4, 90)

        self.note = QLabel(
            "Plugin dashboards registered by installed plugins (ADR-019). Inline slots are "
            "sandboxed HTML; URL slots point at their own host."
        )
        self.note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addWidget(self.table, stretch=1)

    def reload(self) -> None:
        run(self._client.slots, self._show, self._failed)

    def _show(self, slots: list) -> None:
        fill_table(
            self.table,
            [
                [
                    s.key,
                    s.title,
                    s.plugin,
                    s.source,
                    s.trust,
                    s.url or "(inline, sandboxed)",
                ]
                for s in slots
            ],
        )
        if not slots:
            self.note.setText(
                "No plugin slots are registered. Drop plugins into the data directory's "
                "plugins/ folder and they appear here (ADR-016 trust gates apply)."
            )

    def _failed(self, error: object) -> None:
        self.note.setText(f"Could not load slots: {error_detail(error)}")
