"""The main window — one native frame around the whole product.

Sidebar navigation (Alt+1..9), native menu bar, a status bar that carries the
engine's heartbeat, and the keyboard identity the engine was built for:
Ctrl+N posts a voucher from anywhere, F5 refreshes the page in focus, Ctrl+Q
quits. The window IS the application — nothing opens a browser, ever.
"""

from __future__ import annotations

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from .banking import BankingPage
from .client import ApiClient
from .dataexchange import DataExchangePage
from .daybook import DayBookPage
from .gst import GstPage
from .inventory import InventoryPage
from .masters import MastersPage
from .models import Book
from .order_dialog import NewOrderDialog
from .orders import OrdersPage
from .payroll import PayrollPage
from .periods import PeriodsPage
from .reports import ReportsPage
from .sandbox import SandboxPage
from .tasks import run
from .updater import UpdateController
from .voucher_dialog import NewVoucherDialog, VoucherViewDialog
from .widgets import app_icon, error_detail

#: (page key, sidebar label) in navigation order — the Orders page joins
#: after Masters (ADR-024): the quote-to-cash and procure-to-pay documents
#: get their own home between the masters they name and the reports they
#: feed. Inventory follows Banking (ADR-027): the stock depth's masters,
#: positions and recipes. GST follows Inventory (ADR-028): the compliance
#: workbench. Payroll follows GST (ADR-029): the people workbench. Data
#: Exchange follows Payroll (ADR-030): the migration workbench — imports,
#: the TallyPrime path, exports. Alt+1..9 and Alt+0 (the tenth) follow the
#: list; the eleventh page keeps its sidebar place without an Alt key —
#: ten direct keys is the keyboard's own ceiling, and Sandbox is the
#: playground, not the daily driver.
PAGES = [
    ("daybook", "Day Book"),
    ("masters", "Masters"),
    ("orders", "Orders"),
    ("banking", "Banking"),
    ("inventory", "Inventory"),
    ("gst", "GST"),
    ("payroll", "Payroll"),
    ("dataexchange", "Data Exchange"),
    ("reports", "Reports"),
    ("periods", "Periods"),
    ("sandbox", "Sandbox"),
]


class MainWindow(QMainWindow):
    def __init__(self, client: ApiClient, data_dir: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self._data_dir = data_dir
        self._books: list[Book] = []

        self.setWindowTitle("LedgerForge")
        self.setWindowIcon(app_icon())
        self.resize(1180, 760)

        # ---- pages ------------------------------------------------------
        self.pages = QStackedWidget()
        self.daybook = DayBookPage(client)
        self.masters = MastersPage(client)
        self.orders = OrdersPage(client)
        self.banking = BankingPage(client)
        self.inventory = InventoryPage(client)
        self.gst = GstPage(client)
        self.payroll = PayrollPage(client)
        self.dataexchange = DataExchangePage(client)
        self.reports = ReportsPage(client)
        self.periods = PeriodsPage(client)
        self.sandbox = SandboxPage(client)
        for page in (
            self.daybook,
            self.masters,
            self.orders,
            self.banking,
            self.inventory,
            self.gst,
            self.payroll,
            self.dataexchange,
            self.reports,
            self.periods,
            self.sandbox,
        ):
            self.pages.addWidget(page)

        self.daybook.open_voucher_requested.connect(self.open_voucher)
        self.daybook.new_voucher_requested.connect(self.new_voucher)
        self.orders.open_voucher_requested.connect(self.open_voucher)
        self.orders.new_order_requested.connect(self.new_order)
        self.orders.pending_report_requested.connect(self._goto_pending_report)

        # ---- sidebar ----------------------------------------------------
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(190)
        for _key, label in PAGES:
            QListWidgetItem(label, self.sidebar)
        self.sidebar.setCurrentRow(0)
        self.sidebar.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sidebar.currentRowChanged.connect(self._page_changed)

        # central: sidebar + pages
        from PySide6.QtWidgets import QHBoxLayout

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.pages, stretch=1)
        self.setCentralWidget(central)

        self._build_menus()
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.engine_status = self.status_bar
        self.status_bar.showMessage("engine starting…")

        # ADR-031: the updater's words live on the right of the status bar —
        # permanent, quiet, never over the engine's own state
        from PySide6.QtWidgets import QLabel

        self.update_label = QLabel("")
        self.status_bar.addPermanentWidget(self.update_label)
        self._updater = UpdateController(self._data_dir, on_status=self.update_label.setText)
        self._updater.check_on_boot()

        self._load_books()
        self.daybook.reload()
        self.masters.reload()
        self.orders.reload()
        self.sandbox.reload()

    # ---------------------------------------------------------------- menus
    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        new_action = QAction("&New voucher…", self)
        new_action.setShortcut(QKeySequence("Ctrl+N"))
        new_action.triggered.connect(self.new_voucher)
        file_menu.addAction(new_action)

        new_order_action = QAction("New &order document…", self)
        new_order_action.setShortcut(QKeySequence("Ctrl+Shift+N"))
        new_order_action.triggered.connect(self.new_order)
        file_menu.addAction(new_order_action)

        refresh_action = QAction("&Refresh", self)
        refresh_action.setShortcut(QKeySequence("F5"))
        refresh_action.triggered.connect(self.refresh_current)
        file_menu.addAction(refresh_action)

        file_menu.addSeparator()
        quit_action = QAction("E&xit", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        navigate = self.menuBar().addMenu("&Navigate")
        for index, (_key, label) in enumerate(PAGES):
            if index < 10:
                action = QAction(f"&{index + 1}  {label}", self)
                # the tenth page takes Alt+0 — the keyboard's own wrap-around
                action.setShortcut(QKeySequence(f"Alt+{(index + 1) % 10}"))
            else:
                action = QAction(f"    {label}", self)  # past the Alt decade
            action.triggered.connect(lambda _checked=False, row=index: self._goto(row))
            navigate.addAction(action)

        help_menu = self.menuBar().addMenu("&Help")
        check_updates = QAction("Check for &Updates…", self)
        check_updates.setShortcut(QKeySequence("Ctrl+U"))
        check_updates.triggered.connect(self._check_updates)
        help_menu.addAction(check_updates)

        crash_reports = QAction("Crash &reports…", self)
        crash_reports.triggered.connect(self._crash_reports)
        help_menu.addAction(crash_reports)

        help_menu.addSeparator()
        about = QAction("&About LedgerForge", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _check_updates(self) -> None:
        """The manual check (ADR-031): fetch, verify, download, stage —
        and say so in the status bar. Never a dialog over the books."""

        self.update_label.setText("checking…")
        self._updater.check()

    def _crash_reports(self) -> None:
        """The crash pledge, visible (ADR-031): reports are local, plain
        JSON, yours; sending is chosen, never defaulted."""

        from PySide6.QtWidgets import QDialogButtonBox

        from .crashes import crash_dir, endpoint_from_env

        reports = sorted(crash_dir(self._data_dir).glob("crash-*.json"))
        endpoint = endpoint_from_env()
        if not reports:
            QMessageBox.information(
                self,
                "Crash reports",
                "No crash reports — nothing has crashed.\n\n"
                "The pledge, for the record: crashes are always captured "
                "locally (this folder, plain JSON), and nothing is ever "
                "sent anywhere unless you choose to send it.",
            )
            return
        unsent = [r for r in reports if not r.with_suffix(".json.sent").exists()]
        body = (
            f"{len(reports)} crash report(s) in\n{crash_dir(self._data_dir)}\n\n"
            "They are plain JSON on your disk — open the folder to read "
            "them. LedgerForge sends nothing by default."
        )
        if endpoint and unsent:
            body += (
                f"\n\nThis machine has a collector configured ({endpoint}) — "
                "Send posts the reports to it."
            )
            box = QMessageBox(
                QMessageBox.Icon.Information,
                "Crash reports",
                body,
                QDialogButtonBox.StandardButton.Open
                | QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Apply,
                self,
            )
            box.button(QDialogButtonBox.StandardButton.Apply).setText("Send")
            box.button(QDialogButtonBox.StandardButton.Open).setText("Open folder")
            box.exec()
            if box.clickedButton() is box.button(QDialogButtonBox.StandardButton.Apply):
                from .crashes import send_pending

                sent = send_pending(self._data_dir, endpoint)
                self.status_bar.showMessage(f"sent {sent} crash report(s)")
            elif box.clickedButton() is box.button(QDialogButtonBox.StandardButton.Open):
                self._open_crash_folder()
        else:
            box = QMessageBox(
                QMessageBox.Icon.Information,
                "Crash reports",
                body,
                QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Ok,
                self,
            )
            box.button(QDialogButtonBox.StandardButton.Open).setText("Open folder")
            box.exec()
            if box.clickedButton() is box.button(QDialogButtonBox.StandardButton.Open):
                self._open_crash_folder()

    def _open_crash_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from .crashes import crash_dir

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(crash_dir(self._data_dir))))

    def _about(self) -> None:
        from .. import __version__
        from ..updating import KEY_FINGERPRINT, pending_update

        staged = pending_update(self._data_dir)
        update_line = (
            f"Update {staged['to']} staged — applies on restart"
            if staged
            else "Updates verify against the pinned release key"
        )
        QMessageBox.about(
            self,
            "About LedgerForge",
            f"<b>LedgerForge {__version__}</b><br>"
            "The native desktop edition — keyboard-first, double-entry "
            "bookkeeping with an embedded engine.<br><br>"
            f"{update_line} (Ed25519 {KEY_FINGERPRINT})<br>"
            "Crash reports stay on your machine by default — nothing is "
            "sent unless you send it.<br><br>"
            f"Data directory: <code>{self._data_dir}</code><br>"
            'AGPL-3.0 — <a href="https://github.com/mir-ashiq/ledgerforge">github.com/mir-ashiq/ledgerforge</a>',
        )

    # ------------------------------------------------------------ navigation
    def _goto(self, row: int) -> None:
        self.sidebar.setCurrentRow(row)

    def _page_changed(self, row: int) -> None:
        self.pages.setCurrentIndex(row)
        self.refresh_current()

    def refresh_current(self) -> None:
        page = self.pages.currentWidget()
        reloader = getattr(page, "reload", None) or getattr(page, "refresh", None)
        if callable(reloader):
            reloader()

    # ---------------------------------------------------------------- books
    def _load_books(self) -> None:
        def load():
            books = self._client.books()
            if books and self._client.book_id is None:
                self._client.set_book(books[0].id)
            return books

        run(load, self._books_loaded, self._books_failed)

    def _books_loaded(self, books: list[Book]) -> None:
        self._books = books
        if books:
            self.setWindowTitle(f"LedgerForge — {books[0].name}")

        def health():
            return self._client.health()

        run(health, self._engine_ready, self._engine_unreachable)

    def _engine_unreachable(self, _error: object) -> None:
        self.status_bar.showMessage("engine unreachable", 0)

    def _engine_ready(self, health: dict) -> None:
        book = f" · {self._books[0].name}" if self._books else ""
        self.status_bar.showMessage(
            f"engine {health.get('version', '?')} — connected{book} · data: {self._data_dir}", 0
        )

    def _books_failed(self, error: object) -> None:
        self.status_bar.showMessage(f"books unavailable — {error_detail(error)}", 0)

    # -------------------------------------------------------------- vouchers
    def new_voucher(self) -> None:
        names = self.masters.ledger_names()
        if not names:  # first run: pull masters inline, then open
            run(
                self._client.ledgers,
                lambda ledgers: self._open_new_voucher(
                    [led.name for led in ledgers],
                    [
                        led.name
                        for led in ledgers
                        if led.group_name in ("Sundry Debtors", "Sundry Creditors")
                    ],
                ),
                lambda error: QMessageBox.critical(
                    self, "New Voucher", f"Could not load ledgers:\n{error_detail(error)}"
                ),
            )
            return
        self._open_new_voucher(names, self.masters.party_names())

    def _open_new_voucher(self, names: list[str], party_names: list[str]) -> None:
        dialog = self._build_new_voucher(names, party_names)
        dialog.exec()

    def _build_new_voucher(
        self, names: list[str], party_names: list[str] | None = None
    ) -> NewVoucherDialog:
        dialog = NewVoucherDialog(
            self._client,
            names,
            self,
            party_names=party_names,
            cost_centres=self.masters.cost_centres(),
            cost_categories=self.masters.cost_categories(),
        )
        dialog.posted.connect(lambda _v: self.daybook.reload())
        return dialog

    def open_voucher(self, voucher_id: int) -> None:
        run(
            lambda: self._client.voucher(voucher_id),
            self._show_voucher,
            lambda error: QMessageBox.critical(
                self, "Voucher", f"Could not load the voucher:\n{error_detail(error)}"
            ),
        )

    def _show_voucher(self, voucher) -> None:
        VoucherViewDialog(self._client, voucher, self).exec()

    # ---------------------------------------------------------------- orders
    def new_order(self) -> None:
        """The New Order dialog (ADR-024) — the five documents, their own
        series, the against-picker fed by orders-open."""
        dialog = NewOrderDialog(
            self._client,
            self.masters.ledger_names(),
            self,
            party_names=self.masters.party_names(),
        )
        dialog.exec()
        self.orders.reload()

    def _goto_pending_report(self, key: str) -> None:
        """The Orders page's pending button: jump to Reports, pre-selected."""
        self.reports.select_report(key)
        self._goto(PAGES.index(("reports", "Reports")))
