"""Masters API: groups, ledgers (the chart of accounts), currencies and FX
rates (issue #4).

Creating a master fires the documented ``master.created`` after-commit event
(the taxonomy the plugin guide has promised since the first release): groups
and ledgers carry their identity and placement; handler failures are isolated
by the registry, never breaking the master's own transaction."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import exporting
from ..engine.currency import get_currency, resolve_rate
from ..errors import NotFoundError, ValidationError
from ..models import Book, CostCategory, CostCentre, Currency, FxRate, Group, Ledger
from ..plugins import PluginManager
from ..schemas import (
    CostCategoryCreate,
    CostCategoryOut,
    CostCategoryUpdate,
    CostCentreCreate,
    CostCentreOut,
    CostCentreUpdate,
    CurrencyCreate,
    CurrencyOut,
    CurrencyUpdate,
    FxRateCreate,
    FxRateOut,
    FxRateUpdate,
    GroupCreate,
    GroupOut,
    GroupUpdate,
    LedgerCreate,
    LedgerOut,
)
from . import BookDep, BookkeeperDep, ReaderDep, SessionDep

router = APIRouter(tags=["masters"])


def _plugin_manager(request: Request) -> PluginManager:
    return request.app.state.plugin_manager


def _emit_master_created(request: Request, book: Book, kind: str, payload: dict) -> None:
    """The documented after-commit event (see docs/plugin-guide.md): fires
    once the master row is committed. Handler failures are isolated by the
    registry — a listener can never break a master's creation."""
    _plugin_manager(request).emit("master.created", {"kind": kind, "book_id": book.id, **payload})


def _get_group(
    session: Session, book: Book, *, id: int | None = None, name: str | None = None
) -> Group:
    """A group of ``book`` — another book's group id or name is a 404,
    never a cross-book parent (ADR-009)."""
    stmt = select(Group).where(Group.book_id == book.id)
    if id is not None:
        stmt = stmt.where(Group.id == id)
    elif name is not None:
        stmt = stmt.where(Group.name == name)
    else:
        raise ValidationError("provide id or name")
    group = session.scalar(stmt)
    if group is None:
        raise NotFoundError(
            f"Group {'id ' + str(id) if id else name!r} does not exist in book {book.name!r}"
        )
    return group


def _group_out(session: Session, group: Group) -> GroupOut:
    return GroupOut(
        id=group.id,
        name=group.name,
        code=group.code,
        parent_id=group.parent_id,
        nature=group.nature,
        affects_gross=group.affects_gross,
        is_root=group.is_root,
    )


@router.post("/groups", response_model=GroupOut, status_code=201)
def create_group(
    payload: GroupCreate, request: Request, session: SessionDep, book: BookDep, _: BookkeeperDep
):
    parent = _get_group(session, book, id=payload.parent_id, name=payload.parent_name)
    group = Group(
        book_id=book.id,
        name=payload.name,
        code=payload.code,
        parent_id=parent.id,
        nature=payload.nature or parent.nature,
        affects_gross=(
            payload.affects_gross if payload.affects_gross is not None else parent.affects_gross
        ),
    )
    session.add(group)
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        if "duplicate key" in str(exc).lower():
            raise ValidationError(
                f"Group name {payload.name!r} already exists in book {book.name!r}"
            ) from exc
        raise
    session.refresh(group)
    _emit_master_created(
        request,
        book,
        "group",
        {
            "id": group.id,
            "name": group.name,
            "code": group.code,
            "parent_id": group.parent_id,
            "nature": group.nature,
            "affects_gross": group.affects_gross,
        },
    )
    return _group_out(session, group)


@router.get("/groups", response_model=list[GroupOut])
def list_groups(request: Request, session: SessionDep, book: BookDep, _: ReaderDep):
    groups = session.scalars(select(Group).where(Group.book_id == book.id).order_by(Group.id)).all()
    out = [_group_out(session, g) for g in groups]
    exported = exporting.list_export(request, out, GroupOut, "groups")
    return exported if exported is not None else out


@router.patch("/groups/{group_id}", response_model=GroupOut)
def move_group(
    group_id: int,
    payload: GroupUpdate,
    session: SessionDep,
    book: BookDep,
    _: BookkeeperDep,
):
    """Re-parent / reclassify a group. The closure table follows atomically
    (trigger-maintained); cycles are rejected by the database."""
    group = session.get(Group, group_id)
    if group is None or group.book_id != book.id:
        raise NotFoundError(f"Group {group_id} does not exist in book {book.name!r}")
    if payload.parent_id is not None or payload.parent_name is not None:
        new_parent = _get_group(session, book, id=payload.parent_id, name=payload.parent_name)
        group.parent_id = new_parent.id
    if payload.nature is not None:
        group.nature = payload.nature
    if payload.affects_gross is not None:
        group.affects_gross = payload.affects_gross
    if payload.code is not None:
        group.code = payload.code
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        raise ValidationError(str(exc.orig if hasattr(exc, "orig") else exc)) from exc
    session.refresh(group)
    return _group_out(session, group)


@router.post("/ledgers", response_model=LedgerOut, status_code=201)
def create_ledger(
    payload: LedgerCreate, request: Request, session: SessionDep, book: BookDep, _: BookkeeperDep
):
    group = _get_group(session, book, id=payload.group_id, name=payload.group_name)
    ledger = Ledger(book_id=book.id, name=payload.name, code=payload.code, group_id=group.id)
    session.add(ledger)
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        if "duplicate key" in str(exc).lower():
            raise ValidationError(
                f"Ledger name {payload.name!r} already exists in book {book.name!r}"
            ) from exc
        raise
    session.refresh(ledger)
    _emit_master_created(
        request,
        book,
        "ledger",
        {
            "id": ledger.id,
            "name": ledger.name,
            "code": ledger.code,
            "group_id": group.id,
            "group_name": group.name,
        },
    )
    return LedgerOut(
        id=ledger.id,
        name=ledger.name,
        code=ledger.code,
        group_id=ledger.group_id,
        group_name=group.name,
        is_system=ledger.is_system,
    )


@router.get("/ledgers", response_model=list[LedgerOut])
def list_ledgers(
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    search: Annotated[str, Query(max_length=100)] = "",
):
    stmt = (
        select(Ledger, Group)
        .join(Group, Ledger.group_id == Group.id)
        .where(Ledger.book_id == book.id)
        .order_by(Ledger.name)
    )
    if search:
        stmt = stmt.where(Ledger.name.ilike(f"%{search}%"))
    rows = session.execute(stmt).all()
    out = [
        LedgerOut(
            id=ledger.id,
            name=ledger.name,
            code=ledger.code,
            group_id=ledger.group_id,
            group_name=group.name,
            is_system=ledger.is_system,
        )
        for ledger, group in rows
    ]
    exported = exporting.list_export(request, out, LedgerOut, "ledgers")
    return exported if exported is not None else out


# ================================================================ cost masters
def _get_cost_category(
    session: Session, book: Book, *, id: int | None = None, name: str | None = None
) -> CostCategory:
    """A cost category of ``book`` — another book's id or name is a 404
    (ADR-009's discipline, ADR-025's tables)."""
    stmt = select(CostCategory).where(CostCategory.book_id == book.id)
    if id is not None:
        stmt = stmt.where(CostCategory.id == id)
    elif name is not None:
        stmt = stmt.where(CostCategory.name == name)
    else:
        raise ValidationError("provide id or name")
    category = session.scalar(stmt)
    if category is None:
        raise NotFoundError(
            f"Cost category {'id ' + str(id) if id else name!r} does not exist "
            f"in book {book.name!r}"
        )
    return category


def _category_out(category: CostCategory) -> CostCategoryOut:
    return CostCategoryOut(
        id=category.id,
        name=category.name,
        parent_id=category.parent_id,
        is_root=category.is_root,
    )


@router.post("/cost-categories", response_model=CostCategoryOut, status_code=201)
def create_cost_category(
    payload: CostCategoryCreate,
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: BookkeeperDep,
):
    """A classification axis for cost centres (ADR-025): "Department",
    "Location", "Project"… The closure table follows by trigger."""
    parent = _get_cost_category(session, book, id=payload.parent_id, name=payload.parent_name)
    category = CostCategory(book_id=book.id, name=payload.name, parent_id=parent.id)
    session.add(category)
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        if "duplicate key" in str(exc).lower():
            raise ValidationError(
                f"Cost category name {payload.name!r} already exists in book {book.name!r}"
            ) from exc
        raise
    session.refresh(category)
    _emit_master_created(
        request,
        book,
        "cost_category",
        {"id": category.id, "name": category.name, "parent_id": category.parent_id},
    )
    return _category_out(category)


@router.get("/cost-categories", response_model=list[CostCategoryOut])
def list_cost_categories(request: Request, session: SessionDep, book: BookDep, _: ReaderDep):
    categories = session.scalars(
        select(CostCategory).where(CostCategory.book_id == book.id).order_by(CostCategory.id)
    ).all()
    out = [_category_out(c) for c in categories]
    exported = exporting.list_export(request, out, CostCategoryOut, "cost-categories")
    return exported if exported is not None else out


@router.patch("/cost-categories/{category_id}", response_model=CostCategoryOut)
def move_cost_category(
    category_id: int,
    payload: CostCategoryUpdate,
    session: SessionDep,
    book: BookDep,
    _: BookkeeperDep,
):
    """Re-parent a cost category; the closure follows atomically and the
    database rejects cycles and root moves."""
    category = session.get(CostCategory, category_id)
    if category is None or category.book_id != book.id:
        raise NotFoundError(f"Cost category {category_id} does not exist in book {book.name!r}")
    if payload.parent_id is not None or payload.parent_name is not None:
        parent = _get_cost_category(session, book, id=payload.parent_id, name=payload.parent_name)
        category.parent_id = parent.id
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        raise ValidationError(str(exc.orig if hasattr(exc, "orig") else exc)) from exc
    session.refresh(category)
    return _category_out(category)


def _get_cost_centre(
    session: Session, book: Book, *, id: int | None = None, name: str | None = None
) -> CostCentre:
    stmt = select(CostCentre).where(CostCentre.book_id == book.id)
    if id is not None:
        stmt = stmt.where(CostCentre.id == id)
    elif name is not None:
        stmt = stmt.where(CostCentre.name == name)
    else:
        raise ValidationError("provide id or name")
    centre = session.scalar(stmt)
    if centre is None:
        raise NotFoundError(
            f"Cost centre {'id ' + str(id) if id else name!r} does not exist in book {book.name!r}"
        )
    return centre


def _centre_out(session: Session, centre: CostCentre) -> CostCentreOut:
    category = session.get(CostCategory, centre.category_id) if centre.category_id else None
    return CostCentreOut(
        id=centre.id,
        name=centre.name,
        code=centre.code,
        parent_id=centre.parent_id,
        category_id=centre.category_id,
        category_name=category.name if category else None,
        is_root=centre.is_root,
    )


@router.post("/cost-centres", response_model=CostCentreOut, status_code=201)
def create_cost_centre(
    payload: CostCentreCreate,
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: BookkeeperDep,
):
    """An allocation node (ADR-025). Every real centre answers to exactly
    one cost category — TallyPrime's rule, made a schema constraint."""
    parent = _get_cost_centre(session, book, id=payload.parent_id, name=payload.parent_name)
    category = _get_cost_category(session, book, id=payload.category_id, name=payload.category_name)
    centre = CostCentre(
        book_id=book.id,
        name=payload.name,
        code=payload.code,
        parent_id=parent.id,
        category_id=category.id,
    )
    session.add(centre)
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        if "duplicate key" in str(exc).lower():
            raise ValidationError(
                f"Cost centre name {payload.name!r} already exists in book {book.name!r}"
            ) from exc
        raise
    session.refresh(centre)
    _emit_master_created(
        request,
        book,
        "cost_centre",
        {
            "id": centre.id,
            "name": centre.name,
            "code": centre.code,
            "parent_id": centre.parent_id,
            "category_id": centre.category_id,
        },
    )
    return _centre_out(session, centre)


@router.get("/cost-centres", response_model=list[CostCentreOut])
def list_cost_centres(request: Request, session: SessionDep, book: BookDep, _: ReaderDep):
    centres = session.scalars(
        select(CostCentre).where(CostCentre.book_id == book.id).order_by(CostCentre.id)
    ).all()
    out = [_centre_out(session, c) for c in centres]
    exported = exporting.list_export(request, out, CostCentreOut, "cost-centres")
    return exported if exported is not None else out


@router.patch("/cost-centres/{centre_id}", response_model=CostCentreOut)
def move_cost_centre(
    centre_id: int,
    payload: CostCentreUpdate,
    session: SessionDep,
    book: BookDep,
    _: BookkeeperDep,
):
    """Re-parent a centre or reclassify it to another category; cycles and
    root moves are rejected by the database."""
    centre = session.get(CostCentre, centre_id)
    if centre is None or centre.book_id != book.id:
        raise NotFoundError(f"Cost centre {centre_id} does not exist in book {book.name!r}")
    if payload.parent_id is not None or payload.parent_name is not None:
        parent = _get_cost_centre(session, book, id=payload.parent_id, name=payload.parent_name)
        centre.parent_id = parent.id
    if payload.category_id is not None or payload.category_name is not None:
        category = _get_cost_category(
            session, book, id=payload.category_id, name=payload.category_name
        )
        centre.category_id = category.id
    if payload.code is not None:
        centre.code = payload.code
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        raise ValidationError(str(exc.orig if hasattr(exc, "orig") else exc)) from exc
    session.refresh(centre)
    return _centre_out(session, centre)


# ==================================================================== currencies
@router.post("/currencies", response_model=CurrencyOut, status_code=201, tags=["currencies"])
def create_currency(payload: CurrencyCreate, session: SessionDep, _: BookkeeperDep):
    currency = Currency(
        code=payload.code.upper(),
        name=payload.name,
        symbol=payload.symbol,
        minor_unit=payload.minor_unit,
    )
    session.add(currency)
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        if "duplicate key" in str(exc).lower():
            raise ValidationError(f"Currency {payload.code.upper()!r} already exists") from exc
        raise
    session.refresh(currency)
    return _currency_out(currency)


@router.get("/currencies", response_model=list[CurrencyOut], tags=["currencies"])
def list_currencies(request: Request, session: SessionDep, _: ReaderDep):
    currencies = session.scalars(select(Currency).order_by(Currency.code)).all()
    out = [_currency_out(c) for c in currencies]
    exported = exporting.list_export(request, out, CurrencyOut, "currencies")
    return exported if exported is not None else out


@router.patch("/currencies/{code}", response_model=CurrencyOut, tags=["currencies"])
def update_currency(
    code: str,
    payload: CurrencyUpdate,
    session: SessionDep,
    _: BookkeeperDep,
):
    currency = session.get(Currency, code.upper())
    if currency is None:
        raise NotFoundError(f"Currency {code.upper()!r} does not exist")
    if payload.name is not None:
        currency.name = payload.name
    if payload.symbol is not None:
        currency.symbol = payload.symbol
    if payload.minor_unit is not None:
        currency.minor_unit = payload.minor_unit
    session.commit()
    session.refresh(currency)
    return _currency_out(currency)


def _currency_out(currency: Currency) -> CurrencyOut:
    return CurrencyOut(
        code=currency.code,
        name=currency.name,
        symbol=currency.symbol,
        minor_unit=currency.minor_unit,
        is_system=currency.is_system,
    )


# ==================================================================== fx rates
@router.post("/fx-rates", response_model=FxRateOut, status_code=201, tags=["fx-rates"])
def create_fx_rate(payload: FxRateCreate, session: SessionDep, book: BookDep, _: BookkeeperDep):
    if payload.from_ccy.upper() == payload.to_ccy.upper():
        raise ValidationError("fx rate pairs must differ (from_ccy != to_ccy)")
    get_currency(session, payload.from_ccy)
    get_currency(session, payload.to_ccy)
    rate = FxRate(
        book_id=book.id,
        from_ccy=payload.from_ccy.upper(),
        to_ccy=payload.to_ccy.upper(),
        rate=payload.rate,
        valid_on=payload.valid_on,
        source=payload.source,
    )
    session.add(rate)
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        message = str(getattr(exc, "orig", exc))
        if "duplicate key" in message.lower():
            raise ValidationError(
                f"Rate {payload.from_ccy.upper()}→{payload.to_ccy.upper()} on "
                f"{payload.valid_on} already exists — PATCH it to correct"
            ) from exc
        raise
    session.refresh(rate)
    return _fx_rate_out(rate)


@router.get("/fx-rates", response_model=list[FxRateOut], tags=["fx-rates"])
def list_fx_rates(
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    from_ccy: Annotated[str | None, Query(max_length=3)] = None,
    to_ccy: Annotated[str | None, Query(max_length=3)] = None,
    valid_on: date | None = None,
):
    stmt = (
        select(FxRate)
        .where(FxRate.book_id == book.id)
        .order_by(FxRate.valid_on.desc(), FxRate.id.desc())
    )
    if from_ccy:
        stmt = stmt.where(FxRate.from_ccy == from_ccy.upper())
    if to_ccy:
        stmt = stmt.where(FxRate.to_ccy == to_ccy.upper())
    if valid_on:
        stmt = stmt.where(FxRate.valid_on == valid_on)
    out = [_fx_rate_out(r) for r in session.scalars(stmt.limit(500)).all()]
    exported = exporting.list_export(request, out, FxRateOut, "fx-rates")
    return exported if exported is not None else out


@router.patch("/fx-rates/{rate_id}", response_model=FxRateOut, tags=["fx-rates"])
def update_fx_rate(
    rate_id: int,
    payload: FxRateUpdate,
    session: SessionDep,
    book: BookDep,
    _: BookkeeperDep,
):
    rate = session.get(FxRate, rate_id)
    if rate is None or rate.book_id != book.id:
        raise NotFoundError(f"FX rate {rate_id} does not exist in book {book.name!r}")
    rate.rate = payload.rate
    if "source" in payload.model_fields_set:  # absent = keep the provenance
        rate.source = payload.source
    session.commit()
    session.refresh(rate)
    return _fx_rate_out(rate)


@router.get("/fx-rates/resolve", tags=["fx-rates"])
def resolve_fx_rate(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    from_ccy: Annotated[str, Query(max_length=3)],
    to_ccy: Annotated[str, Query(max_length=3)],
    on: Annotated[date, Query()],
):
    """The rate a posting UI should pre-fill: the exact day's row, else the
    latest one on or before it. Direct pairs only — no silent inversion.
    Rates are book-local (ADR-009)."""
    rate = resolve_rate(session, book, from_ccy, to_ccy, on)
    if rate is None:
        raise NotFoundError(
            f"No {from_ccy.upper()}→{to_ccy.upper()} rate on or before {on} — "
            "POST /api/v1/fx-rates first"
        )
    return _fx_rate_out(rate)


def _fx_rate_out(rate: FxRate) -> FxRateOut:
    return FxRateOut(
        id=rate.id,
        from_ccy=rate.from_ccy,
        to_ccy=rate.to_ccy,
        rate=rate.rate,
        valid_on=rate.valid_on,
        source=rate.source,
    )
