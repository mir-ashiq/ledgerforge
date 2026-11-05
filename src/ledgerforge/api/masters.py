"""Masters API: groups and ledgers (the chart of accounts)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import NotFoundError, ValidationError
from ..models import Group, Ledger
from ..schemas import GroupCreate, GroupOut, GroupUpdate, LedgerCreate, LedgerOut
from . import SessionDep

router = APIRouter(tags=["masters"])


def _get_group(session: Session, *, id: int | None = None, name: str | None = None) -> Group:
    stmt = select(Group)
    if id is not None:
        stmt = stmt.where(Group.id == id)
    elif name is not None:
        stmt = stmt.where(Group.name == name)
    else:
        raise ValidationError("provide id or name")
    group = session.scalar(stmt)
    if group is None:
        raise NotFoundError(f"Group {'id ' + str(id) if id else name!r} does not exist")
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
def create_group(payload: GroupCreate, session: SessionDep):
    parent = _get_group(session, id=payload.parent_id, name=payload.parent_name)
    group = Group(
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
            raise ValidationError(f"Group name {payload.name!r} already exists") from exc
        raise
    session.refresh(group)
    return _group_out(session, group)


@router.get("/groups", response_model=list[GroupOut])
def list_groups(session: SessionDep):
    groups = session.scalars(select(Group).order_by(Group.id)).all()
    return [_group_out(session, g) for g in groups]


@router.patch("/groups/{group_id}", response_model=GroupOut)
def move_group(
    group_id: int,
    payload: GroupUpdate,
    session: SessionDep,
):
    """Re-parent / reclassify a group. The closure table follows atomically
    (trigger-maintained); cycles are rejected by the database."""
    group = session.get(Group, group_id)
    if group is None:
        raise NotFoundError(f"Group {group_id} does not exist")
    if payload.parent_id is not None or payload.parent_name is not None:
        new_parent = _get_group(session, id=payload.parent_id, name=payload.parent_name)
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
def create_ledger(payload: LedgerCreate, session: SessionDep):
    group = _get_group(session, id=payload.group_id, name=payload.group_name)
    ledger = Ledger(name=payload.name, code=payload.code, group_id=group.id)
    session.add(ledger)
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        if "duplicate key" in str(exc).lower():
            raise ValidationError(f"Ledger name {payload.name!r} already exists") from exc
        raise
    session.refresh(ledger)
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
    session: SessionDep,
    search: Annotated[str, Query(max_length=100)] = "",
):
    stmt = select(Ledger, Group).join(Group, Ledger.group_id == Group.id).order_by(Ledger.name)
    if search:
        stmt = stmt.where(Ledger.name.ilike(f"%{search}%"))
    rows = session.execute(stmt).all()
    return [
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
