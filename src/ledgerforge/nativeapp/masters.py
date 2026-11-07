"""Masters — the chart of accounts: ledgers under their groups, both lists.

Creating a ledger posts to the same validation the API enforces; duplicates
surface the engine's own message in a message box, not a SQL traceback.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .client import ApiClient
from .models import Group, Ledger
from .tasks import run
from .widgets import error_detail, fill_table, make_table


class NewLedgerDialog(QDialog):
    created = Signal(object)  # Ledger

    def __init__(self, client: ApiClient, groups: list[Group], parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self._groups = groups
        self.setWindowTitle("New Ledger")
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. State Bank of India — Current")
        layout.addWidget(self.name_edit)
        layout.addWidget(QLabel("Code (optional)"))
        self.code_edit = QLineEdit()
        layout.addWidget(self.code_edit)
        layout.addWidget(QLabel("Group"))
        self.group_box = QComboBox()
        for group in sorted(groups, key=lambda g: g.name):
            self.group_box.addItem(f"{group.name} ({group.nature})", group.name)
        layout.addWidget(self.group_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create ledger")
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _submit(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "New Ledger", "The ledger needs a name.")
            return

        def create() -> Ledger:
            return self._client.create_ledger(
                name,
                group_name=self.group_box.currentData(),
                code=self.code_edit.text().strip() or None,
            )

        run(
            create,
            lambda ledger: (self.created.emit(ledger), self.accept()),
            lambda error: QMessageBox.critical(
                self, "New Ledger", f"The engine refused:\n{error_detail(error)}"
            ),
        )


class MastersPage(QWidget):
    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self._groups: list[Group] = []
        self._ledgers: list[Ledger] = []

        self.tabs = QTabWidget()
        self.ledger_table = make_table(["Ledger", "Code", "Group", "System"])
        self.group_table = make_table(["Group", "Code", "Nature", "Affects Gross", "Root"])
        self.tabs.addTab(self.ledger_table, "Ledgers")
        self.tabs.addTab(self.group_table, "Groups")

        bar = QHBoxLayout()
        self.new_ledger_button = QPushButton("New Ledger…")
        self.new_ledger_button.clicked.connect(self._new_ledger)
        bar.addWidget(self.new_ledger_button)
        self.reload_button = QPushButton("Refresh")
        self.reload_button.clicked.connect(self.reload)
        bar.addWidget(self.reload_button)
        bar.addStretch(1)
        self.count_label = QLabel()
        bar.addWidget(self.count_label)

        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.tabs, stretch=1)

    def reload(self) -> None:
        run(self._client.groups, self._show_groups, self._failed)
        run(self._client.ledgers, self._show_ledgers, self._failed)

    def _show_groups(self, groups: list[Group]) -> None:
        self._groups = groups
        fill_table(
            self.group_table,
            [
                [
                    g.name,
                    g.code or "",
                    g.nature,
                    "yes" if g.affects_gross else "",
                    "yes" if g.is_root else "",
                ]
                for g in groups
            ],
        )

    def _show_ledgers(self, ledgers: list[Ledger]) -> None:
        self._ledgers = ledgers
        fill_table(
            self.ledger_table,
            [
                [
                    ledger.name,
                    ledger.code or "",
                    ledger.group_name,
                    "yes" if ledger.is_system else "",
                ]
                for ledger in ledgers
            ],
        )
        self.count_label.setText(f"{len(ledgers)} ledgers · {len(self._groups)} groups")

    def _new_ledger(self) -> None:
        if not self._groups:
            QMessageBox.information(
                self, "New Ledger", "Groups are still loading — try again in a moment."
            )
            return
        dialog = NewLedgerDialog(self._client, self._groups, self)
        dialog.created.connect(lambda _ledger: self.reload())
        dialog.exec()

    def _failed(self, error: object) -> None:
        QMessageBox.critical(self, "Masters", f"Could not load masters:\n{error_detail(error)}")

    def ledger_names(self) -> list[str]:
        """The voucher entry form's autocomplete list."""
        return [ledger.name for ledger in self._ledgers]

    def party_names(self) -> list[str]:
        """The bill-managed party ledgers (ADR-023): Sundry Debtors and
        Sundry Creditors — the voucher form's allocation dialog keys on these."""
        return [
            ledger.name
            for ledger in self._ledgers
            if ledger.group_name in ("Sundry Debtors", "Sundry Creditors")
        ]
