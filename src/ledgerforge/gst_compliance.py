"""The GST compliance kernel seam (ADR-028, roadmap Phase 7): portal
download facts — storage, idempotent import and the line fold the GST
pack's reconciliation reads.

The ADR-026 argument restated for the tax portal: a downloaded GSTR-2A/2B
statement's supplier rows are **external facts** the books must remember
exactly — supplier GSTIN, document type/number/date, taxable value, the
IGST/CGST/SGST split, the portal's own ITC-eligibility verdict. They need
database-enforced identity (re-downloading the same period is a no-op;
two suppliers' look-alike documents both survive), exact aggregation on
the reconciliation's portal side, and book scoping (ADR-009).
``vouchers.meta`` cannot promise any of that — a download is not
double-entry and Law 1 forbids inventing a voucher to carry it — so
migration ``0010_gst_2b`` lands the banking pack's exact shape: a batch
(the import receipt) and lines (the facts), INSERT-only and
trigger-guarded append-only.

The kernel's boundary is the same one banking drew: **storage and fold,
never interpretation and never connectivity**. Interpreting a supplier
row's tax meaning is the GST pack's fold (served through the versioned
plugin API 0.13 — ``Resolver.gst_portal_lines``); talking to the GST
portal is the operator's adapter, out-of-process by contract (D3, the
IRN precedent). The download format is therefore an **adapter contract**
(``lf-gst-2b/1``), not the portal's own schema — the operator's tooling
translates, exactly as the IRP adapter translates the outbox envelope.

Revisions are honest, not edits: the portal regenerates a period's 2B
statement as suppliers file, so a re-download with changed amounts lands
beside the earlier rows (a different content fingerprint — append-only,
Law 2) and ``portal_lines`` prefers the latest snapshot per document key.
The books' side of the comparison never changes underneath anyone; the
portal's side moves exactly when the portal moved.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import ValidationError
from .models import Book, GstDownload, GstDownloadLine

logger = logging.getLogger("ledgerforge.gst_compliance")

#: The download kinds a statement may carry — the portal's two inward
#: statements: 2A (auto-drafted from suppliers' GSTR-1) and 2B (the static,
#: ITC-eligible-as-stated input the monthly 3B relies on).
KINDS = ("2a", "2b")

#: The adapter contract's kind tag (one per download file).
DOWNLOAD_KINDS = {"lf-gst-2a/1": "2a", "lf-gst-2b/1": "2b"}

DOC_TYPES = ("INV", "CN", "DN")

_PERIOD_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")

CENT = Decimal("0.01")


def valid_gstin(value: object) -> bool:
    """The pack's own format rule, kept in step with it: 15 characters,
    state code + PAN + entity + Z + checksum — format-checked, checksums
    unverified (the honest footnote every GST statement carries)."""
    return bool(_GSTIN_RE.match(str(value or "").strip()))


@dataclass(frozen=True)
class PortalEntry:
    """One supplier document as the portal states it — the parsed shape
    before identity is assigned."""

    supplier_gstin: str
    party: str | None
    doc_type: str
    doc_no: str
    doc_date: date
    taxable: Decimal
    igst: Decimal
    cgst: Decimal
    sgst: Decimal
    pos: str | None
    itc_available: bool


@dataclass(frozen=True)
class PortalLine:
    """One portal row as the pack's fold sees it (plugin API 0.13): the
    stored fact with its provenance, latest-snapshot-per-document-key."""

    supplier_gstin: str
    party: str
    doc_type: str
    doc_no: str
    doc_date: date
    taxable: Decimal
    igst: Decimal
    cgst: Decimal
    sgst: Decimal
    pos: str
    itc_available: bool


@dataclass(frozen=True)
class ParsedDownload:
    """One download file's full content: the statement's identity plus its
    supplier rows, or the rejects that name why not."""

    kind: str
    period: str
    gstin: str
    source: str | None
    entries: tuple[PortalEntry, ...]
    rejects: tuple[tuple[int, str], ...] = ()  # (entry index, reason)


def _decimal(value: object, where: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(f"gst: {where} must be a number, got {value!r}") from None


def _parse_date(raw: object, where: str) -> date:
    try:
        return date.fromisoformat(str(raw))
    except (ValueError, TypeError):
        raise ValidationError(f"gst: {where} must be a date (YYYY-MM-DD), got {raw!r}") from None


def parse_download(text: str) -> ParsedDownload:
    """Parse one ``lf-gst-2a/1`` / ``lf-gst-2b/1`` adapter file — the
    operator's translated portal download. All-or-nothing like the bank
    statement parser: one bad row names itself and refuses the import."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"gst: the download is not valid JSON ({exc})") from None
    if not isinstance(payload, dict):
        raise ValidationError("gst: the download must be a JSON object")

    kind = str(payload.get("kind") or "").strip()
    if kind in KINDS:
        pass  # the bare '2a'/'2b' shorthand is accepted
    elif kind in DOWNLOAD_KINDS:
        kind = DOWNLOAD_KINDS[kind]
    else:
        raise ValidationError(
            "gst: kind must be one of lf-gst-2a/1, lf-gst-2b/1 (or the bare 2a/2b), "
            f"got {payload.get('kind')!r}"
        )

    period = str(payload.get("period") or "").strip()
    if not _PERIOD_RE.match(period):
        raise ValidationError(f"gst: period must be YYYY-MM (the return period), got {period!r}")

    gstin = str(payload.get("gstin") or "").strip().upper()
    if not valid_gstin(gstin):
        raise ValidationError(
            f"gst: gstin (the registrant whose statement this is) is malformed: {gstin!r}"
        )

    entries_raw = payload.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise ValidationError("gst: entries must be a non-empty list of supplier documents")

    entries: list[PortalEntry] = []
    rejects: list[tuple[int, str]] = []
    for i, raw in enumerate(entries_raw, start=1):
        try:
            entries.append(_parse_entry(raw, i))
        except ValidationError as exc:
            rejects.append((i, str(exc).removeprefix("gst: ")))
    if rejects:
        head = "; ".join(f"entry {no}: {reason}" for no, reason in rejects[:5])
        more = f" (+{len(rejects) - 5} more)" if len(rejects) > 5 else ""
        raise ValidationError(
            f"gst: {len(rejects)} of {len(entries_raw)} entries rejected — nothing "
            f"imported (all-or-nothing): {head}{more}"
        )

    source = str(payload.get("source") or "").strip() or None
    return ParsedDownload(
        kind=kind, period=period, gstin=gstin, source=source, entries=tuple(entries)
    )


def _parse_entry(raw: object, index: int) -> PortalEntry:
    if not isinstance(raw, dict):
        raise ValidationError(f"entries[{index}] must be an object")
    supplier = str(raw.get("supplier_gstin") or raw.get("gstin") or "").strip().upper()
    if not valid_gstin(supplier):
        raise ValidationError(f"entries[{index}].supplier_gstin is malformed: {supplier!r}")
    doc_type = str(raw.get("doc_type") or "INV").strip().upper()
    if doc_type not in DOC_TYPES:
        raise ValidationError(
            f"entries[{index}].doc_type must be one of INV, CN, DN, got {doc_type!r}"
        )
    doc_no = str(raw.get("doc_no") or "").strip()
    if not doc_no:
        raise ValidationError(f"entries[{index}].doc_no is required")
    doc_date = _parse_date(raw.get("doc_date"), f"entries[{index}].doc_date")
    taxable = _decimal(raw.get("taxable", 0), f"entries[{index}].taxable")
    if taxable < 0:
        raise ValidationError(f"entries[{index}].taxable must be >= 0")
    igst = _decimal(raw.get("igst", 0), f"entries[{index}].igst")
    cgst = _decimal(raw.get("cgst", 0), f"entries[{index}].cgst")
    sgst = _decimal(raw.get("sgst", 0), f"entries[{index}].sgst")
    if min(igst, cgst, sgst) < 0:
        raise ValidationError(f"entries[{index}] tax amounts must be >= 0")
    pos = str(raw.get("pos") or "").strip() or None
    itc_available = raw.get("itc_available", True)
    if not isinstance(itc_available, bool):
        raise ValidationError(f"entries[{index}].itc_available must be true or false")
    return PortalEntry(
        supplier_gstin=supplier,
        party=str(raw.get("party") or "").strip() or None,
        doc_type=doc_type,
        doc_no=doc_no,
        doc_date=doc_date,
        taxable=taxable,
        igst=igst,
        cgst=cgst,
        sgst=sgst,
        pos=pos,
        itc_available=itc_available,
    )


# ------------------------------------------------------------- fingerprints
def _fingerprint(parsed: ParsedDownload, entry: PortalEntry, occurrence: int, revision: int) -> str:
    """The row's content identity as one snapshot holds it: the statement's
    own identity (kind, period, registrant) + the row's facts (including
    amounts — a portal revision is new content, not a duplicate) + the
    occurrence among identical twins + the snapshot's revision. The import
    checks a re-download against the LATEST snapshot's fingerprints (its
    own revision), so an unchanged period is a clean no-op while a
    re-statement lands whole beside the snapshots it supersedes."""
    payload = "|".join(
        (
            parsed.kind,
            parsed.period,
            parsed.gstin,
            entry.supplier_gstin,
            entry.doc_type,
            entry.doc_no,
            entry.doc_date.isoformat(),
            str(entry.taxable),
            str(entry.igst),
            str(entry.cgst),
            str(entry.sgst),
            entry.pos or "",
            "1" if entry.itc_available else "0",
            str(occurrence),
            str(revision),
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------- the import
def import_download(
    session: Session,
    *,
    book: Book,
    text: str,
    source: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Parse, validate and (unless ``dry_run``) insert one portal download
    — all-or-nothing, idempotent by content fingerprint. Returns the
    receipt the UIs show and the ``gst.download.imported`` event carries."""
    parsed = parse_download(text)

    # the import revision: this download's (kind, period, gstin) snapshot
    # number — 1 for the first, one more per changed re-download. The fold
    # prefers the highest revision per document key, so the portal's
    # latest state wins without ever editing a stored fact.
    prior_batches = (
        session.execute(
            select(GstDownload.id).where(
                GstDownload.book_id == book.id,
                GstDownload.kind == parsed.kind,
                GstDownload.period == parsed.period,
                GstDownload.gstin == parsed.gstin,
            )
        )
        .scalars()
        .all()
    )
    revision = len(prior_batches) + 1
    # idempotency prices the LATEST snapshot: the newest batch is the
    # len(prior_batches)-th import, so its rows carry that revision in
    # their fingerprints — a re-download of what it already holds is a
    # no-op, while rows last seen in a superseded snapshot are re-stated
    # into the new batch (the fold reads the latest batch only, so a
    # re-statement must land whole or its carried-forward rows would
    # vanish from the comparison)
    latest_revision = len(prior_batches)

    seen: dict[tuple, int] = {}
    new_lines: list[GstDownloadLine] = []
    duplicates = 0
    for entry in parsed.entries:
        quad = (
            entry.supplier_gstin,
            entry.doc_type,
            entry.doc_no,
            str(entry.taxable),
        )
        occurrence = seen.get(quad, 0)
        seen[quad] = occurrence + 1
        already = None
        if latest_revision:
            already = session.execute(
                select(GstDownloadLine.id).where(
                    GstDownloadLine.book_id == book.id,
                    GstDownloadLine.fingerprint
                    == _fingerprint(parsed, entry, occurrence, latest_revision),
                )
            ).scalar_one_or_none()
        if already is not None:
            duplicates += 1
            continue
        fingerprint = _fingerprint(parsed, entry, occurrence, revision)
        new_lines.append(
            GstDownloadLine(
                book_id=book.id,
                batch_id=0,  # filled after the batch row flushes
                supplier_gstin=entry.supplier_gstin,
                party=entry.party,
                doc_type=entry.doc_type,
                doc_no=entry.doc_no,
                doc_date=entry.doc_date,
                taxable=entry.taxable,
                igst=entry.igst,
                cgst=entry.cgst,
                sgst=entry.sgst,
                pos=entry.pos,
                itc_available=entry.itc_available,
                fingerprint=fingerprint,
            )
        )

    label = source or parsed.source
    receipt = {
        "kind": parsed.kind,
        "period": parsed.period,
        "gstin": parsed.gstin,
        "source": label,
        "parsed": len(parsed.entries),
        "new": len(new_lines),
        "duplicates": duplicates,
        "revision": revision,
        "dry_run": dry_run,
    }
    if dry_run or not new_lines:
        return receipt  # a full duplicate imports no batch — the receipt IS the answer

    batch = GstDownload(
        book_id=book.id,
        kind=parsed.kind,
        period=parsed.period,
        gstin=parsed.gstin,
        source=label,
        line_count=len(new_lines),
    )
    session.add(batch)
    session.flush()  # batch.id for the lines
    for row in new_lines:
        row.batch_id = batch.id
        session.add(row)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    receipt["batch_id"] = batch.id
    logger.info(
        "gst: imported %s %s for %s — %d new, %d duplicates (batch %s, revision %d)",
        parsed.kind,
        parsed.period,
        parsed.gstin,
        len(new_lines),
        duplicates,
        batch.id,
        revision,
    )
    return receipt


# ----------------------------------------------------------------- the fold
def portal_lines(
    session: Session, *, book: Book, kind: str, period: str, gstin: str | None = None
) -> list[PortalLine]:
    """The portal side of one comparison: the **latest snapshot** of the
    (kind, period) statement — the registrant GSTIN filtered when a
    multi-GSTIN book names one. The portal regenerates a period's 2A/2B as
    suppliers file, so each re-download lands as a new batch (append-only,
    Law 2) and the fold takes each registration's newest batch only: the
    portal's own latest-state semantics, replayed from stored facts. Exact
    ``Decimal`` amounts as stored; zero rounding happens here."""
    if kind not in KINDS:
        raise ValidationError(f"gst: kind must be one of {', '.join(KINDS)}, got {kind!r}")
    if not _PERIOD_RE.match(period):
        raise ValidationError(f"gst: period must be YYYY-MM, got {period!r}")

    batches = session.scalars(
        select(GstDownload)
        .where(
            GstDownload.book_id == book.id,
            GstDownload.kind == kind,
            GstDownload.period == period,
        )
        .order_by(GstDownload.id)
    ).all()
    if gstin:
        wanted = gstin.upper()
        batches = [b for b in batches if b.gstin == wanted]
    # each registration's statement snapshots independently: the newest
    # batch per gstin is that registration's latest download
    latest_batch: dict[str, int] = {}
    for batch in batches:
        latest_batch[batch.gstin] = batch.id  # ascending order — the last wins
    if not latest_batch:
        return []

    rows = session.scalars(
        select(GstDownloadLine).where(
            GstDownloadLine.book_id == book.id,
            GstDownloadLine.batch_id.in_(latest_batch.values()),
        )
    ).all()
    return [
        PortalLine(
            supplier_gstin=row.supplier_gstin,
            party=row.party or "",
            doc_type=row.doc_type,
            doc_no=row.doc_no,
            doc_date=row.doc_date,
            taxable=row.taxable,
            igst=row.igst,
            cgst=row.cgst,
            sgst=row.sgst,
            pos=row.pos or "",
            itc_available=row.itc_available,
        )
        for row in sorted(rows, key=lambda r: (r.doc_date, r.doc_no))
    ]


def downloads(session: Session, *, book: Book) -> list[dict]:
    """Every import receipt, newest first — the downloads table the UIs
    list, the honest provenance of every portal-side figure."""
    rows = session.scalars(
        select(GstDownload).where(GstDownload.book_id == book.id).order_by(GstDownload.id)
    ).all()
    return [
        {
            "id": b.id,
            "kind": b.kind,
            "period": b.period,
            "gstin": b.gstin,
            "source": b.source,
            "line_count": b.line_count,
            "imported_at": b.imported_at.isoformat() if b.imported_at else None,
        }
        for b in sorted(rows, key=lambda b: b.id, reverse=True)
    ]


__all__ = [
    "DOC_TYPES",
    "KINDS",
    "ParsedDownload",
    "PortalEntry",
    "PortalLine",
    "downloads",
    "import_download",
    "parse_download",
    "portal_lines",
    "valid_gstin",
]
