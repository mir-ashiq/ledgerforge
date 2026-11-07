"""Vouchers API: posting, retrieval, reversal, opening balances."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engine.opening import post_opening
from ..engine.posting import get_voucher_with_entries, post_voucher
from ..engine.reversal import reverse_voucher
from ..models import Voucher, VoucherEntry, VoucherType
from ..plugins import PluginManager
from ..schemas import (
    OpeningCreate,
    ReversalCreate,
    VoucherCreate,
    VoucherLineOut,
    VoucherOut,
)
from ..settings import get_settings
from . import BookDep, BookkeeperDep, ReaderDep, SessionDep

router = APIRouter(tags=["vouchers"])


def _plugin_manager(request: Request) -> PluginManager:
    return request.app.state.plugin_manager


def _voucher_out(session: Session, voucher: Voucher, *, replayed: bool = False) -> VoucherOut:
    entries = session.scalars(
        select(VoucherEntry)
        .where(VoucherEntry.voucher_id == voucher.id)
        .order_by(VoucherEntry.line_no)
    ).all()
    from ..models import Ledger  # local import to keep module imports flat

    ledger_rows = session.scalars(
        select(Ledger).where(Ledger.id.in_({e.ledger_id for e in entries}))
    ).all()
    ledger_names = {row.id: row.name for row in ledger_rows}
    vtype = session.get(VoucherType, voucher.voucher_type_id)
    # the daybook's amount column (ADR-015): the debit-side total, engine-
    # computed and quantized to cents — the client never sums (Law 3)
    debit_total = sum((e.amount for e in entries if e.dc == "D"), Decimal("0"))
    return VoucherOut(
        id=voucher.id,
        uuid=voucher.uuid,
        voucher_no=voucher.voucher_no,
        voucher_type=vtype.code if vtype else "",
        voucher_date=voucher.voucher_date,
        amount=debit_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        narration=voucher.narration,
        reversal_of=voucher.reversal_of,
        posted_at=voucher.posted_at,
        client_token=voucher.client_token,
        meta=voucher.meta or {},
        replayed=replayed,
        entries=[
            VoucherLineOut(
                line_no=e.line_no,
                ledger_id=e.ledger_id,
                ledger_name=ledger_names.get(e.ledger_id, ""),
                dc=e.dc,
                amount=e.amount,
                narration=e.narration,
                currency=e.currency,
                amount_fx=e.amount_fx,
                fx_rate=e.fx_rate,
            )
            for e in entries
        ],
    )


@router.post("/vouchers")
def create_voucher(
    payload: VoucherCreate,
    request: Request,
    response: Response,
    session: SessionDep,
    book: BookDep,
    _: BookkeeperDep,
):
    voucher, created = post_voucher(
        session, payload, _plugin_manager(request), get_settings(), book
    )
    response.status_code = 201 if created else 200
    return _voucher_out(session, voucher, replayed=not created)


@router.get("/vouchers/{voucher_id}", response_model=VoucherOut)
def get_voucher(voucher_id: int, session: SessionDep, book: BookDep, _: ReaderDep):
    voucher, _entries = get_voucher_with_entries(session, voucher_id)
    if voucher.book_id != book.id:
        from ..errors import NotFoundError

        raise NotFoundError(f"Voucher {voucher_id} does not exist in book {book.name!r}")
    return _voucher_out(session, voucher)


@router.get("/vouchers/{voucher_id}/pdf")
def voucher_pdf(
    voucher_id: int,
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    employee: Annotated[str, Query()] = "",
):
    """Any posted voucher as a PDF (ADR-013, the print bridge).

    The voucher's meta keys are offered to the plugin registry's
    document-view factories — a payroll run prints as the payslip of
    ``?employee=``, a sales invoice as the tax invoice — and anything
    unclaimed (plain vouchers, reversals, payouts) prints as the voucher
    slip. Reader role: printing reads, never writes.
    """
    from ..errors import NotFoundError
    from ..reporting.printing import render_voucher_pdf

    voucher, entries = get_voucher_with_entries(session, voucher_id)
    if voucher.book_id != book.id:
        raise NotFoundError(f"Voucher {voucher_id} does not exist in book {book.name!r}")
    pdf, filename = render_voucher_pdf(
        _plugin_manager(request), session, book, voucher, entries, {"employee": employee}
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/vouchers", response_model=list[VoucherOut])
def list_vouchers(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    type_code: Annotated[str, Query(alias="type")] = "",
    date_from: date | None = None,
    date_to: date | None = None,
    limit: Annotated[int, Query()] = 50,
):
    stmt = (
        select(Voucher).where(Voucher.book_id == book.id).order_by(Voucher.id.desc()).limit(limit)
    )
    if type_code:
        stmt = stmt.join(VoucherType, Voucher.voucher_type_id == VoucherType.id).where(
            VoucherType.code == type_code.upper()
        )
    if date_from:
        stmt = stmt.where(Voucher.voucher_date >= date_from)
    if date_to:
        stmt = stmt.where(Voucher.voucher_date <= date_to)
    vouchers = session.scalars(stmt).all()
    return [_voucher_out(session, v) for v in vouchers]


@router.post("/vouchers/{voucher_id}/reverse")
def reverse(
    voucher_id: int,
    request: Request,
    response: Response,
    session: SessionDep,
    book: BookDep,
    _: BookkeeperDep,
    payload: ReversalCreate | None = None,
):
    payload = payload or ReversalCreate()
    voucher, created = reverse_voucher(
        session,
        voucher_id,
        _plugin_manager(request),
        get_settings(),
        book,
        reversal_date=payload.reversal_date,
        narration=payload.narration,
        client_token=payload.client_token,
    )
    response.status_code = 201 if created else 200
    return _voucher_out(session, voucher, replayed=not created)


@router.post("/opening-balances")
def opening_balances(
    payload: OpeningCreate,
    request: Request,
    response: Response,
    session: SessionDep,
    book: BookDep,
    _: BookkeeperDep,
):
    voucher, created = post_opening(
        session, payload, _plugin_manager(request), get_settings(), book
    )
    response.status_code = 201 if created else 200
    return _voucher_out(session, voucher, replayed=not created)
