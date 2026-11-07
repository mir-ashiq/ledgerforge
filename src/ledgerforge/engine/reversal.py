"""Reversal vouchers — the only sanctioned correction mechanism (Law 2).

A reversal inserts a NEW voucher mirroring the original (debit/credit
swapped, same amounts), linked back via ``reversal_of``. The original is
never modified: the database forbids it by trigger and by privilege.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import Book, CostAllocation, Voucher, VoucherType
from ..plugins import PluginManager
from ..schemas import CostAllocationIn, VoucherCreate, VoucherLineIn
from ..settings import Settings
from .posting import get_voucher_with_entries, post_voucher


def reverse_voucher(
    session: Session,
    voucher_id: int,
    manager: PluginManager,
    settings: Settings,
    book: Book,
    *,
    reversal_date: date | None = None,
    narration: str | None = None,
    client_token=None,
) -> tuple[Voucher, bool]:
    """Reverse a voucher of ``book``. A voucher id from another book is a
    404, never a cross-book correction (ADR-009)."""
    original, entries = get_voucher_with_entries(session, voucher_id)
    if original.book_id != book.id:
        raise NotFoundError(f"Voucher {voucher_id} does not exist in book {book.name!r}")

    if original.reversal_of is not None:
        raise ValidationError(
            "A reversal voucher cannot itself be reversed; post a fresh correcting voucher instead"
        )
    existing = session.scalar(
        select(Voucher).where(Voucher.reversal_of == original.id, Voucher.book_id == book.id)
    )
    if existing is not None:
        raise ConflictError(
            f"Voucher {original.voucher_no} is already reversed by {existing.voucher_no}"
        )
    if not entries:
        raise ValidationError("Cannot reverse a voucher with no entries")

    vtype = session.get(VoucherType, original.voucher_type_id)
    assert vtype is not None

    # The original's cost allocations mirror onto the reversal (ADR-025):
    # same centres, same amounts — the flipped Dr/Cr rides the flipped
    # line, so every centre's net walks back to exactly zero.
    allocations_by_entry: dict[int, list[CostAllocation]] = {}
    for alloc in session.scalars(
        select(CostAllocation).where(CostAllocation.entry_id.in_({e.id for e in entries}))
    ):
        allocations_by_entry.setdefault(alloc.entry_id, []).append(alloc)

    payload = VoucherCreate(
        voucher_type=vtype.code,
        voucher_date=reversal_date or date.today(),
        narration=narration or f"Reversal of voucher {original.voucher_no}",
        client_token=client_token,
        entries=[
            VoucherLineIn(
                ledger_id=e.ledger_id,
                dc="C" if e.dc == "D" else "D",  # mirror
                amount=e.amount,
                narration=e.narration,
                # FX snapshot carries over verbatim: the reversal cancels the
                # original at the very rate it was posted (ADR-007).
                currency=e.currency,
                amount_fx=e.amount_fx,
                fx_rate=e.fx_rate,
                cost_allocations=[
                    CostAllocationIn(centre_id=a.centre_id, amount=a.amount)
                    for a in allocations_by_entry.get(e.id, [])
                ],
            )
            for e in entries
        ],
        reversal_of=original.id,
    )
    return post_voucher(session, payload, manager, settings, book)
