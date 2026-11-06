"""Opening balances.

Opening balances are posted as a balanced Opening Journal (type ``OPEN``) —
Law 1 applies to openings exactly as it applies to every other transaction.
A client-side wizard can synthesize the balancing line against a suitable
ledger (e.g. an equity or suspense ledger); the engine itself never fakes
balance.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Book, Voucher
from ..plugins import PluginManager
from ..schemas import OpeningCreate, VoucherCreate
from ..settings import Settings
from .posting import post_voucher


def post_opening(
    session: Session,
    payload: OpeningCreate,
    manager: PluginManager,
    settings: Settings,
    book: Book,
) -> tuple[Voucher, bool]:  # noqa: F821 — Voucher from .posting
    voucher_payload = VoucherCreate(
        voucher_type="OPEN",
        voucher_date=payload.voucher_date,
        narration=payload.narration or "Opening balances",
        client_token=payload.client_token,
        meta=payload.meta,
        entries=payload.entries,
    )
    return post_voucher(session, voucher_payload, manager, settings, book)
