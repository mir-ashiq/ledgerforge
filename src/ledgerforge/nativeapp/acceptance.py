"""The accountant's day — the scripted acceptance run (ADR-032, Phase 11).

``LedgerForge --accountants-day`` executes one complete working day against
a fresh embedded engine, through the native window's own :class:`ApiClient`
— the exact door the Qt shell uses — and asserts the *numbers* at every
step, not just the status codes. It is the release gate the roadmap
promised: a release that cannot run an accountant's day does not ship.

The day (April 2026, one book, fifty vouchers across the packs):

- **The company** — a fresh book for "Meridian Traders", masters (parties,
  a bank, a cost category with two centres, batch- and plain-tracked stock,
  a BOM, a price list), two financial years.
- **The morning** — capital in; stock purchased (batches, expiry, godown);
  a quotation → sales order → delivery note → tax invoice chain whose
  invoice opens a bill and settles the order; a counter sale for cash.
- **The afternoon** — receipts that settle the bills to the paisa (and one
  on-account); a purchase order → GRN → GST purchase invoice chain (ITC
  feeds GSTR-3B, the invoice feeds the GSTR-2B reconciliation); a cheque
  payment; rent allocated across cost centres; a bank deposit.
- **The works** — planks bought and manufactured into desks (cost rolled
  up at moving averages); stock transferred to a second store; an
  inter-state sale that exercises IGST.
- **The month end** — an attendance register; a two-employee payroll run
  (one per-day wage derived from attendance, one fixed salary; PF
  withheld and employer-side); a payout with its payslip; a bank
  statement imported and reconciled line by line (one cheque deliberately
  unpresented — the reconciliation proof must absorb it); GSTR-1, GSTR-3B
  and the GSTR-2B comparison; the counter sales and petty payments of a
  real week; the financial year closed with its sweep verified.
- **The window** — the real ``MainWindow`` constructed offscreen, shown
  the day's books: the day book carries the fifty vouchers plus the
  close's sweep, the trial balance is level.

Every step that can lie is asserted: outstanding is zero after settlement,
the trial balance's difference is exactly 0, GSTR-3B's net payable is the
computed 384.00, the reconciliation proof holds with the unpresented
cheque, the payroll nets are exact, the sweep lands in the next year.
Exit 0 with ``DAY OK``, or exit 1 naming the step that lied.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

from .app import Engine, _make_app, default_data_dir
from .client import ApiClient
from .tasks import run as run_task

# The suite never phones the network (ADR-031): the window's boot update
# check is off unless a test turns it on explicitly. The release workflow's
# gates run the real check separately.
os.environ.setdefault("LF_UPDATE_CHECKS", "0")

APR = {d: date(2026, 4, d) for d in range(1, 31)}
FY1 = ("FY 2026-27", date(2026, 4, 1), date(2027, 3, 31))
FY2 = ("FY 2027-28", date(2027, 4, 1), date(2028, 3, 31))

BANK = "Meridian Bank"
CASH = "Cash"
ACME = "Acme Retail"
BEACON = "Beacon Stores"
OMEGA = "Omega Supplies"
TIMBER = "Timber Line"

COUNTER_SALES = (470, 590, 700, 649, 812, 534, 766, 890, 1012, 1100)
PETTY = (
    ("Courier", 500),
    ("Stationery", 800),
    ("Cleaning", 300),
    ("Transport Charges", 450),
    ("Tea & Coffee", 600),
)


class DayError(Exception):
    """A step of the day did not survive its assertion."""


def _money(value: object) -> Decimal:
    return Decimal(str(value))


def _check(condition: object, message: str) -> None:
    if not condition:
        raise DayError(message)


def _eq(actual: object, expected: object, what: str) -> None:
    _check(
        _money(actual) == _money(expected),
        f"{what}: expected {expected}, the books say {actual}",
    )


# ------------------------------------------------------------------ the day


class AccountantsDay:
    """One book, fifty vouchers, every pack composing — asserted as it goes."""

    def __init__(self, client: ApiClient):
        self.client = client
        self.vouchers_posted = 0

    # ------------------------------------------------------------ plumbing
    def _voucher(self, **payload) -> dict:
        result = self.client.create_voucher(**payload)
        self.vouchers_posted += 1
        return result

    def _doc(self, *, voucher_type: str, voucher_date: date, meta: dict, narration: str) -> dict:
        """A document-mode voucher (orders/stock/payroll/gst): one placeholder
        entry — the pack derives the real lines (the API needs >= 1 entry)."""
        return self._voucher(
            voucher_type=voucher_type,
            voucher_date=voucher_date,
            narration=narration,
            entries=[{"ledger_name": CASH, "dc": "D", "amount": "0.01"}],
            meta=meta,
        )

    # ---------------------------------------------------------- phase 0
    def company(self) -> None:
        c = self.client
        # the day always runs on a fresh book: a second run over the same
        # data directory proves the migrate boot path (ADR-032's twin
        # launch discipline, like the smoke) and gets its own numbered
        # company — books have no delete, by design (Law 2's world)
        prior = sum(1 for b in c.books() if b.name.startswith("Meridian Traders"))
        name = "Meridian Traders" if not prior else f"Meridian Traders {prior + 1}"
        book = c.create_book(name, slug=f"the-day-{prior + 1}")
        c.set_book(book.id)
        c.create_period(*FY1)
        c.create_period(*FY2)

        for name, group in (
            (BANK, "Bank Accounts"),
            (ACME, "Sundry Debtors"),
            (BEACON, "Sundry Debtors"),
            (OMEGA, "Sundry Creditors"),
            (TIMBER, "Sundry Creditors"),
            ("Meridian Capital", "Capital Account"),
            ("Sales", "Sales Accounts"),
            ("Purchases", "Purchase Accounts"),
            ("Rent", "Indirect Expenses"),
            ("Electricity", "Indirect Expenses"),
            ("Depreciation", "Indirect Expenses"),
            ("Accumulated Depreciation", "Fixed Assets"),
            ("Salaries Expense", "Indirect Expenses"),
            ("Salaries Payable", "Provisions"),
            ("Production Wages", "Direct Expenses"),
            *((name, "Indirect Expenses") for name, _ in PETTY),
        ):
            c.create_ledger(name, group_name=group)

        c.create_cost_category("Location", parent_name="Primary")
        self.mumbai = c.create_cost_centre(
            "Mumbai", parent_name="Primary", category_name="Location"
        )
        self.pune = c.create_cost_centre("Pune", parent_name="Primary", category_name="Location")

        self.widget_a = c.inventory_create_item(
            {"name": "Widget A", "unit": "nos", "track_batches": True, "reorder_level": "20"}
        )
        self.widget_b = c.inventory_create_item({"name": "Widget B", "unit": "nos"})
        self.planks = c.inventory_create_item({"name": "Planks", "unit": "nos"})
        self.desks = c.inventory_create_item({"name": "Desks", "unit": "nos"})
        c.inventory_create_bom(
            {
                "name": "Desk-STD",
                "output_item_id": self.desks["id"],
                "output_qty": "2",
                "components": [{"component_item_id": self.planks["id"], "qty_per_output": "6"}],
            }
        )
        c.inventory_create_price(
            {
                "item_id": self.widget_a["id"],
                "party_ledger_id": next(
                    led.id for led in c.ledgers(search=ACME) if led.name == ACME
                ),
                "effective_from": "2026-04-01",
                "rate": "105.00",
            }
        )

    # ---------------------------------------------------------- phase 1
    def the_morning(self) -> None:
        # 1 — the owners' capital: the bank and cash the day works from
        self._voucher(
            voucher_type="OPEN",
            voucher_date=APR[1],
            narration="capital introduced",
            entries=[
                {"ledger_name": BANK, "dc": "D", "amount": "500000.00"},
                {"ledger_name": CASH, "dc": "D", "amount": "50000.00"},
                {"ledger_name": "Meridian Capital", "dc": "C", "amount": "550000.00"},
            ],
        )
        # 2-3 — stock purchased: batches with expiry, plain, into the Main godown
        self._doc(
            voucher_type="PUR",
            voucher_date=APR[1],
            narration="Widget A purchased",
            meta={
                "stock": {
                    "movement": "in",
                    "party": OMEGA,
                    "items": [
                        {
                            "item": "Widget A",
                            "qty": "100",
                            "rate": "100.00",
                            "batch": "WA-2604",
                            "expiry": "2027-09-30",
                            "godown": "Main",
                        }
                    ],
                }
            },
        )
        self._doc(
            voucher_type="PUR",
            voucher_date=APR[1],
            narration="Widget B purchased",
            meta={
                "stock": {
                    "movement": "in",
                    "party": OMEGA,
                    "items": [
                        {"item": "Widget B", "qty": "200", "rate": "25.00", "godown": "Main"}
                    ],
                }
            },
        )
        # 4-8 — the quote-to-cash chain: quotation, order, delivery, goods, invoice
        self._doc(
            voucher_type="QT",
            voucher_date=APR[2],
            narration="quotation to Acme Retail",
            meta=self._order(
                "quotation",
                ACME,
                "QT-1",
                [{"item": "Widget A", "qty": "12", "rate": "100.00", "tax_rate": "18"}],
            ),
        )
        self._doc(
            voucher_type="SO",
            voucher_date=APR[3],
            narration="Acme's order",
            meta=self._order(
                "sales_order",
                ACME,
                "SO-1",
                [{"item": "Widget A", "qty": "12", "rate": "100.00", "tax_rate": "18"}],
                against="QT-1",
            ),
        )
        self._doc(
            voucher_type="DN",
            voucher_date=APR[4],
            narration="delivered against SO-1",
            meta=self._order(
                "delivery_note", ACME, "DN-1", [{"item": "Widget A", "qty": "12"}], against="SO-1"
            ),
        )
        self._doc(  # the goods leave at cost: COGS 1,200 / stock 1,200
            voucher_type="SAL",
            voucher_date=APR[4],
            narration="goods issued to Acme",
            meta={
                "stock": {
                    "movement": "out",
                    "party": ACME,
                    "items": [
                        {
                            "item": "Widget A",
                            "qty": "12",
                            "rate": "105.00",
                            "godown": "Main",
                            "batch": "WA-2604",
                        }
                    ],
                }
            },
        )
        invoice = self._doc(  # the tax invoice: GST + bill + order settlement
            voucher_type="SAL",
            voucher_date=APR[4],
            narration="tax invoice INV-1001",
            meta={
                "gst": self._gst_sale(ACME, "29", "29", "INV-1001", "Widget A", 12, "100.00"),
                "bills": {
                    "party": ACME,
                    "refs": [{"kind": "new", "ref": "INV-1001", "dr": "1416.00", "due_days": 30}],
                },
                "orders": {
                    "settle": [{"order": "SO-1", "lines": [{"item": "Widget A", "qty": "12"}]}]
                },
            },
        )
        computed = invoice.meta["gst"]["computed"]
        _eq(computed["grand_total"], "1416.00", "INV-1001 grand total")
        # 9-10 — the second sale of the morning, Widget B
        self._doc(
            voucher_type="SAL",
            voucher_date=APR[6],
            narration="Widget B issued to Acme",
            meta={
                "stock": {
                    "movement": "out",
                    "party": ACME,
                    "items": [{"item": "Widget B", "qty": "30", "rate": "40.00", "godown": "Main"}],
                }
            },
        )
        self._doc(
            voucher_type="SAL",
            voucher_date=APR[6],
            narration="tax invoice INV-1002",
            meta={
                "gst": self._gst_sale(ACME, "29", "29", "INV-1002", "Widget B", 30, "40.00"),
                "bills": {
                    "party": ACME,
                    "refs": [{"kind": "new", "ref": "INV-1002", "dr": "1416.00", "due_days": 30}],
                },
            },
        )
        # 11 — a counter sale for cash (plain entries, no pack)
        self._voucher(
            voucher_type="SAL",
            voucher_date=APR[7],
            narration="counter sale",
            entries=[
                {"ledger_name": CASH, "dc": "D", "amount": "2360.00"},
                {"ledger_name": "Sales", "dc": "C", "amount": "2000.00"},
                {"ledger_name": "CGST Payable", "dc": "C", "amount": "180.00"},
                {"ledger_name": "SGST Payable", "dc": "C", "amount": "180.00"},
            ],
        )

    # ---------------------------------------------------------- phase 2
    def the_afternoon(self) -> None:
        # 12-14 — Acme settles: partial, then to the paisa (plus an advance)
        self._receipt(APR[8], ACME, "1000.00", [("against", "INV-1001", "1000.00")])
        self._receipt(
            APR[9],
            ACME,
            "516.00",
            [("against", "INV-1001", "416.00"), ("on_account", None, "100.00")],
        )
        self._receipt(APR[10], ACME, "1416.00", [("against", "INV-1002", "1416.00")])

        # 15-17 — the procure-to-pay chain: PO, GRN, GST purchase invoice
        self._doc(
            voucher_type="PO",
            voucher_date=APR[5],
            narration="packaging ordered",
            meta=self._order(
                "purchase_order",
                OMEGA,
                "PO-1",
                [{"item": "Packaging materials", "qty": "200", "rate": "12.00"}],
            ),
        )
        self._doc(
            voucher_type="GRN",
            voucher_date=APR[6],
            narration="goods received against PO-1",
            meta=self._order(
                "grn",
                OMEGA,
                "GRN-1",
                [{"item": "Packaging materials", "qty": "200"}],
                against="PO-1",
            ),
        )
        purchase = self._doc(  # GST + bill + order settlement, mirrored
            voucher_type="PUR",
            voucher_date=APR[6],
            narration="purchase invoice OS-4401",
            meta={
                "gst": {
                    "mode": "purchase",
                    "party": OMEGA,
                    "party_gstin": "29ABCDE1234F1Z5",
                    "invoice_no": "OS-4401",
                    "supplier_state": "29",
                    "place_of_supply": "29",
                    "items": [
                        {
                            "description": "Packaging materials",
                            "hsn": "1511",
                            "qty": 200,
                            "rate": "12.00",
                            "tax_rate": "5",
                        }
                    ],
                },
                "bills": {
                    "party": OMEGA,
                    "refs": [{"kind": "new", "ref": "OS-4401", "cr": "2520.00", "due_days": 15}],
                },
                "orders": {
                    "settle": [
                        {"order": "PO-1", "lines": [{"item": "Packaging materials", "qty": "200"}]}
                    ]
                },
            },
        )
        _eq(purchase.meta["gst"]["computed"]["grand_total"], "2520.00", "OS-4401 grand total")

        # 18 — a cheque payment (the register's PDC, deliberately unpresented)
        self._voucher(
            voucher_type="PYT",
            voucher_date=APR[11],
            narration="cheque 448812 to Omega",
            entries=[
                {"ledger_name": OMEGA, "dc": "D", "amount": "2000.00"},
                {"ledger_name": BANK, "dc": "C", "amount": "2000.00"},
            ],
            meta={
                "banking": {
                    "cheque": {"no": "448812", "bank_ledger": BANK, "matures_on": "2026-05-01"}
                },
                "bills": {
                    "party": OMEGA,
                    "refs": [{"kind": "against", "ref": "OS-4401", "dr": "2000.00"}],
                },
            },
        )
        # 19 — and the balance of OS-4401 in cash: fully settled
        self._voucher(
            voucher_type="PYT",
            voucher_date=APR[12],
            narration="balance of OS-4401",
            entries=[
                {"ledger_name": OMEGA, "dc": "D", "amount": "520.00"},
                {"ledger_name": CASH, "dc": "C", "amount": "520.00"},
            ],
            meta={
                "bills": {
                    "party": OMEGA,
                    "refs": [{"kind": "against", "ref": "OS-4401", "dr": "520.00"}],
                }
            },
        )
        # 20 — rent, allocated across the two cost centres
        self._voucher(
            voucher_type="PYT",
            voucher_date=APR[13],
            narration="April rent",
            entries=[
                {
                    "ledger_name": "Rent",
                    "dc": "D",
                    "amount": "12000.00",
                    "cost_allocations": [
                        {"centre_id": self.mumbai.id, "amount": "7000.00"},
                        {"centre_id": self.pune.id, "amount": "5000.00"},
                    ],
                },
                {"ledger_name": CASH, "dc": "C", "amount": "12000.00"},
            ],
        )
        # 21 — electricity by UPI (the statement's REF1)
        self._voucher(
            voucher_type="PYT",
            voucher_date=APR[3],
            narration="electricity",
            entries=[
                {"ledger_name": "Electricity", "dc": "D", "amount": "2500.00"},
                {"ledger_name": BANK, "dc": "C", "amount": "2500.00"},
            ],
        )
        # 22 — the week's takings banked
        self._voucher(
            voucher_type="CN",
            voucher_date=APR[9],
            narration="cash deposited",
            entries=[
                {"ledger_name": BANK, "dc": "D", "amount": "20000.00"},
                {"ledger_name": CASH, "dc": "C", "amount": "20000.00"},
            ],
        )

    # ---------------------------------------------------------- phase 3
    def the_works(self) -> None:
        # 23 — planks from a second supplier
        self._doc(
            voucher_type="PUR",
            voucher_date=APR[14],
            narration="planks purchased",
            meta={
                "stock": {
                    "movement": "in",
                    "party": TIMBER,
                    "items": [{"item": "Planks", "qty": "60", "rate": "50.00", "godown": "Main"}],
                }
            },
        )
        # 24 — six planks become two desks; wages roll into the cost
        manufacture = self._doc(
            voucher_type="JV",
            voucher_date=APR[15],
            narration="two desks manufactured",
            meta={
                "stock": {
                    "movement": "manufacture",
                    "components": [{"item": "Planks", "qty": "6"}],
                    "output": {"item": "Desks", "qty": "2", "batch": "DSK-2604"},
                    "charges": [{"ledger": "Production Wages", "amount": "450.00"}],
                    "bom": "Desk-STD",
                }
            },
        )
        computed = manufacture.meta["stock"]["computed"]
        _eq(computed["output_unit_cost"], "375.0000", "the desks' unit cost")
        # 25 — twenty Widget A move to the second store
        self._doc(
            voucher_type="JV",
            voucher_date=APR[16],
            narration="stock moved to Store 2",
            meta={
                "stock": {
                    "movement": "transfer",
                    "items": [
                        {
                            "item": "Widget A",
                            "qty": "20",
                            "to_ledger": "Widget A Store 2",
                            "godown": "Main",
                            "to_godown": "Store 2",
                        }
                    ],
                }
            },
        )
        # 26-28 — an inter-state sale to Beacon Stores (IGST), settled in full
        self._doc(
            voucher_type="SAL",
            voucher_date=APR[20],
            narration="tax invoice INV-1003 (inter-state)",
            meta={
                "gst": self._gst_sale(BEACON, "29", "33", "INV-1003", "Widget B", 10, "40.00"),
                "bills": {
                    "party": BEACON,
                    "refs": [{"kind": "new", "ref": "INV-1003", "dr": "472.00", "due_days": 15}],
                },
            },
        )
        self._doc(
            voucher_type="SAL",
            voucher_date=APR[20],
            narration="Widget B issued to Beacon",
            meta={
                "stock": {
                    "movement": "out",
                    "party": BEACON,
                    "items": [{"item": "Widget B", "qty": "10", "rate": "40.00", "godown": "Main"}],
                }
            },
        )
        self._receipt(APR[22], BEACON, "472.00", [("against", "INV-1003", "472.00")])

    # ---------------------------------------------------------- phase 4
    def the_month_end(self) -> dict:
        c = self.client
        # the attendance register, then the run it feeds
        c.attendance_import(
            "2026-04",
            [
                {"employee": "Asha Verma", "present": 22, "absent": 0, "half": 2, "leave_paid": 1},
                {"employee": "Vikram Rao", "present": 26, "absent": 0, "half": 0, "leave_paid": 0},
            ],
            source="the day's biometric export",
        )
        run = self._doc(  # 29 — the payroll run
            voucher_type="JV",
            voucher_date=APR[28],
            narration="April payroll",
            meta={
                "payroll": {
                    "mode": "run",
                    "period": "2026-04",
                    "reference": "April payroll",
                    "employees": [
                        {
                            "employee": "Asha Verma",
                            "components": [
                                {"name": "Wages", "per_day": "1500.00", "kind": "earning"},
                                {"name": "PF Payable", "amount": "1800.00", "kind": "withheld"},
                                {
                                    "name": "Employer PF",
                                    "amount": "1800.00",
                                    "kind": "employer",
                                    "liability": "PF Payable",
                                },
                            ],
                        },
                        {
                            "employee": "Vikram Rao",
                            "components": [
                                {"name": "Basic Salary", "amount": "50000.00", "kind": "earning"},
                                {"name": "PF Payable", "amount": "1800.00", "kind": "withheld"},
                                {
                                    "name": "Employer PF",
                                    "amount": "1800.00",
                                    "kind": "employer",
                                    "liability": "PF Payable",
                                },
                            ],
                        },
                    ],
                }
            },
        )
        computed = run.meta["payroll"]["computed"]
        asha, vikram = computed["employees"]
        _eq(asha["gross"], "36000.00", "Asha's gross (24 payable days x 1,500)")
        _eq(asha["net"], "34200.00", "Asha's net")
        _eq(vikram["net"], "48200.00", "Vikram's net")
        _eq(computed["total_net"], "82400.00", "the payroll's total net")

        # 30 — Asha is paid; her payslip prints
        self._doc(
            voucher_type="PYT",
            voucher_date=APR[28],
            narration="Asha's April salary",
            meta={
                "payroll": {
                    "mode": "payout",
                    "employee": "Asha Verma",
                    "amount": "34200.00",
                    "bank": BANK,
                    "reference": "NEFT-SAL-0426",
                }
            },
        )
        slip = c.voucher_pdf(run.id, employee="Asha Verma")
        _check(slip[0][:5] == b"%PDF-", "the payslip is not a PDF")

        # 31 — a reorder of Widget A lands (a second batch)
        self._doc(
            voucher_type="PUR",
            voucher_date=APR[24],
            narration="Widget A reordered",
            meta={
                "stock": {
                    "movement": "in",
                    "party": OMEGA,
                    "items": [
                        {
                            "item": "Widget A",
                            "qty": "50",
                            "rate": "100.00",
                            "batch": "WA-2605",
                            "expiry": "2028-03-31",
                            "godown": "Main",
                        }
                    ],
                }
            },
        )
        # 32-41 — the counter sales of a real week
        for i, amount in enumerate(COUNTER_SALES, start=1):
            self._voucher(
                voucher_type="SAL",
                voucher_date=APR[10 + (i % 14)],
                narration=f"counter sale {i}",
                entries=[
                    {"ledger_name": CASH, "dc": "D", "amount": f"{amount}.00"},
                    {"ledger_name": "Sales", "dc": "C", "amount": f"{amount}.00"},
                ],
            )
        # 42-46 — the petty payments of a real week
        for name, amount in PETTY:
            self._voucher(
                voucher_type="PYT",
                voucher_date=APR[17],
                narration=name.lower(),
                entries=[
                    {"ledger_name": name, "dc": "D", "amount": f"{amount}.00"},
                    {"ledger_name": CASH, "dc": "C", "amount": f"{amount}.00"},
                ],
            )
        # 47-48 — the provisions of a careful book
        self._voucher(
            voucher_type="JV",
            voucher_date=APR[27],
            narration="April salaries provided",
            entries=[
                {"ledger_name": "Salaries Expense", "dc": "D", "amount": "5000.00"},
                {"ledger_name": "Salaries Payable", "dc": "C", "amount": "5000.00"},
            ],
        )
        self._voucher(
            voucher_type="JV",
            voucher_date=APR[27],
            narration="April depreciation",
            entries=[
                {"ledger_name": "Depreciation", "dc": "D", "amount": "1250.00"},
                {"ledger_name": "Accumulated Depreciation", "dc": "C", "amount": "1250.00"},
            ],
        )
        # 49-50 — the suppliers are paid down
        self._voucher(
            voucher_type="PYT",
            voucher_date=APR[28],
            narration="on account to Omega",
            entries=[
                {"ledger_name": OMEGA, "dc": "D", "amount": "10000.00"},
                {"ledger_name": CASH, "dc": "C", "amount": "10000.00"},
            ],
        )
        self._voucher(
            voucher_type="PYT",
            voucher_date=APR[28],
            narration="Timber Line settled",
            entries=[
                {"ledger_name": TIMBER, "dc": "D", "amount": "3000.00"},
                {"ledger_name": CASH, "dc": "C", "amount": "3000.00"},
            ],
        )
        return run

    # ------------------------------------------------- phase 5: the proofs
    def the_reconciliation(self) -> None:
        c = self.client
        bank_id = next(led["id"] for led in c.bank_ledgers() if led["name"] == BANK)
        statement = (
            "Date,Narrative,Ref No,Withdrawal,Deposit\n"
            "03/04/2026,UPI/Payment to BESCOM,REF1,2500.00,\n"
            "05/04/2026,NEFT Credit from Acme Retail,REF2,,1000.00\n"
            "06/04/2026,NEFT Credit from Acme Retail,REF3,,516.00\n"
            "08/04/2026,NEFT Credit from Acme Retail,REF4,,1416.00\n"
            "09/04/2026,CASH DEPOSIT,REF6,,20000.00\n"
            "20/04/2026,NEFT Credit from Beacon Stores,REF7,,472.00\n"
            "28/04/2026,NEFT Debit Salary Asha,REF5,34200.00,\n"
        )
        receipt = c.bank_import(
            bank_id, "csv", statement, source="april.csv", opening_balance="500000.00"
        )
        _check(receipt["new"] == 7, f"the statement must import 7 lines, imported {receipt['new']}")

        # every line finds its book entry: the day's amounts are unique, so
        # each unmatched line pairs with the unmatched entry of its amount —
        # side-opposite (v1.0's convention): a deposit line reconciles to a
        # Dr-bank entry, a payment line to a Cr one
        entries = c.bank_entries(bank_id, status="unmatched")["entries"]
        lines = {ln["reference"]: ln for ln in c.bank_lines(bank_id, status="unmatched")["lines"]}
        _check(len(lines) == 7, f"7 statement lines must wait, {len(lines)} do")
        for ref, line in lines.items():
            amount = str(_money(line["amount"]))
            want_dc = "D" if line["dc"] == "C" else "C"
            candidates = [
                e for e in entries if e["dc"] == want_dc and str(_money(e["amount"])) == amount
            ]
            _check(
                len(candidates) == 1,
                f"statement line {ref} ({amount}) must have exactly one book entry, "
                f"found {len(candidates)}",
            )
            match = candidates[0]
            c.bank_match(
                bank_id,
                {
                    "statement_line_id": line["id"],
                    "entry_id": match["entry_id"],
                    "voucher_id": match["voucher_id"],
                    "voucher_date": match["voucher_date"],
                },
            )
        remaining = c.bank_lines(bank_id, status="unmatched")["lines"]
        _check(not remaining, f"lines still unmatched: {[ln['reference'] for ln in remaining]}")

        proof = c.bank_reconciliation(bank_id, APR[30], statement_balance="486704.00")
        _check(proof["proof"]["ok"], "the reconciliation proof does not hold")
        _eq(
            proof["book_balance"],
            "484704.00",
            "the day's bank ledger, asset-positive",
        )
        _eq(
            proof["reconciled_balance"],
            "-13296.00",
            "the matched April activity (500,000 opening − 13,296 net out = 486,704)",
        )
        _eq(proof["unmatched_statement_total"], "0.00", "every statement line is matched")
        _eq(
            proof["unmatched_book_total"],
            "498000.00",
            "the capital opening and the unpresented cheque",
        )

        cheques = c.pack_json("banking-cheques", as_of="2026-04-30", ledger=BANK)
        cheque = next(q for q in cheques["rows"] if q["no"] == "448812")
        _check(cheque["status"] == "issued", f"cheque 448812 must be issued, is {cheque['status']}")
        _check(cheque["pdc"] is True, "cheque 448812 is a post-dated cheque")

    def the_statements(self) -> None:
        c = self.client
        gstr1 = c.pack_json("gstr1", period="2026-04")
        _check(
            len(gstr1["invoices"]) == 3, f"GSTR-1 carries {len(gstr1['invoices'])} invoices, want 3"
        )
        by_party = {}
        for invoice in gstr1["invoices"]:
            by_party.setdefault(invoice["party"], []).append(invoice)
        _check(set(by_party) == {ACME, BEACON}, f"GSTR-1 parties: {set(by_party)}")
        acme_taxable = sum(_money(i["taxable"]) for i in by_party[ACME])
        _eq(acme_taxable, "2400.00", "Acme's taxable value in GSTR-1")
        beacon = by_party[BEACON][0]
        _eq(beacon["taxable"], "400.00", "Beacon's taxable value in GSTR-1")
        igst = sum(_money(i["igst"]) for i in gstr1["invoices"])
        _eq(igst, "72.00", "GSTR-1 IGST (the inter-state sale)")

        gstr3b = c.pack_json("gstr3b", period="2026-04")
        _eq(gstr3b["itc_available"], "120.00", "GSTR-3B ITC (OS-4401's 60+60)")
        _eq(gstr3b["net_payable"], "384.00", "GSTR-3B net payable (504 tax - 120 ITC)")

        portal = (
            '{"kind": "lf-gst-2b/1", "period": "2026-04", "gstin": "29PQRSX1234K1ZP",'
            ' "source": "the April 2B download", "entries": [{'
            '"supplier_gstin": "29ABCDE1234F1Z5", "party": "Omega Supplies", "doc_type": "INV",'
            ' "doc_no": "OS-4401", "doc_date": "2026-04-06", "taxable": "2400.00",'
            ' "cgst": "60.00", "sgst": "60.00", "igst": "0.00", "pos": "29", '
            ' "itc_available": true}]}'
        )
        c.gst_import(portal, source="GSTR-2B (April)")
        recon = c.pack_json("gst-recon", period="2026-04", kind="2b")
        _check(recon["counts"]["matched"] == 1, f"2B matched {recon['counts']['matched']}, want 1")
        _check(recon["counts"]["missing_books"] == 0, "2B found an invoice the books do not carry")
        _check(
            recon["counts"]["missing_portal"] == 0, "the books carry an invoice the portal does not"
        )

    def the_reports(self) -> None:
        c = self.client
        vouchers = c.vouchers(limit=200)
        _check(
            len(vouchers) == 50, f"the day must post exactly 50 vouchers, posted {len(vouchers)}"
        )

        tb = c.trial_balance(as_of=APR[30])
        _eq(tb["totals"]["difference"], "0.00", "the trial balance's difference")

        bills = c.pack_json("bills-receivable")
        _eq(bills["total"], "0.00", "every receivable bill is settled")
        _check(all(_money(b["outstanding"]) == 0 for b in bills["bills"]), "a bill is still open")
        acme_open = c.pack_json("bills-open", party=ACME)
        _eq(
            acme_open["on_account"],
            "-100.00",
            "Acme's advance — the 100 the books owe back (on-account credit)",
        )
        payables = c.pack_json("bills-payable")
        _check(
            all(_money(b["outstanding"]) == 0 for b in payables["bills"]),
            "a payable bill is still open",
        )
        _eq(
            payables["on_account"].get(ACME),
            "-100.00",
            "Acme's advance — owed back, so it sits on the payable side",
        )
        _eq(
            payables["total"],
            "-100.00",
            "the payable side is the advance alone — every bill settled",
        )

        sales = c.pack_json("pending-orders", side="sales")
        _check(
            sales["total_pending_value"] == "0.00" or _money(sales["total_pending_value"]) == 0,
            f"sales orders still pend: {sales['total_pending_value']}",
        )
        purchase = c.pack_json("pending-orders", side="purchase")
        _check(_money(purchase["total_pending_value"]) == 0, "purchase orders still pend")

        stock = c.pack_json("stock-position", as_of="2026-04-30")
        positions = {row["item"]: row for row in stock["items"]}
        _eq(positions["Widget A"]["qty"], 138, "Widget A position (150 in, 12 out)")
        _eq(positions["Widget B"]["qty"], 160, "Widget B position (200 in, 40 out)")
        _eq(positions["Planks"]["qty"], 54, "Planks position (60 in, 6 manufactured)")
        _eq(positions["Desks"]["qty"], 2, "the two desks")
        _check("Store 2" in positions["Widget A"]["godowns"], "the transfer never reached Store 2")

        centres = c.cost_centre_summary(APR[1], APR[30])
        by_name = {row["centre_name"]: row for row in centres["centres"]}
        _eq(by_name["Mumbai"]["dr"], "7000.00", "Mumbai's rent share")
        _eq(by_name["Pune"]["dr"], "5000.00", "Pune's rent share")

    def the_close(self) -> None:
        c = self.client
        periods = {p.name: p for p in c.periods()}
        preview = c.close_preview(periods[FY1[0]].id)
        _eq(preview["totals"]["difference"], "0.00", "the close preview's totals")
        _check(_money(preview["net_profit"]) != 0, "a month of trading must leave a profit or loss")

        c.lock_period(periods[FY1[0]].id)
        closed = c.close_period(periods[FY1[0]].id)
        _check(closed.status == "closed", f"FY 2026-27 must close, is {closed.status}")
        vouchers = c.vouchers(limit=200)
        _check(
            len(vouchers) == 51, f"the sweep must land as voucher 51, day book has {len(vouchers)}"
        )
        fyc = next(v for v in vouchers if v.voucher_type == "FYC")
        _check(fyc.voucher_date == date(2027, 4, 1), "the sweep posts into the next financial year")

        # a closed year refuses further posting — the period gate holds
        try:
            self._voucher(
                voucher_type="JV",
                voucher_date=APR[15],
                narration="one voucher too many",
                entries=[
                    {"ledger_name": CASH, "dc": "D", "amount": "1.00"},
                    {"ledger_name": "Sales", "dc": "C", "amount": "1.00"},
                ],
            )
        except Exception:
            pass
        else:
            raise DayError("posting into the closed year must be refused")

    # ----------------------------------------------------------- helpers
    def _order(self, kind: str, party: str, ref: str, items: list[dict], **extra) -> dict:
        doc = {"kind": kind, "party": party, "ref": ref, "items": items}
        doc.update(extra)
        return {"orders": doc}

    def _gst_sale(
        self, party: str, supplier_state: str, pos: str, doc_no: str, item: str, qty: int, rate: str
    ) -> dict:
        return {
            "mode": "sale",
            "party": party,
            "party_gstin": "29ABCDE1234F1Z5",
            "doc_no": doc_no,
            "supplier_state": supplier_state,
            "place_of_supply": pos,
            "supplier_gstin": "29PQRSX1234K1ZP",
            "items": [
                {"description": item, "hsn": "8517", "qty": qty, "rate": rate, "tax_rate": "18"}
            ],
        }

    def _receipt(self, day: date, party: str, amount: str, refs: list[tuple]) -> None:
        """A receipt against bills: every ref credits the party (an `against`
        settles a named bill, an `on_account` is the advance)."""
        payload_refs = []
        for kind, ref, value in refs:
            entry = {"kind": kind, "cr": value}
            if ref is not None:
                entry["ref"] = ref
            payload_refs.append(entry)
        self._voucher(
            voucher_type="RCT",
            voucher_date=day,
            narration=f"received from {party}",
            entries=[
                {"ledger_name": BANK, "dc": "D", "amount": amount},
                {"ledger_name": party, "dc": "C", "amount": amount},
            ],
            meta={"bills": {"party": party, "refs": payload_refs}},
        )


# ------------------------------------------------------------------ runner


def run_day(data_dir: Path | None = None, port: int | None = None) -> int:
    """The accountant's day, headless: exit 0 with ``DAY OK`` or exit 1."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from .. import __version__

    data = data_dir or default_data_dir()
    print(f"LedgerForge {__version__} — the accountant's day, data directory: {data}")
    engine = Engine(data, port)
    try:
        engine.start()
        client = ApiClient(engine.port)
        engine.wait_healthy()
    except Exception as exc:  # noqa: BLE001 — any boot failure fails the day
        print(f"DAY FAILED: engine boot: {exc}", flush=True)
        engine.stop()
        return 1

    day = AccountantsDay(client)
    phases = (
        ("the company", day.company),
        ("the morning", day.the_morning),
        ("the afternoon", day.the_afternoon),
        ("the works", day.the_works),
        ("the month end", day.the_month_end),
        ("the reconciliation", day.the_reconciliation),
        ("the statements", day.the_statements),
        ("the reports", day.the_reports),
        ("the close", day.the_close),
    )
    try:
        for name, phase in phases:
            phase()
            print(f"  {name}: {day.vouchers_posted} vouchers in", flush=True)
    except Exception as exc:  # noqa: BLE001 — any wrong number fails the day
        import traceback

        print(f"DAY FAILED: {exc}", flush=True)
        traceback.print_exc()
        engine.stop()
        return 1

    # the window, shown the day's books — the GUI is part of the product
    from PySide6.QtCore import QTimer

    from .main_window import MainWindow

    app = _make_app()
    window = MainWindow(client, str(data))
    window.show()
    state = {"failed": False}

    def fail(error: object) -> None:
        if state["failed"]:
            return
        state["failed"] = True
        print(f"DAY FAILED: the window: {error}", flush=True)
        engine.stop()
        app.exit(1)

    def _daybook():
        def done(vouchers):
            count = len(vouchers)
            if count != 51:
                fail(f"the window's day book must carry 51 vouchers, carries {count}")
                return
            print(f"  the window: day book {count} vouchers", flush=True)

        run_task(lambda: client.vouchers(limit=200), done, fail)

    def _tb():
        def done(data):
            totals = data.get("totals", {})
            if _money(totals.get("difference")) != 0:
                fail(f"the window's trial balance is not level: {totals}")
                return
            print(f"  the window: trial balance {totals}", flush=True)
            if not state["failed"]:
                print("DAY OK     fifty vouchers, every pack, the books level", flush=True)
                engine.stop()
                app.exit(0)

        run_task(client.trial_balance, done, fail)

    QTimer.singleShot(240_000, lambda: fail(TimeoutError("four minutes, no completion")))
    QTimer.singleShot(100, _daybook)
    QTimer.singleShot(150, _tb)
    code = app.exec()
    window.close()
    return code
