"""Reports — Trial Balance, P&L, Balance Sheet, FY-close, the GST pack
(GSTR-1 plus the ADR-028 compliance fold: the 3B summary, the 2A/2B
reconciliation, the GSTR-9 scaffolding),
the bills pack's outstanding book (ADR-023: receivable/payable with
ageing, the party ledger statement, interest on overdue bills), the
orders pack's pending book (ADR-024: sales and purchase sides), and the
cost-centre break-up trio (ADR-025: centre summary, one centre's
ledger-wise detail, the category rollup).

Every report renders into a native tree/table and exports CSV through a real
file dialog; pack reports additionally save their statutory PDF — bytes
through the same print bridge the web edition uses, written to disk by
the native app, never handed to a browser.
"""

from __future__ import annotations

import csv
import io

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .client import ApiClient
from .models import TrialBalanceRow, money
from .tasks import run

#: report id -> (label, needs) where needs ∈ {"as_of", "range", "period",
#: "party_range", "centre_range", "year"}: as_of = one date; range =
#: from/to; period = YYYY-MM; party_range = a party (required) + an
#: optional from/to window (ADR-023); centre_range = a cost centre name
#: (required) + from/to (ADR-025); year = YYYY (the annual returns,
#: ADR-028).
REPORTS: dict[str, tuple[str, str]] = {
    "trial-balance": ("Trial Balance", "as_of"),
    "profit-loss": ("Profit & Loss", "range"),
    "balance-sheet": ("Balance Sheet", "as_of"),
    "fy-close": ("FY Close Preview", "as_of"),
    "gstr1": ("GSTR-1 (GST pack)", "period"),
    "gstr3b": ("GSTR-3B Summary (GST pack)", "period"),
    "gst-recon": ("GSTR-2A/2B Reconciliation (GST pack)", "period"),
    "gstr9": ("GSTR-9 Scaffolding (GST pack)", "year"),
    "bills-receivable": ("Bills Receivable (ageing)", "as_of"),
    "bills-payable": ("Bills Payable (ageing)", "as_of"),
    "ledger-statement": ("Party Ledger Statement", "party_range"),
    "bills-interest": ("Interest on Overdue Bills", "as_of"),
    "pending-sales": ("Pending Orders (sales)", "as_of"),
    "pending-purchase": ("Pending Orders (purchase)", "as_of"),
    "cost-summary": ("Cost Centre Summary", "range"),
    "cost-breakup": ("Cost Centre Break-up", "centre_range"),
    "cost-category": ("Cost Category Summary", "range"),
}

#: the pack keys whose PDF view "Save PDF…" offers (the statutory print).
PDF_PACKS = {
    "gstr1",
    "gstr3b",
    "gst-recon",
    "gstr9",
    "bills-receivable",
    "bills-payable",
    "ledger-statement",
    "bills-interest",
    "pending-sales",
    "pending-purchase",
}

#: report key -> the pack key + fixed params it runs (the pending-orders
#: pair shares one pack report with a side parameter — ADR-024).
_PACK_ALIASES = {
    "pending-sales": ("pending-orders", {"side": "sales"}),
    "pending-purchase": ("pending-orders", {"side": "purchase"}),
}


class ReportsPage(QWidget):
    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self._client = client
        self._last_csv: list[list[str]] = []

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Report"))
        self.report_box = QComboBox()
        self.report_box.setAccessibleName("Report")
        for key, (label, _needs) in REPORTS.items():
            self.report_box.addItem(label, key)
        self.report_box.currentIndexChanged.connect(self._sync_params)
        bar.addWidget(self.report_box)

        self.as_of = QDateEdit()
        self.as_of.setCalendarPopup(True)
        self.as_of.setDisplayFormat("dd-MM-yyyy")
        self.as_of.setDate(self.as_of.date().currentDate())
        self.as_of_label = QLabel("as of")
        bar.addWidget(self.as_of_label)
        bar.addWidget(self.as_of)

        self.range_label = QLabel("from")
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("dd-MM-yyyy")
        self.date_from.setDate(self.date_from.date().currentDate().addMonths(-12))
        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("dd-MM-yyyy")
        self.date_to.setDate(self.date_to.date().currentDate())
        bar.addWidget(self.range_label)
        bar.addWidget(self.date_from)
        bar.addWidget(QLabel("to"))
        bar.addWidget(self.date_to)

        self.period_label = QLabel("period (YYYY-MM)")
        self.period_edit = QLineEdit()
        self.period_edit.setPlaceholderText("e.g. 2026-10")
        bar.addWidget(self.period_label)
        bar.addWidget(self.period_edit)

        self.year_label = QLabel("year (YYYY)")
        self.year_edit = QLineEdit()
        self.year_edit.setPlaceholderText("e.g. 2026")
        bar.addWidget(self.year_label)
        bar.addWidget(self.year_edit)

        self.party_label = QLabel("party")
        self.party_edit = QLineEdit()
        self.party_edit.setPlaceholderText("e.g. Acme Traders")
        bar.addWidget(self.party_label)
        bar.addWidget(self.party_edit)

        self.centre_label = QLabel("cost centre")
        self.centre_edit = QLineEdit()
        self.centre_edit.setPlaceholderText("e.g. Mumbai")
        bar.addWidget(self.centre_label)
        bar.addWidget(self.centre_edit)

        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self.run_report)
        bar.addWidget(self.run_button)
        self.csv_button = QPushButton("Export CSV…")
        self.csv_button.clicked.connect(self._export_csv)
        self.csv_button.setEnabled(False)
        bar.addWidget(self.csv_button)
        self.pdf_button = QPushButton("Save PDF…")
        self.pdf_button.clicked.connect(self._save_pack_pdf)
        self.pdf_button.setEnabled(False)
        bar.addWidget(self.pdf_button)
        bar.addStretch(1)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Particulars", "Debit", "Credit"])
        self.tree.setAccessibleName("Report: particulars, debit, credit")
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        header = self.tree.header()
        header.resizeSection(0, 460)
        header.resizeSection(1, 160)
        header.setStretchLastSection(False)

        self.note_label = QLabel("")
        self.note_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.tree, stretch=1)
        layout.addWidget(self.note_label)

        self._sync_params()

    # ------------------------------------------------------------- params UI
    def select_report(self, key: str) -> None:
        """Pick a report in the combo (the Orders page's pending button)."""
        index = self.report_box.findData(key)
        if index >= 0:
            self.report_box.setCurrentIndex(index)

    def _sync_params(self) -> None:
        needs = REPORTS[self.report_box.currentData()][1]
        self.as_of_label.setVisible(needs == "as_of")
        self.as_of.setVisible(needs == "as_of")
        self.range_label.setVisible(needs in ("range", "party_range", "centre_range"))
        self.date_from.setVisible(needs in ("range", "party_range", "centre_range"))
        self.date_to.setVisible(needs in ("range", "party_range", "centre_range"))
        self.period_label.setVisible(needs == "period")
        self.period_edit.setVisible(needs == "period")
        self.year_label.setVisible(needs == "year")
        self.year_edit.setVisible(needs == "year")
        self.party_label.setVisible(needs == "party_range")
        self.party_edit.setVisible(needs == "party_range")
        self.centre_label.setVisible(needs == "centre_range")
        self.centre_edit.setVisible(needs == "centre_range")
        self.pdf_button.setEnabled(False)

    # --------------------------------------------------------------- running
    def run_report(self) -> None:
        key = self.report_box.currentData()
        loaders = {
            "trial-balance": self._load_trial_balance,
            "profit-loss": self._load_profit_loss,
            "balance-sheet": self._load_balance_sheet,
            "fy-close": self._load_fy_close,
            "gstr1": self._load_gstr1,
            "gstr3b": self._load_gstr3b,
            "gst-recon": self._load_gst_recon,
            "gstr9": self._load_gstr9,
            "bills-receivable": self._load_bills,
            "bills-payable": self._load_bills,
            "ledger-statement": self._load_statement,
            "bills-interest": self._load_interest,
            "pending-sales": self._load_pending_sales,
            "pending-purchase": self._load_pending_purchase,
            "cost-summary": self._load_cost_summary,
            "cost-breakup": self._load_cost_breakup,
            "cost-category": self._load_cost_category,
        }
        try:
            loader = loaders[key]()
        except ValueError as exc:
            QMessageBox.warning(self, "Reports", str(exc))
            return
        run(loader, self._render, self._failed)

    def _load_trial_balance(self):
        as_of = self.as_of.date().toPython()
        return lambda: self._client.trial_balance(as_of)

    def _load_profit_loss(self):
        d_from = self.date_from.date().toPython()
        d_to = self.date_to.date().toPython()
        return lambda: ("profit-loss", self._client.profit_loss(d_from, d_to))

    def _load_balance_sheet(self):
        as_of = self.as_of.date().toPython()
        return lambda: ("balance-sheet", self._client.balance_sheet(as_of))

    def _load_fy_close(self):
        as_of = self.as_of.date().toPython()
        return lambda: ("fy-close", self._client.fy_close(as_of))

    def _load_gstr1(self):
        period = self.period_edit.text().strip()
        if len(period) != 7 or period[4] != "-":
            raise ValueError("GSTR-1 needs a period like 2026-10 (YYYY-MM).")
        return lambda: self._client.pack("gstr1", period=period, format_="csv")

    def _load_gstr3b(self):
        period = self.period_edit.text().strip()
        if len(period) != 7 or period[4] != "-":
            raise ValueError("GSTR-3B needs a period like 2026-10 (YYYY-MM).")
        return lambda: self._client.pack("gstr3b", period=period, format_="csv")

    def _load_gst_recon(self):
        period = self.period_edit.text().strip()
        if len(period) != 7 or period[4] != "-":
            raise ValueError("The reconciliation needs a period like 2026-10 (YYYY-MM).")
        return lambda: self._client.pack("gst-recon", period=period, format_="csv")

    def _load_gstr9(self):
        year = self.year_edit.text().strip()
        if len(year) != 4 or not year.isdigit():
            raise ValueError("GSTR-9 needs a year like 2026 (YYYY).")
        return lambda: self._client.pack("gstr9", period=year, format_="csv")

    def _load_bills(self):
        key = self.report_box.currentData()  # bills-receivable | bills-payable
        as_of = self.as_of.date().toPython()
        return lambda: self._client.pack(key, as_of=as_of.isoformat(), format_="csv")

    def _load_statement(self):
        party = self.party_edit.text().strip()
        if not party:
            raise ValueError("The ledger statement needs a party (ledger name).")
        d_from = self.date_from.date().toPython()
        d_to = self.date_to.date().toPython()

        def fetch():
            params = {"party": party, "format_": "csv"}
            if d_from:
                params["from"] = d_from.isoformat()
            if d_to:
                params["to"] = d_to.isoformat()
            return self._client.pack("ledger-statement", **params)

        return fetch

    def _load_interest(self):
        as_of = self.as_of.date().toPython()
        return lambda: self._client.pack("bills-interest", as_of=as_of.isoformat(), format_="csv")

    def _load_pending_sales(self):
        as_of = self.as_of.date().toPython()
        return lambda: self._client.pack(
            "pending-orders", side="sales", as_of=as_of.isoformat(), format_="csv"
        )

    def _load_pending_purchase(self):
        as_of = self.as_of.date().toPython()
        return lambda: self._client.pack(
            "pending-orders", side="purchase", as_of=as_of.isoformat(), format_="csv"
        )

    def _load_cost_summary(self):
        d_from = self.date_from.date().toPython()
        d_to = self.date_to.date().toPython()
        return lambda: ("cost-summary", self._client.cost_centre_summary(d_from, d_to))

    def _load_cost_breakup(self):
        centre = self.centre_edit.text().strip()
        if not centre:
            raise ValueError("The break-up needs a cost centre name (e.g. Mumbai).")

        def fetch():
            centres = self._client.cost_centres()
            match = next(
                (c for c in centres if not c.is_root and c.name.lower() == centre.lower()), None
            )
            if match is None:
                raise ValueError(f"No cost centre named {centre!r} in this book.")
            d_from = self.date_from.date().toPython()
            d_to = self.date_to.date().toPython()
            payload = self._client.cost_centre_breakup(match.id, d_from, d_to)
            payload["centre_name"] = match.name
            return ("cost-breakup", payload)

        return fetch

    def _load_cost_category(self):
        d_from = self.date_from.date().toPython()
        d_to = self.date_to.date().toPython()
        return lambda: ("cost-category", self._client.cost_category_summary(d_from, d_to))

    # -------------------------------------------------------------- rendering
    def _render(self, payload: object) -> None:
        self.tree.clear()
        self._last_csv = []
        if isinstance(payload, dict) and "ledgers" in payload:
            self._render_trial_balance(payload)
        elif isinstance(payload, tuple) and len(payload) == 3:
            content, _media, name = payload  # a pack result (content, type, name)
            if name.lower().endswith(".csv") or isinstance(content, str):
                self._render_csv(
                    content.decode("utf-8-sig") if isinstance(content, bytes) else content
                )
            else:
                self._write_pack(content, name)
        elif isinstance(payload, tuple) and len(payload) == 2:
            kind, data = payload
            if kind == "profit-loss":
                self._render_profit_loss(data)
            elif kind == "balance-sheet":
                self._render_balance_sheet(data)
            elif kind == "cost-summary":
                self._render_cost_summary(data)
            elif kind == "cost-breakup":
                self._render_cost_breakup(data)
            elif kind == "cost-category":
                self._render_cost_category(data)
            else:
                self._render_fy_close(data)
        else:  # pragma: no cover - defensive: an unexpected payload
            self._failed(TypeError(f"unknown report payload {type(payload).__name__}"))
            return
        self.csv_button.setEnabled(bool(self._last_csv))

    def _top(
        self, label: str, debit: str = "", credit: str = "", bold: bool = False
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem([label, debit, credit])
        if bold:
            font = QFont()
            font.setBold(True)
            for column in range(3):
                item.setFont(column, font)
        return item

    def _render_trial_balance(self, data: dict) -> None:
        rows: list[TrialBalanceRow] = data["ledgers"]
        for row in rows:
            self.tree.addTopLevelItem(
                self._top(
                    row.ledger_name,
                    money(row.dr) if row.dr else "",
                    money(row.cr) if row.cr else "",
                )
            )
            self._last_csv.append(
                [row.ledger_name, str(row.dr), str(row.cr), row.group_name, row.nature]
            )
        totals = data.get("totals", {})
        self.tree.addTopLevelItem(
            self._top("TOTAL", money(totals.get("dr", 0)), money(totals.get("cr", 0)), bold=True)
        )
        difference = totals.get("difference", "0")
        self.note_label.setText(
            f"as of {data.get('as_of', '')} — {len(rows)} ledgers; "
            f"the engine proved the balance (difference {money(difference)})"
        )
        self._last_csv.append(["TOTAL", str(totals.get("dr", 0)), str(totals.get("cr", 0)), "", ""])

    def _render_profit_loss(self, data: dict) -> None:
        trading = data.get("trading", {})
        pl = data.get("profit_and_loss", {})

        def section(parent: QTreeWidgetItem, title: str, groups: list[dict]) -> None:
            for group in groups:
                child = self._top(group["group_name"], money(group["amount"]))
                parent.addChild(child)
                label = f"{title} — {group['group_name']}"
                self._last_csv.append([label, str(group["amount"]), "", "", ""])

        direct_in = self._top("Direct Incomes", bold=True)
        section(direct_in, "Direct Incomes", trading.get("direct_incomes", []))
        direct_out = self._top("Direct Expenses", bold=True)
        section(direct_out, "Direct Expenses", trading.get("direct_expenses", []))
        gross = self._top("GROSS PROFIT", money(trading.get("gross_profit", 0)), bold=True)
        indirect_in = self._top("Indirect Incomes", bold=True)
        section(indirect_in, "Indirect Incomes", pl.get("indirect_incomes", []))
        indirect_out = self._top("Indirect Expenses", bold=True)
        section(indirect_out, "Indirect Expenses", pl.get("indirect_expenses", []))
        net = self._top("NET PROFIT", money(pl.get("net_profit", 0)), bold=True)

        for item in (direct_in, direct_out, gross, indirect_in, indirect_out, net):
            self.tree.addTopLevelItem(item)
            item.setExpanded(True)
        self._last_csv.append(["GROSS PROFIT", str(trading.get("gross_profit", 0)), "", "", ""])
        self._last_csv.append(["NET PROFIT", str(pl.get("net_profit", 0)), "", "", ""])
        self.note_label.setText(f"{data.get('from', '')} → {data.get('to', '')}")

    def _render_balance_sheet(self, data: dict) -> None:
        sections = ("assets", "ASSETS"), ("liabilities", "LIABILITIES"), ("equity", "EQUITY")
        for key, title in sections:
            section = data.get(key, {})
            parent = self._top(title, bold=True)
            for group in section.get("groups", []):
                parent.addChild(self._top(group["group_name"], money(group["amount"])))
                label = f"{title} — {group['group_name']}"
                self._last_csv.append([label, str(group["amount"]), "", "", ""])
            total = section.get("total", 0)
            if key == "equity" and "net_profit_to_date" in section:
                parent.addChild(
                    self._top("Net profit to date", money(section["net_profit_to_date"]))
                )
            parent.setText(1 if key == "assets" else 2, money(total))
            self.tree.addTopLevelItem(parent)
            parent.setExpanded(True)
            self._last_csv.append([f"{title} TOTAL", "", str(total), "", ""])
        self.note_label.setText(f"as of {data.get('as_of', '')}")

    def _render_fy_close(self, data: dict) -> None:
        lines = data.get("lines", [])
        for line in lines:
            debit = money(line["amount"]) if line["dc"] == "D" else ""
            credit = money(line["amount"]) if line["dc"] == "C" else ""
            self.tree.addTopLevelItem(self._top(line["ledger_name"], debit, credit))
            self._last_csv.append([line["ledger_name"], str(line["amount"]), line["dc"], "", ""])
        balanced = data.get("balanced", True)
        self.note_label.setText(
            "the close sweep zeroes every income/expense ledger and moves the "
            f"net to Profit & Loss A/c — balanced: {balanced}"
        )

    # ----------------------------------------------------- cost-centre reports
    def _render_cost_summary(self, data: dict) -> None:
        """The centre tree with each node's subtree totals (ADR-025) — the
        rollup the closure computes; the Unallocated mass rides at the end."""
        rows = data.get("centres", [])
        by_id = {row["centre_id"]: row for row in rows}
        items: dict[int, QTreeWidgetItem] = {}
        # parents first (id order is creation order; parents are older)
        for row in rows:
            node = self._top(
                row["centre_name"] + (" (root)" if row["is_root"] else ""),
                money(row["dr"]) if row["dr"] != "0" else "",
                money(row["cr"]) if row["cr"] != "0" else "",
            )
            items[row["centre_id"]] = node
        for row in rows:
            parent = by_id.get(row["parent_id"]) if row["parent_id"] else None
            if parent is not None and parent["centre_id"] in items:
                items[parent["centre_id"]].addChild(items[row["centre_id"]])
            else:
                self.tree.addTopLevelItem(items[row["centre_id"]])
            self._last_csv.append(
                [
                    row["centre_name"],
                    row["dr"],
                    row["cr"],
                    row.get("category_name") or "",
                    "root" if row["is_root"] else "",
                ]
            )
        unallocated = data.get("unallocated", {"dr": "0", "cr": "0", "net": "0"})
        self.tree.addTopLevelItem(
            self._top(
                "Unallocated",
                money(unallocated["dr"]) if unallocated["dr"] != "0" else "",
                money(unallocated["cr"]) if unallocated["cr"] != "0" else "",
            )
        )
        self._last_csv.append(["Unallocated", unallocated["dr"], unallocated["cr"], "", ""])
        self.note_label.setText(
            f"{data.get('from', '')} → {data.get('to', '')} — each centre carries its "
            "subtree; allocated + Unallocated is the window's whole entry mass (ADR-025)"
        )

    def _render_cost_breakup(self, data: dict) -> None:
        """One centre's ledger-wise detail (ADR-025) — its own allocations,
        not its subtree's."""
        centre_name = data.get("centre_name", "")
        for row in data.get("ledgers", []):
            self.tree.addTopLevelItem(
                self._top(
                    f"{row['ledger_name']} ({row['nature']})",
                    money(row["dr"]) if row["dr"] != "0" else "",
                    money(row["cr"]) if row["cr"] != "0" else "",
                )
            )
            self._last_csv.append(
                [row["ledger_name"], row["dr"], row["cr"], row["group_name"], row["nature"]]
            )
        totals = data.get("totals", {"dr": "0", "cr": "0"})
        self.tree.addTopLevelItem(
            self._top("TOTAL", money(totals["dr"]), money(totals["cr"]), bold=True)
        )
        self._last_csv.append(["TOTAL", totals["dr"], totals["cr"], "", ""])
        self.note_label.setText(
            f"{centre_name} — {data.get('from', '')} → {data.get('to', '')}; the engine "
            "proved the total against the ledger rows"
        )

    def _render_cost_category(self, data: dict) -> None:
        """The category rollup (ADR-025) — primary categories carry their
        secondaries' centres."""
        rows = data.get("categories", [])
        by_id = {row["category_id"]: row for row in rows}
        items: dict[int, QTreeWidgetItem] = {}
        for row in rows:
            items[row["category_id"]] = self._top(
                row["category_name"] + (" (root)" if row["is_root"] else ""),
                money(row["dr"]) if row["dr"] != "0" else "",
                money(row["cr"]) if row["cr"] != "0" else "",
            )
        for row in rows:
            parent = by_id.get(row["parent_id"]) if row["parent_id"] else None
            if parent is not None and parent["category_id"] in items:
                items[parent["category_id"]].addChild(items[row["category_id"]])
            else:
                self.tree.addTopLevelItem(items[row["category_id"]])
            self._last_csv.append([row["category_name"], row["dr"], row["cr"], "", ""])
        unallocated = data.get("unallocated", {"dr": "0", "cr": "0", "net": "0"})
        self.tree.addTopLevelItem(
            self._top(
                "Unallocated",
                money(unallocated["dr"]) if unallocated["dr"] != "0" else "",
                money(unallocated["cr"]) if unallocated["cr"] != "0" else "",
            )
        )
        self._last_csv.append(["Unallocated", unallocated["dr"], unallocated["cr"], "", ""])
        self.note_label.setText(
            f"{data.get('from', '')} → {data.get('to', '')} — a category carries its "
            "descendant categories' centres (ADR-025)"
        )

    def _render_csv(self, text: str) -> None:
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return
        columns = max(len(r) for r in rows)
        self.tree.setColumnCount(max(3, columns))
        self.tree.setHeaderLabels(rows[0] + [""] * (max(3, columns) - len(rows[0])))
        for row in rows[1:]:
            item = QTreeWidgetItem(row + [""] * (columns - len(row)))
            self.tree.addTopLevelItem(item)
        self._last_csv = rows
        self.note_label.setText(
            f"{len(rows) - 1} rows — use “Save PDF…” for the statutory GSTR-1 print"
        )

    # -------------------------------------------------------------- exporting
    def _export_csv(self) -> None:
        if not self._last_csv:
            return
        key = self.report_box.currentData()
        target, _filter = QFileDialog.getSaveFileName(
            self, "Export CSV", f"ledgerforge-{key}.csv", "CSV (*.csv)"
        )
        if not target:
            return
        with open(target, "w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(self._last_csv)
        QMessageBox.information(self, "Export", f"Saved {target}")

    def _save_pack_pdf(self) -> None:
        key = self.report_box.currentData()
        if key not in PDF_PACKS:
            return
        params = {}
        if key in ("gstr1", "gstr3b", "gst-recon"):
            params = {"period": self.period_edit.text().strip()}
        elif key == "gstr9":
            params = {"period": self.year_edit.text().strip()}
        elif key == "ledger-statement":
            party = self.party_edit.text().strip()
            if not party:
                QMessageBox.warning(self, "Reports", "The ledger statement needs a party.")
                return
            params = {"party": party}
        elif key in ("bills-receivable", "bills-payable", "bills-interest"):
            params = {"as_of": self.as_of.date().toPython().isoformat()}
        elif key in ("pending-sales", "pending-purchase"):
            pack_key, fixed = _PACK_ALIASES[key]
            params = {
                **fixed,
                "as_of": self.as_of.date().toPython().isoformat(),
            }

        def fetch():
            if key in _PACK_ALIASES:
                pack_key, fixed = _PACK_ALIASES[key]
                return self._client.pack(pack_key, **{**fixed, **params})
            return self._client.pack(key, **params)  # PDF, the printed report

        run(
            fetch,
            lambda result: self._write_pack(result[0], result[2] or f"{key}.pdf"),
            self._failed,
        )

    def _write_pack(self, content: bytes, filename: str) -> None:
        target, _filter = QFileDialog.getSaveFileName(
            self, "Save report PDF", filename, "PDF (*.pdf)"
        )
        if not target:
            return
        with open(target, "wb") as handle:
            handle.write(content)
        QMessageBox.information(self, "Report", f"Saved {filename}")

    def _failed(self, error: object) -> None:
        detail = getattr(error, "detail", str(error))
        if "No pack report registered" in detail:
            detail += "\n\n(the plugin providing this report must be installed in this build)"
        QMessageBox.critical(self, "Reports", f"Could not run the report:\n{detail}")

    def refresh(self) -> None:
        self.run_report()
