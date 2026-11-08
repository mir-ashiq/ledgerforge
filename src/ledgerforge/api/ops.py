"""Scale-out operations API (ADR-018): the admin screen's status and the
balance-cache refresh — plus the analyst seat (ADR-030): minting and
revoking the ODBC login.

The partition conversion itself (``ops.partition_entries_by_year()`` /
``ops.unpartition_entries()``) is deliberately NOT an API endpoint: it is
a maintenance-window DDL operation owned by the database owner, driven from the
CLI (``ledgerforge scale-out partition``) or psql. The two scale-out
endpoints here are safe for the app role: the refresh is SECURITY
DEFINER, advisory-locked and idempotent, and the status reads catalog
statistics (never a count(*) on a hot table).

The analyst seat (ADR-030 §5) is the desktop's ODBC answer:
``POST /ops/analyst-access`` mints or rotates the ``lf_analyst`` LOGIN
role with a server-generated password (returned once — PostgreSQL itself
stores its SCRAM verifier; the app never sees it again), grants it
``ledgerforge_readonly`` (SELECT on the ``views`` schema, nothing else),
and answers with the connection parameters the ODBC dialog wants.
``DELETE`` drops the seat. One seat by design — per-analyst seats are the
DB owner's own ``GRANT`` away, not this phase's UI. On server
deployments the app role may lack CREATEROLE; the failure explains
itself (the database owner mints the seat or grants the privilege).
"""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from ..errors import ValidationError
from . import AdminDep, SessionDep

router = APIRouter(prefix="/ops", tags=["ops"])

ANALYST_ROLE = "lf_analyst"


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


# ------------------------------------------------------------ analyst access
def _connection_info() -> dict:
    """The ODBC dialog's parameters, parsed from the engine's own DSN —
    honest about what the deployment actually is."""
    from ..settings import get_settings

    parts = urlsplit(get_settings().database_url)
    host = parts.hostname or ""
    port = parts.port or 5432
    dbname = (parts.path or "/postgres").lstrip("/") or "postgres"
    if not host or host.startswith("/"):
        return {
            "host": host or "(unix socket)",
            "port": port,
            "database": dbname,
            "note": (
                "the engine's database is a unix-socket cluster — psqlODBC needs "
                "TCP. On the desktop this is the embedded cluster (Windows builds "
                "already speak TCP); point LF_DATABASE_URL at a TCP listener to "
                "enable ODBC elsewhere."
            ),
        }
    return {"host": host, "port": port, "database": dbname}


@router.post("/analyst-access")
def mint_analyst(session: SessionDep, _: AdminDep) -> dict:
    """Mint or rotate the analyst seat: the ``lf_analyst`` LOGIN role with
    a fresh server-generated password (shown exactly once), granted
    ``ledgerforge_readonly`` — SELECT on the ``views`` schema and nothing
    else (ADR-030). Idempotent: an existing seat is rotated, not duplicated."""
    # PostgreSQL takes no bind parameters in utility statements (CREATE
    # ROLE) — the password is inlined. It is server-generated urlsafe
    # (no quotes possible) and the assert keeps that a law, not a hope.
    password = secrets.token_urlsafe(24)
    assert "'" not in password, "the generated password must be literal-safe"
    try:
        exists = session.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": ANALYST_ROLE}
        ).scalar()
        if exists:
            session.execute(text(f"ALTER ROLE {ANALYST_ROLE} LOGIN PASSWORD '{password}'"))
            action = "rotated"
        else:
            session.execute(text(f"CREATE ROLE {ANALYST_ROLE} LOGIN PASSWORD '{password}'"))
            action = "minted"
        session.execute(text(f"GRANT ledgerforge_readonly TO {ANALYST_ROLE}"))
        session.commit()
    except Exception as exc:  # noqa: BLE001 — the privilege story varies by deployment
        session.rollback()
        raise ValidationError(
            "ops: could not mint the analyst seat — this deployment's database user "
            "lacks the privilege (the database owner mints it, or grants CREATEROLE "
            f"to the app role). PostgreSQL said: {exc}"
        ) from exc
    return {
        "role": ANALYST_ROLE,
        "action": action,
        "password": password,
        "note": "shown exactly once — PostgreSQL keeps only its SCRAM verifier; "
        "rotation is the only recovery",
        "connection": _connection_info(),
        "sees": "the views schema (books, groups, ledgers, vouchers, voucher_lines, "
        "ledger_monthly, ledger_balances) — every book, read-only",
    }


@router.delete("/analyst-access")
def revoke_analyst(session: SessionDep, _: AdminDep) -> dict:
    """Drop the analyst seat — the views and the readonly role stay (they
    are the schema's own objects); the login dies."""
    try:
        exists = session.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": ANALYST_ROLE}
        ).scalar()
        if not exists:
            return {"role": ANALYST_ROLE, "action": "absent"}
        session.execute(text(f"REVOKE ledgerforge_readonly FROM {ANALYST_ROLE}"))
        session.execute(text(f"DROP ROLE {ANALYST_ROLE}"))
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise ValidationError(
            "ops: could not drop the analyst seat — an open session or a granted "
            f"membership holds it. PostgreSQL said: {exc}"
        ) from exc
    return {"role": ANALYST_ROLE, "action": "dropped"}
