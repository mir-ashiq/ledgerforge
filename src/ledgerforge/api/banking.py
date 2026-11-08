"""The banking API (ADR-026, roadmap Phase 5): statement import, the line
book, the matchmaker's suggestions, match/un-match, and the bank-ledger
pickers' feed.

Roles follow the codebase's rule: reads need the reader role, imports and
matches are bookkeeper writes. Every route is book-scoped through the
standard header; the ledger must be a bank account (the seeded Bank
Accounts / Bank OD A/c groups — the service guards it). After-commit
events (``bank.statement.imported``, ``bank.line.matched``,
``bank.line.unmatched``) fire through the plugin registry like
``voucher.posted`` — isolated, never breaking the request they describe.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from .. import banking, exporting
from ..banking import (
    DEFAULT_WINDOW_DAYS,
    bank_entries,
    bank_ledgers,
    bank_lines,
    import_statement,
    match_line,
    reconciliation,
    resolve_bank_ledger,
    suggestions,
    unmatch_line,
)
from . import BookDep, BookkeeperDep, ReaderDep, SessionDep

router = APIRouter(prefix="/banking", tags=["banking"])

# Query-default singletons (ruff B008): FastAPI's own idiom, hoisted
STATUS_Q = Query(default="all")
WINDOW_Q = Query(default=DEFAULT_WINDOW_DAYS, ge=0, le=366)
PER_LINE_Q = Query(default=5, ge=1, le=20)
LIMIT_Q = Query(default=50, ge=1, le=500)
AS_OF_Q = Query(description="the statement date — the proof's cut")
STMT_BAL_Q = Query(
    default=None,
    description="the closing balance on the statement (the classic anchor); "
    "omitted = fold from the earliest imported opening",
)


def _emit(request: Request, event: str, payload: dict) -> None:
    request.app.state.plugin_manager.emit(event, payload)


def _export(request, payload):
    """ADR-030's format seam for the line book (matched/voucher columns
    ride along — the reconciliation screen's spreadsheet)."""
    fmt = exporting.export_format(request)
    if fmt is None:
        return None
    columns, rows = exporting.bank_lines_projection(payload)
    return exporting.export_response(
        fmt, columns=columns, rows=rows, stem=f"bank-lines-{payload.get('ledger_name', 'ledger')}"
    )


# ------------------------------------------------------------------ the feed
@router.get("/ledgers")
def list_bank_ledgers(request: Request, session: SessionDep, book: BookDep, _: ReaderDep):
    """Every bank-account ledger of the book with its current balance —
    the pickers' feed (the same Cr-positive figure the reconciliation
    proves)."""
    ledgers = bank_ledgers(session, book)
    exported = exporting.rows_export(request, ledgers, ["id", "name", "balance"], "bank-ledgers")
    return exported if exported is not None else {"ledgers": ledgers}


# ------------------------------------------------------------------- import
class StatementImport(BaseModel):
    format: Literal["csv", "mt940"]
    text: str = Field(min_length=1, description="the statement file's text")
    source: str | None = Field(
        default=None, max_length=200, description="a label for the receipt (file name, gateway…)"
    )
    opening_balance: str | None = None
    closing_balance: str | None = None
    dry_run: bool = Field(default=False, description="validate and preview — nothing is written")


@router.post("/{ledger_id}/statements", status_code=201)
def post_statement(
    ledger_id: int,
    payload: StatementImport,
    request: Request,
    session: SessionDep,
    book: BookDep,
    principal: BookkeeperDep,
):
    """Import a bank statement (CSV or MT940): validated, all-or-nothing,
    idempotent by content fingerprint. ``dry_run`` is the preview. The
    receipt is the response and the ``bank.statement.imported`` payload."""
    ledger = resolve_bank_ledger(session, book, ledger_id)
    receipt = import_statement(
        session,
        book=book,
        ledger=ledger,
        fmt=payload.format,
        text=payload.text,
        source=payload.source,
        opening_balance=payload.opening_balance,
        closing_balance=payload.closing_balance,
        dry_run=payload.dry_run,
    )
    if not payload.dry_run and receipt.get("new", 0) > 0:
        _emit(request, "bank.statement.imported", {"book_id": book.id, **receipt})
    return receipt


# ----------------------------------------------------------------- the lines
@router.get("/{ledger_id}/lines")
def get_lines(
    ledger_id: int,
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    status: Literal["all", "unmatched", "matched"] = STATUS_Q,
):
    """The line book: every imported statement line of one bank ledger,
    oldest first, with the live (or last) match riding along. Amounts are
    strings — the client never sums (Law 3)."""
    ledger = resolve_bank_ledger(session, book, ledger_id)
    rows = bank_lines(session, book=book, ledger_id=ledger.id)
    if status == "unmatched":
        rows = [r for r in rows if r.match is None or not r.match.live]
    elif status == "matched":
        rows = [r for r in rows if r.match is not None and r.match.live]
    payload = {
        "ledger_id": ledger.id,
        "ledger_name": ledger.name,
        "status": status,
        "lines": [_line_out(row) for row in rows],
    }
    exported = _export(request, payload)
    return exported if exported is not None else payload


def _line_out(row) -> dict:
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "value_date": row.value_date.isoformat(),
        "dc": row.dc,
        "amount": row.amount,
        "reference": row.reference,
        "narrative": row.narrative,
        "cheque_no": row.cheque_no,
        "match": (
            {
                "voucher_id": row.match.voucher_id,
                "voucher_no": row.match.voucher_no,
                "voucher_date": row.match.voucher_date.isoformat(),
                "entry_id": row.match.entry_id,
                "matched_at": row.match.matched_at,
                "matched_by": row.match.matched_by,
                "unmatched_at": row.match.unmatched_at,
                "live": row.match.live,
            }
            if row.match is not None
            else None
        ),
    }


# --------------------------------------------------------- the entry book
@router.get("/{ledger_id}/entries")
def get_entries(
    ledger_id: int,
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    status: Literal["all", "unmatched", "matched"] = STATUS_Q,
):
    """The bank ledger's own entries with live-match state — the reconcile
    screen's book pane and the manual matchmaker's discovery surface: an
    entry the suggestions engine will not pair (the amounts disagree) is
    still matchable by name here."""
    ledger = resolve_bank_ledger(session, book, ledger_id)
    entries = bank_entries(session, book=book, ledger_id=ledger.id, status=status)
    payload = {
        "ledger_id": ledger.id,
        "ledger_name": ledger.name,
        "status": status,
        "entries": entries,
    }
    exported = exporting.rows_export(
        request,
        entries,
        ["voucher_no", "voucher_date", "dc", "amount", "narration", "cheque_no"],
        f"bank-entries-{ledger.name}",
    )
    return exported if exported is not None else payload


# -------------------------------------------------------------- suggestions
@router.get("/{ledger_id}/suggestions")
def get_suggestions(
    ledger_id: int,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    window: int = WINDOW_Q,
    per_line: int = PER_LINE_Q,
    limit: int = LIMIT_Q,
):
    """The matchmaker's candidates — amount-exact, date-windowed,
    reference-fuzzy, ranked. Computed on request, written nowhere: an
    explicit match is the only commit (the roadmap's law, structural)."""
    ledger = resolve_bank_ledger(session, book, ledger_id)
    return suggestions(
        session, book=book, ledger=ledger, window_days=window, per_line=per_line, limit=limit
    )


# ------------------------------------------------------------ match/unmatch
class MatchCreate(BaseModel):
    statement_line_id: int
    entry_id: int = Field(description="the bank ledger's own voucher entry")
    voucher_id: int
    voucher_date: date = Field(description="the carrying voucher's date — the match names it")


@router.post("/{ledger_id}/matches", status_code=201)
def post_match(
    ledger_id: int,
    payload: MatchCreate,
    request: Request,
    session: SessionDep,
    book: BookDep,
    principal: BookkeeperDep,
):
    """Record the reconciliation judgement: this statement line is that
    book entry. One live match per line (409 otherwise — un-match first).
    The database re-proves the entry, its voucher, its date, its book and
    its ledger."""
    ledger = resolve_bank_ledger(session, book, ledger_id)
    result = match_line(
        session,
        book=book,
        ledger=ledger,
        statement_line_id=payload.statement_line_id,
        entry_id=payload.entry_id,
        voucher_id=payload.voucher_id,
        voucher_date=payload.voucher_date,
        matched_by=principal.sub,
    )
    _emit(request, "bank.line.matched", {"book_id": book.id, **result})
    return result


@router.delete("/matches/{statement_line_id}")
def delete_match(
    statement_line_id: int,
    request: Request,
    session: SessionDep,
    book: BookDep,
    principal: BookkeeperDep,
):
    """Withdraw a match — the row stays (never deleted), ``unmatched_at``
    records when the judgement was withdrawn."""
    # the ledger guard runs inside; the line book is ledger-scoped already
    line = session.get(banking.BankStatementLine, statement_line_id)
    if line is None or line.book_id != book.id:
        from ..errors import NotFoundError

        raise NotFoundError(f"Statement line {statement_line_id} does not exist")
    ledger = resolve_bank_ledger(session, book, line.ledger_id)
    result = unmatch_line(session, book=book, ledger=ledger, statement_line_id=statement_line_id)
    _emit(request, "bank.line.unmatched", {"book_id": book.id, **result})
    return result


# --------------------------------------------------------- the reconciliation
@router.get("/{ledger_id}/reconciliation")
def get_reconciliation(
    ledger_id: int,
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    as_of: date = AS_OF_Q,
    statement_balance: str | None = STMT_BAL_Q,
):
    """Book vs bank, as-of, with the reconciled-balance proof (ADR-026):
    book − statement = unmatched-book effects − unmatched-statement effects
    + matched-pair differences, exact Decimals, asserted."""
    ledger = resolve_bank_ledger(session, book, ledger_id)
    payload = reconciliation(
        session, book=book, ledger=ledger, as_of=as_of, statement_balance=statement_balance
    )
    exported = exporting.rows_export(
        request,
        payload.get("rows") or [],
        ["kind", "label", "date", "amount", "effect", "reference", "narration"],
        f"reconciliation-{ledger.name}",
    )
    return exported if exported is not None else payload
