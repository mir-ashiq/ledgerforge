"""The native order dialogs (ADR-024, Phase 3).

Two dialogs, one module:

- **NewOrderDialog** — post any of the five order documents: kind (with its
  own numbering series), party of the right side (Sundry Debtors for the
  sales chain, Sundry Creditors for the purchase chain), the document ref,
  an items grid (item, description, qty, rate, tax), an *against* picker
  fed by the engine's ``orders-open`` report (a sales order may convert a
  quotation, a delivery note fulfils an order, a GRN fulfils a purchase
  order), free-text terms. The document posts as one voucher; the engine
  derives the memorandum trace and the kind's own voucher number.
- **OrderAllocationDialog** — the *Order…* picker on a voucher that moves a
  party ledger: that party's open orders with pending-to-bill per line, a
  qty cell per line, ``meta.orders.settle`` armed on the post — an invoice
  settling an order, the TallyPrime flow.

The dialogs suggest; the engine decides — every cap and guard re-checks at
posting time, and a refusal keeps the draft intact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .client import ApiClient
from .tasks import run
from .widgets import error_detail

CENT = Decimal("0.01")

#: kind -> (series code, label) — the pack's five document kinds.
KIND_CHOICES: list[tuple[str, str]] = [
    ("quotation", "QT — Quotation"),
    ("sales_order", "SO — Sales Order"),
    ("delivery_note", "DN — Delivery Note"),
    ("purchase_order", "PO — Purchase Order"),
    ("grn", "GRN — Goods Receipt Note"),
]

_KIND_BY_CODE = {
    code: kind
    for kind, code in [
        ("quotation", "QT"),
        ("sales_order", "SO"),
        ("delivery_note", "DN"),
        ("purchase_order", "PO"),
        ("grn", "GRN"),
    ]
}
_KIND_LABEL = dict(KIND_CHOICES)

#: kind of the parent document the *against* picker offers (ADR-024 §1).
_PARENT_KIND = {
    "sales_order": "quotation",
    "delivery_note": "sales_order",
    "grn": "purchase_order",
}

_SALES_KINDS = ("quotation", "sales_order", "delivery_note")
_PARTY_GROUPS = ("Sundry Debtors", "Sundry Creditors")


def to_cents(raw: str) -> Decimal:
    """A typed amount -> Decimal cents; invalid text -> 0.00 (never floats)."""
    try:
        return Decimal(raw.replace(",", "").strip() or "0").quantize(CENT, rounding=ROUND_HALF_UP)
    except ArithmeticError:
        return Decimal("0.00")


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(slots=True)
class _ItemRow:
    item: QLineEdit
    description: QLineEdit
    qty: QLineEdit
    rate: QLineEdit
    tax: QComboBox


class NewOrderDialog(QDialog):
    """Post one order document — the five kinds, their own series."""

    def __init__(
        self,
        client: ApiClient,
        ledgers: list[str],
        parent: QWidget | None = None,
        *,
        party_names: list[str] | None = None,
    ):
        super().__init__(parent)
        self._client = client
        self._ledgers = ledgers
        self._party_names = list(party_names or [])
        self._rows: list[_ItemRow] = []
        self._against_docs: list[dict] = []

        self.setWindowTitle("New Order Document")
        self.setModal(True)
        self.setMinimumSize(920, 560)

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Kind"))
        self.kind_box = QComboBox()
        for kind, label in KIND_CHOICES:
            self.kind_box.addItem(label, kind)
        self.kind_box.currentIndexChanged.connect(self._kind_changed)
        top.addWidget(self.kind_box)
        top.addWidget(QLabel("Date"))
        self.date_edit = QDateEdit()
        self.date_edit.setDate(self.date_edit.date().currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd-MM-yyyy")
        top.addWidget(self.date_edit)
        top.addWidget(QLabel("Party"))
        self.party_edit = QLineEdit()
        self.party_edit.setPlaceholderText("the customer / supplier ledger name")
        top.addWidget(self.party_edit, stretch=1)
        top.addWidget(QLabel("Ref"))
        self.ref_edit = QLineEdit()
        self.ref_edit.setPlaceholderText("e.g. SO-42")
        top.addWidget(self.ref_edit)
        layout.addLayout(top)

        against_row = QHBoxLayout()
        self.against_label = QLabel("Against")
        self.against_box = QComboBox()
        self.against_box.setMinimumWidth(280)
        against_row.addWidget(self.against_label)
        against_row.addWidget(self.against_box)
        self.against_reload = QPushButton("Reload…")
        self.against_reload.clicked.connect(self._load_against)
        against_row.addWidget(self.against_reload)
        against_row.addStretch(1)
        layout.addLayout(against_row)

        terms_row = QHBoxLayout()
        terms_row.addWidget(QLabel("Terms"))
        self.terms_edit = QLineEdit()
        self.terms_edit.setPlaceholderText("printed on the document (optional)")
        terms_row.addWidget(self.terms_edit, stretch=1)
        layout.addLayout(terms_row)

        layout.addWidget(QLabel("Items — one line per item; qty × rate must total above zero"))
        self.items_table = QTableWidget(0, 5)
        self.items_table.setHorizontalHeaderLabels(["Item", "Description", "Qty", "Rate", "Tax"])
        self.items_table.verticalHeader().setVisible(False)
        self.items_table.setColumnWidth(0, 200)
        self.items_table.setColumnWidth(1, 240)
        self.items_table.setColumnWidth(2, 90)
        self.items_table.setColumnWidth(3, 110)
        self.items_table.setColumnWidth(4, 80)
        header = self.items_table.horizontalHeader()
        header.setStretchLastSection(False)
        layout.addWidget(self.items_table, stretch=1)

        buttons_row = QHBoxLayout()
        add_button = QPushButton("Add item (+)")
        add_button.clicked.connect(lambda: self.add_item())
        remove_button = QPushButton("Remove last item")
        remove_button.clicked.connect(self.remove_last_item)
        buttons_row.addWidget(add_button)
        buttons_row.addWidget(remove_button)
        buttons_row.addStretch(1)
        self.total_label = QLabel()
        buttons_row.addWidget(self.total_label)
        layout.addLayout(buttons_row)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Post document")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.add_item()
        self.add_item()
        self._kind_changed()

    # ------------------------------------------------------------- the grid
    def add_item(self) -> None:
        row = len(self._rows)
        self.items_table.insertRow(row)

        item = QLineEdit()
        item.setPlaceholderText("item name (must match the order's lines)")
        description = QLineEdit()
        qty = QLineEdit()
        qty.setPlaceholderText("1")
        qty.setAlignment(Qt.AlignmentFlag.AlignRight)
        rate = QLineEdit()
        rate.setPlaceholderText("0.00")
        rate.setAlignment(Qt.AlignmentFlag.AlignRight)
        tax = QComboBox()
        for slab in ("0", "5", "12", "18", "28"):
            tax.addItem(f"{slab}%" if slab != "0" else "—", slab)
        tax.setCurrentIndex(0)

        for editor in (item, qty, rate):
            editor.textChanged.connect(self._recap)
        self.items_table.setCellWidget(row, 0, item)
        self.items_table.setCellWidget(row, 1, description)
        self.items_table.setCellWidget(row, 2, qty)
        self.items_table.setCellWidget(row, 3, rate)
        self.items_table.setCellWidget(row, 4, tax)
        self.items_table.setRowHeight(row, 32)
        self._rows.append(_ItemRow(item, description, qty, rate, tax))

    def remove_last_item(self) -> None:
        if len(self._rows) <= 1:
            return
        self.items_table.removeRow(len(self._rows) - 1)
        self._rows.pop()

    def _items_payload(self) -> list[dict] | None:
        """The document's items; None when a row is unusable and the doc incomplete."""
        items: list[dict] = []
        for row in self._rows:
            name = row.item.text().strip()
            if not name:
                continue
            qty = to_cents(row.qty.text() or "1")
            rate_raw = row.rate.text().strip()
            entry = {"item": name, "qty": _plain(qty)}
            if row.description.text().strip():
                entry["description"] = row.description.text().strip()
            if rate_raw:
                entry["rate"] = _plain(to_cents(rate_raw))
            slab = row.tax.currentData()
            if slab and slab != "0":
                entry["tax_rate"] = slab
            items.append(entry)
        return items

    def _totals(self) -> tuple[Decimal, int]:
        subtotal = Decimal("0.00")
        usable = 0
        for row in self._rows:
            if not row.item.text().strip():
                continue
            usable += 1
            qty = to_cents(row.qty.text() or "1")
            rate = to_cents(row.rate.text())
            subtotal += qty * rate
        return subtotal.quantize(CENT), usable

    def _recap(self) -> None:
        subtotal, usable = self._totals()
        ok = (
            bool(self._party_ok())
            and bool(self.ref_edit.text().strip())
            and usable > 0
            and subtotal > 0
        )
        self.total_label.setText(f"Subtotal {subtotal:,.2f}")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def _party_ok(self) -> bool:
        return bool(self.party_edit.text().strip())

    # ------------------------------------------------------------ the kind
    def _kind_changed(self) -> None:
        kind = self.kind_box.currentData()
        parent_kind = _PARENT_KIND.get(kind)
        has_against = parent_kind is not None
        self.against_label.setVisible(has_against)
        self.against_box.setVisible(has_against)
        self.against_reload.setVisible(has_against)
        self.against_box.clear()
        self.against_box.addItem("(none)", "")
        if not has_against:
            self._against_docs = []
            return
        self._load_against()

    def _load_against(self) -> None:
        kind = self.kind_box.currentData()
        parent_kind = _PARENT_KIND.get(kind)
        if parent_kind is None:
            return
        party = self.party_edit.text().strip()

        def fetch():
            params = {"kind": parent_kind}
            if party:
                params["party"] = party
            return self._client.pack_json("orders-open", **params)

        run(fetch, self._against_loaded, self._against_failed)

    def _against_failed(self, error: object) -> None:
        self.against_box.clear()
        self.against_box.addItem("(could not load — check the party)", "")

    def _against_loaded(self, data: dict) -> None:
        self._against_docs = data.get("documents", [])
        current = self.against_box.currentData() or ""
        self.against_box.clear()
        self.against_box.addItem("(none)", "")
        for doc in self._against_docs:
            label = f"{doc.get('ref')} — {doc.get('party')} ({doc.get('status')})"
            self.against_box.addItem(label, doc.get("ref"))
        if current:
            index = self.against_box.findData(current)
            if index >= 0:
                self.against_box.setCurrentIndex(index)

    # ------------------------------------------------------------ posting
    def _submit(self) -> None:
        from uuid import uuid4

        kind = self.kind_box.currentData()
        code = dict(
            quotation="QT",
            sales_order="SO",
            delivery_note="DN",
            purchase_order="PO",
            grn="GRN",
        )[kind]
        items = self._items_payload()
        if not items:
            QMessageBox.warning(self, "Order", "Add at least one item line.")
            return
        doc: dict = {
            "kind": kind,
            "party": self.party_edit.text().strip(),
            "ref": self.ref_edit.text().strip(),
            "items": items,
        }
        if self.against_box.isVisible() and self.against_box.currentData():
            doc["against"] = self.against_box.currentData()
        if self.terms_edit.text().strip():
            doc["terms"] = self.terms_edit.text().strip()

        payload = {
            "voucher_type": code,
            "voucher_date": self.date_edit.date().toPython().isoformat(),
            "narration": f"{_KIND_LABEL[kind].split('—')[1].strip().lower()} {doc['ref']}",
            "entries": [{"ledger_name": "(order document)", "dc": "D", "amount": "0.01"}],
            "meta": {"orders": doc},
            "client_token": str(uuid4()),
        }

        def post():
            return self._client.create_voucher(
                voucher_type=payload["voucher_type"],
                voucher_date=date.fromisoformat(payload["voucher_date"]),
                entries=payload["entries"],
                narration=payload["narration"],
                client_token=payload["client_token"],
                meta=payload["meta"],
            )

        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        run(post, self._posted_ok, self._post_failed)

    def _posted_ok(self, voucher) -> None:
        # no modal here: the Orders page reloads behind the dialog and shows
        # the new document at its number — the dialog's job is done
        self.accept()

    def _post_failed(self, error: object) -> None:
        detail = getattr(error, "detail", str(error))
        QMessageBox.critical(
            self,
            "Order rejected",
            f"The engine refused the document:\n\n{detail}\n\n"
            "Nothing was posted — fix it and post again.",
        )
        self._recap()


class OrderAllocationDialog(QDialog):
    """Pick the open orders a billing voucher settles, line by line.

    One party's open sales orders (the party the voucher credits) or
    purchase orders (the party it debits), each line with its pending-to-
    bill quantity, a qty cell per line. ``settle()`` answers the
    meta.orders settle allocation list."""

    def __init__(
        self,
        client: ApiClient,
        party: str,
        side: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._client = client
        self._party = party
        self._side = side  # "sales" | "purchase"
        self._settle: list[dict] = []
        self._docs: list[dict] = []

        self.setWindowTitle(f"Orders — {party}")
        self.setModal(True)
        self.setMinimumSize(700, 460)

        layout = QVBoxLayout(self)
        verb = (
            "bills against this party's open purchase orders"
            if side == "purchase"
            else "bills against this party's open sales orders"
        )
        layout.addWidget(
            QLabel(
                f"This voucher <b>{verb}</b> — pick the order lines it "
                "settles; pending-to-bill caps every cell."
            )
        )

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Order", "Date", "Item", "Ordered", "Pending", "Bill qty"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 200)
        self.table.setColumnWidth(3, 90)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 90)
        layout.addWidget(self.table, stretch=1)

        self.summary = QLabel("Reading open orders…")
        layout.addWidget(self.summary)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        kind = "purchase_order" if self._side == "purchase" else "sales_order"

        def fetch():
            return self._client.pack_json("orders-open", kind=kind, party=self._party)

        run(fetch, self._loaded, self._failed)

    def _failed(self, error: object) -> None:
        self.summary.setText(f"Could not read open orders — {error_detail(error)}")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

    def _loaded(self, data: dict) -> None:
        self._docs = data.get("documents", [])
        rows: list[tuple[dict, dict]] = []
        for doc in self._docs:
            if doc.get("status") == "closed":
                continue
            for item in doc.get("items", []):
                rows.append((doc, item))

        self.table.setRowCount(len(rows))
        self._row_docs: list[dict] = []
        for row, (doc, item) in enumerate(rows):
            for col, value in enumerate(
                (
                    doc.get("ref", ""),
                    doc.get("date", ""),
                    item.get("item", ""),
                    item.get("qty", ""),
                    item.get("pending_bill", ""),
                )
            ):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, col, cell)
            qty_cell = QLineEdit()
            qty_cell.setPlaceholderText("0")
            qty_cell.setAlignment(Qt.AlignmentFlag.AlignRight)
            qty_cell.textChanged.connect(self._recap)
            self.table.setCellWidget(row, 5, qty_cell)
            self._row_docs.append(doc)
        self._row_items = [item for _doc, item in rows]
        self._recap()

    # ----------------------------------------------------------------- recap
    def _allocated_lines(self) -> list[tuple[dict, dict, Decimal]]:
        out: list[tuple[dict, dict, Decimal]] = []
        rows = len(getattr(self, "_row_items", []))
        for row in range(rows):
            cell = self.table.cellWidget(row, 5)
            if not isinstance(cell, QLineEdit):
                continue
            qty = to_cents(cell.text() or "0")
            if qty <= 0:
                continue
            out.append((self._row_docs[row], self._row_items[row], qty))
        return out

    def _recap(self) -> None:
        picked = self._allocated_lines()
        total = sum((qty for _d, _i, qty in picked), Decimal("0"))
        self.summary.setText(f"{len(picked)} line(s) selected · {total.normalize():f} units")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(picked))

    # ---------------------------------------------------------------- accept
    def _accept(self) -> None:
        by_order: dict[str, dict] = {}
        for doc, item, qty in self._allocated_lines():
            pending = Decimal(str(item.get("pending_bill", "0")))
            if qty > pending:
                QMessageBox.warning(
                    self,
                    "Orders",
                    f"Order {doc.get('ref')}, item {item.get('item')}: {qty:f} exceeds "
                    f"the pending to bill {pending:f}.",
                )
                return
            entry = by_order.setdefault(
                doc.get("ref", ""), {"order": doc.get("ref", ""), "lines": []}
            )
            entry["lines"].append({"item": item.get("item", ""), "qty": _plain(qty)})
        self._settle = list(by_order.values())
        self.accept()

    # ------------------------------------------------------------------ read
    def settle(self) -> list[dict]:
        return self._settle
