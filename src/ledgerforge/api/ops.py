"""Scale-out operations API (ADR-018): the admin screen's status and the
balance-cache refresh.

The partition conversion itself (``ops.partition_entries_by_year()`` /
``ops.unpartition_entries()``) is deliberately NOT an API endpoint: it is a
maintenance-window DDL operation owned by the database owner, driven from the
CLI (``ledgerforge scale-out partition``) or psql. The two endpoints here are
safe for the app role: the refresh is SECURITY DEFINER, advisory-locked and
idempotent, and the status reads catalog statistics (never a count(*) on a
hot table).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from . import AdminDep, SessionDep

router = APIRouter(prefix="/ops", tags=["ops"])


class RefreshIn(BaseModel):
    #: Refresh one book; omit for every book.
    book_id: int | None = None


@router.get("/scale-out")
def scale_out_status(session: SessionDep, _: AdminDep) -> dict:
    """The scale-out dashboard: entries shape, partition count, entry
    estimate and balance-cache state per book (ADR-018)."""
    rows = (
        session.execute(text("SELECT out_name, out_value FROM ops.scale_out_status()"))
        .mappings()
        .all()
    )
    return {"status": {r.out_name: r.out_value for r in rows}}


@router.post("/scale-out/refresh-balance-cache")
def refresh_balance_cache(payload: RefreshIn, session: SessionDep, _: AdminDep) -> dict:
    """Rebuild (incrementally) the per-book balance cache. Safe to re-run;
    safe to run concurrently (advisory-locked per book); a book without
    periods reports itself disabled — fast reads fall back to scans there."""
    rows = (
        session.execute(
            text("SELECT * FROM rpt.refresh_balance_cache(:book)"),
            {"book": payload.book_id},
        )
        .mappings()
        .all()
    )
    session.commit()
    return {
        "refreshed": [
            {
                "book_id": r.out_book_id,
                "cache_rows": r.out_ledgers,
                "horizon": r.out_horizon,
                "action": r.out_action,
            }
            for r in rows
        ]
    }
