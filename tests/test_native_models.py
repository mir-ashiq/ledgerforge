"""The native app's boundary types (ADR-022): JSON in, Decimal out, the
client never sums, and money text is plain and locale-free."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from ledgerforge.nativeapp.models import (
    Book,
    Group,
    Ledger,
    Period,
    Slot,
    TrialBalanceRow,
    Voucher,
    VoucherLine,
    money,
)

IST = timezone(timedelta(hours=5, minutes=30))

VOUCHER_JSON = {
    "id": 7,
    "uuid": "6f9d2f6e-2b7f-4f9e-8dbb-3f1c9a2e4d10",
    "voucher_no": "SAL-1",
    "voucher_type": "SAL",
    "voucher_date": "2026-10-31",
    "amount": "11800.00",
    "narration": "desk sale",
    "reversal_of": None,
    "posted_at": "2026-10-31T09:15:00+05:30",
    "client_token": "00000000-0000-0000-0000-000000000001",
    "meta": {},
    "entries": [
        {
            "line_no": 1,
            "ledger_id": 3,
            "ledger_name": "Sundry Debtors",
            "dc": "D",
            "amount": "11800.00",
            "narration": None,
        },
        {
            "line_no": 2,
            "ledger_id": 4,
            "ledger_name": "Sales Account",
            "dc": "C",
            "amount": "10000.00",
            "narration": None,
        },
        {
            "line_no": 3,
            "ledger_id": 5,
            "ledger_name": "Output CGST",
            "dc": "C",
            "amount": "900.00",
            "narration": None,
        },
    ],
}


def test_voucher_parses_with_exact_decimals():
    voucher = Voucher.from_json(VOUCHER_JSON)
    assert voucher.voucher_no == "SAL-1"
    assert voucher.voucher_date == date(2026, 10, 31)
    assert voucher.posted_at == datetime(2026, 10, 31, 9, 15, tzinfo=IST)
    assert isinstance(voucher.amount, Decimal)
    assert voucher.amount == Decimal("11800.00")
    assert [e.dc for e in voucher.entries] == ["D", "C", "C"]
    assert [e.amount for e in voucher.entries] == [
        Decimal("11800.00"),
        Decimal("10000.00"),
        Decimal("900.00"),
    ]
    assert voucher.is_reversal is False


def test_voucher_line_parses_fx_when_present():
    line = VoucherLine.from_json(
        {
            "line_no": 2,
            "ledger_id": 9,
            "ledger_name": "USD Bank",
            "dc": "D",
            "amount": "82500.00",
            "narration": None,
            "currency": "USD",
            "amount_fx": "1000.00",
            "fx_rate": "82.50",
        }
    )
    assert line.currency == "USD"
    assert line.amount_fx == Decimal("1000.00")
    assert line.fx_rate == Decimal("82.50")


def test_ledger_group_book_period_slot_parse():
    ledger = Ledger.from_json(
        {
            "id": 1,
            "name": "Cash",
            "code": None,
            "group_id": 11,
            "group_name": "Cash-in-Hand",
            "is_system": True,
        }
    )
    assert ledger.is_system and ledger.group_name == "Cash-in-Hand"

    group = Group.from_json(
        {
            "id": 11,
            "name": "Cash-in-Hand",
            "code": None,
            "parent_id": 2,
            "nature": "asset",
            "affects_gross": False,
            "is_root": False,
        }
    )
    assert group.nature == "asset"

    book = Book.from_json(
        {
            "id": 1,
            "name": "Main Book",
            "slug": "default",
            "base_currency": "INR",
            "fy_start_month": 4,
            "is_archived": False,
            "created_at": "2026-01-01T00:00:00",
        }
    )
    assert book.fy_start_month == 4 and book.base_currency == "INR"

    period = Period.from_json(
        {
            "id": 1,
            "name": "FY 2026-27",
            "starts_on": "2026-04-01",
            "ends_on": "2027-03-31",
            "status": "open",
            "locked_at": None,
            "closed_at": None,
            "closed_by": None,
        }
    )
    assert period.status == "open" and period.starts_on == date(2026, 4, 1)

    slot = Slot.from_json(
        {
            "key": "gst.dashboard",
            "title": "GST",
            "plugin": "gst",
            "source": "entrypoint",
            "trust": "signed",
            "url": None,
        }
    )
    assert slot.url is None and slot.trust == "signed"


def test_trial_balance_row_and_money_text():
    row = TrialBalanceRow.from_json(
        {
            "ledger_name": "Cash",
            "group_name": "Cash-in-Hand",
            "nature": "asset",
            "dr": "500.00",
            "cr": "0.00",
            "balance": "500.00",
            "dc": "D",
        }
    )
    assert row.dr == Decimal("500.00") and row.dc == "D"
    assert money(row.balance) == "500.00"
    assert money(Decimal("1234567.5")) == "1,234,567.50"
    assert money("0") == "0.00"  # strings parse through Decimal, never float
