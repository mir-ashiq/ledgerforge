"""Masters — the chart of accounts and the cost trees: ledgers under their
groups, cost centres under their root with their categories.

Creating a ledger or a cost centre posts to the same validation the API
enforces; duplicates surface the engine's own message in a message box,
not a SQL traceback.
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
from .models import CostCategory, CostCentre, Group, Ledger
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


class NewCostCategoryDialog(QDialog):
    """A classification axis (ADR-025) — name + parent."""

    created = Signal(object)  # CostCategory

    def __init__(
        self, client: ApiClient, categories: list[CostCategory], parent: QWidget | None = None
    ):
        super().__init__(parent)
        self._client = client
        self.setWindowTitle("New Cost Category")
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Department")
        layout.addWidget(self.name_edit)
        layout.addWidget(QLabel("Under"))
        self.parent_box = QComboBox()
        for category in categories:
            self.parent_box.addItem(
                category.name + (" (root)" if category.is_root else ""), category.name
            )
        layout.addWidget(self.parent_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create category")
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _submit(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "New Cost Category", "The category needs a name.")
            return

        def create() -> CostCategory:
            return self._client.create_cost_category(
                name, parent_name=self.parent_box.currentData()
            )

        run(
            create,
            lambda category: (self.created.emit(category), self.accept()),
            lambda error: QMessageBox.critical(
                self, "New Cost Category", f"The engine refused:\n{error_detail(error)}"
            ),
        )


class NewCostCentreDialog(QDialog):
    """An allocation node (ADR-025) — name, parent centre, category."""

    created = Signal(object)  # CostCentre

    def __init__(
        self,
        client: ApiClient,
        centres: list[CostCentre],
        categories: list[CostCategory],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._client = client
        self.setWindowTitle("New Cost Centre")
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Mumbai branch")
        layout.addWidget(self.name_edit)
        layout.addWidget(QLabel("Code (optional)"))
        self.code_edit = QLineEdit()
        layout.addWidget(self.code_edit)
        layout.addWidget(QLabel("Under"))
        self.parent_box = QComboBox()
        for centre in centres:
            self.parent_box.addItem(
                centre.name + (" (root)" if centre.is_root else ""), centre.name
            )
        layout.addWidget(self.parent_box)
        layout.addWidget(QLabel("Category"))
        self.category_box = QComboBox()
        for category in categories:
            if not category.is_root:
                self.category_box.addItem(category.name, category.name)
        layout.addWidget(self.category_box)
        if self.category_box.count() == 0:
            QMessageBox.information(
                self,
                "New Cost Centre",
                "No cost category exists yet — create one on the Categories "
                "tab first (every real centre answers to a category).",
            )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create centre")
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _submit(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "New Cost Centre", "The centre needs a name.")
            return
        if self.category_box.count() == 0:
            QMessageBox.warning(
                self, "New Cost Centre", "Create a cost category first (Categories tab)."
            )
            return

        def create() -> CostCentre:
            return self._client.create_cost_centre(
                name,
                parent_name=self.parent_box.currentData(),
                category_name=self.category_box.currentData(),
                code=self.code_edit.text().strip() or None,
            )

        run(
            create,
            lambda centre: (self.created.emit(centre), self.accept()),
            lambda error: QMessageBox.critical(
                self, "New Cost Centre", f"The engine refused:\n{error_detail(error)}"
            ),
        )


class MastersPage(QWidget):
    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self._groups: list[Group] = []
        self._ledgers: list[Ledger] = []
        self._cost_centres: list[CostCentre] = []
        self._cost_categories: list[CostCategory] = []

        self.tabs = QTabWidget()
        self.ledger_table = make_table(["Ledger", "Code", "Group", "System"])
        self.group_table = make_table(["Group", "Code", "Nature", "Affects Gross", "Root"])
        self.centre_table = make_table(["Cost Centre", "Code", "Parent", "Category", "Root"])
        self.category_table = make_table(["Cost Category", "Parent", "Root"])
        self.tabs.addTab(self.ledger_table, "Ledgers")
        self.tabs.addTab(self.group_table, "Groups")
        self.tabs.addTab(self.centre_table, "Cost Centres")
        self.tabs.addTab(self.category_table, "Cost Categories")

        bar = QHBoxLayout()
        self.new_ledger_button = QPushButton("New Ledger…")
        self.new_ledger_button.clicked.connect(self._new_ledger)
        bar.addWidget(self.new_ledger_button)
        self.new_centre_button = QPushButton("New Cost Centre…")
        self.new_centre_button.clicked.connect(self._new_cost_centre)
        bar.addWidget(self.new_centre_button)
        self.new_category_button = QPushButton("New Cost Category…")
        self.new_category_button.clicked.connect(self._new_cost_category)
        bar.addWidget(self.new_category_button)
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
        run(self._client.cost_centres, self._show_centres, self._failed)
        run(self._client.cost_categories, self._show_categories, self._failed)

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
        self._refresh_count()

    def _show_centres(self, centres: list[CostCentre]) -> None:
        self._cost_centres = centres
        by_id = {centre.id: centre.name for centre in centres}
        fill_table(
            self.centre_table,
            [
                [
                    centre.name,
                    centre.code or "",
                    by_id.get(centre.parent_id, "") if centre.parent_id else "",
                    centre.category_name or "",
                    "yes" if centre.is_root else "",
                ]
                for centre in centres
            ],
        )
        self._refresh_count()

    def _show_categories(self, categories: list[CostCategory]) -> None:
        self._cost_categories = categories
        by_id = {category.id: category.name for category in categories}
        fill_table(
            self.category_table,
            [
                [
                    category.name,
                    by_id.get(category.parent_id, "") if category.parent_id else "",
                    "yes" if category.is_root else "",
                ]
                for category in categories
            ],
        )
        self._refresh_count()

    def _refresh_count(self) -> None:
        self.count_label.setText(
            f"{len(self._ledgers)} ledgers · {len(self._groups)} groups · "
            f"{len(self._cost_centres)} cost centres · {len(self._cost_categories)} categories"
        )

    def _new_ledger(self) -> None:
        if not self._groups:
            QMessageBox.information(
                self, "New Ledger", "Groups are still loading — try again in a moment."
            )
            return
        dialog = NewLedgerDialog(self._client, self._groups, self)
        dialog.created.connect(lambda _ledger: self.reload())
        dialog.exec()

    def _new_cost_centre(self) -> None:
        if not self._cost_centres:
            QMessageBox.information(
                self, "New Cost Centre", "Centres are still loading — try again in a moment."
            )
            return
        dialog = NewCostCentreDialog(self._client, self._cost_centres, self._cost_categories, self)
        dialog.created.connect(lambda _centre: self.reload())
        dialog.exec()

    def _new_cost_category(self) -> None:
        if not self._cost_categories:
            QMessageBox.information(
                self, "New Cost Category", "Categories are still loading — try again in a moment."
            )
            return
        dialog = NewCostCategoryDialog(self._client, self._cost_categories, self)
        dialog.created.connect(lambda _category: self.reload())
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

    def cost_centres(self) -> list[CostCentre]:
        """The voucher form's Ctrl+K split dialog feeds on these (ADR-025)."""
        return self._cost_centres

    def cost_categories(self) -> list[CostCategory]:
        return self._cost_categories
