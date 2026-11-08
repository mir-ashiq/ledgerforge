"""The GST compliance API (ADR-028, roadmap Phase 7): portal-download
import, the receipts list, and the imported lines feed.

Roles follow the codebase's rule: reads need the reader role, an import
is a bookkeeper write. Every route is book-scoped through the standard
header. The download file itself never travels: the client posts its
text (the operator's adapter already translated the portal file to the
``lf-gst-2a/1`` / ``lf-gst-2b/1`` contract), the kernel validates
all-or-nothing and imports idempotently by content fingerprint, and the
after-commit event (``gst.download.imported``) fires through the plugin
registry exactly like ``bank.statement.imported`` — isolated, never
breaking the request it describes.

The reconciliation report itself is the GST pack's registered report
(``GET /api/v1/reports/pack/gst-recon``) — one fold, three renderings
(PDF/CSV/JSON), the banking precedent; this router owns the facts, that
fold owns the meaning.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from .. import gst_compliance
from . import BookDep, BookkeeperDep, ReaderDep, SessionDep

router = APIRouter(prefix="/gst", tags=["gst"])

# Query-default singletons (ruff B008): FastAPI's own idiom, hoisted
KIND_Q = Query(default="2b", description="the portal statement kind: 2a or 2b")


def _emit(request: Request, event: str, payload: dict) -> None:
    request.app.state.plugin_manager.emit(event, payload)


class DownloadImport(BaseModel):
    text: str = Field(
        min_length=1, description="the download file's JSON text (lf-gst-2a/1 / lf-gst-2b/1)"
    )
    source: str | None = Field(
        default=None, max_length=200, description="a label for the receipt (file name…)"
    )
    dry_run: bool = Field(default=False, description="validate and preview — nothing is written")


# ------------------------------------------------------------------ imports
@router.post("/portal/import", status_code=201)
def post_download(
    payload: DownloadImport,
    request: Request,
    session: SessionDep,
    book: BookDep,
    principal: BookkeeperDep,
):
    """Import one GST portal download (GSTR-2A/2B): validated,
    all-or-nothing, idempotent by content fingerprint. ``dry_run`` is the
    preview. The receipt is the response and the ``gst.download.imported``
    payload."""
    receipt = gst_compliance.import_download(
        session,
        book=book,
        text=payload.text,
        source=payload.source,
        dry_run=payload.dry_run,
    )
    if not payload.dry_run and receipt.get("new", 0) > 0:
        _emit(request, "gst.download.imported", {"book_id": book.id, **receipt})
    return receipt


@router.get("/portal/downloads")
def get_downloads(session: SessionDep, book: BookDep, _: ReaderDep):
    """Every import receipt, newest first — the honest provenance of every
    portal-side figure the reconciliation shows."""
    return {"downloads": gst_compliance.downloads(session, book=book)}


@router.get("/portal/lines")
def get_lines(
    session: SessionDep,
    book: BookDep,
    _: ReaderDep,
    kind: Literal["2a", "2b"] = KIND_Q,
    period: str = Query(description="the return period the statement covers (YYYY-MM)"),
    gstin: str | None = Query(
        default=None, description="one registration's statement (multi-GSTIN books)"
    ),
):
    """The portal side of one comparison: the latest snapshot's rows of the
    (kind, period) statement, oldest document first. Amounts are strings —
    the client never sums (Law 3)."""
    rows = gst_compliance.portal_lines(session, book=book, kind=kind, period=period, gstin=gstin)
    return {
        "kind": kind,
        "period": period,
        "gstin": (gstin or "").upper() or None,
        "lines": [
            {
                "supplier_gstin": r.supplier_gstin,
                "party": r.party,
                "doc_type": r.doc_type,
                "doc_no": r.doc_no,
                "doc_date": r.doc_date.isoformat(),
                "taxable": str(r.taxable),
                "igst": str(r.igst),
                "cgst": str(r.cgst),
                "sgst": str(r.sgst),
                "pos": r.pos,
                "itc_available": r.itc_available,
            }
            for r in rows
        ],
    }
