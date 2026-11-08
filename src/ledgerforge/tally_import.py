"""The TallyPrime XML import (ADR-030, roadmap Phase 9) — the migration
path: a TallyPrime shop's export becomes LedgerForge books, with a
verification report that proves the imported trial balance equals the
source's — or names every difference.

The dialect: TallyPrime's XML export wraps ``TALLYMESSAGE`` objects
(squashed tag names — ``OPENINGBALANCE``, ``BASEUNITS``,
``ALLLEDGERENTRIES.LIST`` — a UDF namespace, and signed amounts:
negative is debit, ``ISDEEMEDPOSITIVE`` the tiebreak) in
``ENVELOPE/BODY/IMPORTDATA/REQUESTDATA``. The mapping leans on v0.1's
seeding decision: TallyPrime's 15 primary groups and 13 sub-groups ARE
LedgerForge's group names, so masters land where they always belonged.

The discipline (the dataexchange pipeline's, restated):

- **One transaction or nothing** — masters create-if-absent, openings as
  ONE Opening Journal, vouchers through the full engine path, every inner
  commit a savepoint release inside one outer transaction
  (``dataexchange.import_transaction``); ``dry_run`` runs everything and
  rolls back.
- **Never guess.** An unmapped voucher type is skipped and reported; a
  voucher naming a ledger the master section omitted auto-creates it
  under *Suspense A/c* and reports it (a migration that dies on the tenth
  voucher helps nobody); a Tally stock item's opening **quantity** is
  listed for re-keying — positions are documents (ADR-027), and a file
  that invented valuation rates would violate Law 3's honesty.
- **The verification report is the point.** The source trial balance is
  folded directly from the XML (ledger openings + every voucher line,
  pure arithmetic, no database); the imported trial balance is folded
  from exactly what this import posted. Per ledger, with a difference
  column and both totals asserted — an operator signs the migration when
  the differences are zero and understands every one that is not.
"""

from __future__ import annotations

import hashlib
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .dataexchange import IMPORT_NS, import_transaction
from .engine.opening import post_opening
from .engine.posting import post_voucher
from .errors import ValidationError
from .inventory import create_item as create_stock_item
from .models import Book, Group, Ledger, StockItem, VoucherEntry, VoucherType
from .plugins import PluginManager
from .schemas import OpeningCreate, VoucherCreate, VoucherLineIn
from .settings import Settings

#: TallyPrime's voucher type names → the seeded codes (seeding.py). A
#: caller may extend this through ``type_map``; anything unmapped is
#: skipped and reported, never guessed.
TALLY_TYPE_MAP: dict[str, str] = {
    "receipt": "RCT",
    "payment": "PYT",
    "journal": "JV",
    "sales": "SAL",
    "purchase": "PUR",
    "credit note": "CRN",
    "debit note": "DBN",
    "contra": "CN",
}

_ROOT_GROUP = "Primary"
_SUSPENSE_GROUP = "Suspense A/c"  # a seeded group (nature: liability)


# ------------------------------------------------------------------- parsing
@dataclass
class TallyLine:
    ledger: str
    dc: str  # "D" | "C" — ISDEEMEDPOSITIVE's verdict
    amount: Decimal  # base currency, positive
    currency: str | None = None
    amount_fx: Decimal | None = None
    fx_rate: Decimal | None = None


@dataclass
class TallyVoucher:
    vchtype: str
    voucher_date: date
    narration: str | None
    identity: str  # GUID / VCHNO / index — the dedup key inside the file
    lines: list[TallyLine] = field(default_factory=list)


@dataclass
class TallyLedger:
    name: str
    parent: str
    opening: Decimal | None = None  # Tally-signed: negative = debit
    currency: str | None = None


@dataclass
class TallyGroup:
    name: str
    parent: str


@dataclass
class TallyStockItem:
    name: str
    unit: str | None = None
    track_batches: bool = False
    track_serials: bool = False
    opening_qty: Decimal | None = None


@dataclass
class TallyDoc:
    groups: list[TallyGroup] = field(default_factory=list)
    ledgers: list[TallyLedger] = field(default_factory=list)
    stock_items: list[TallyStockItem] = field(default_factory=list)
    vouchers: list[TallyVoucher] = field(default_factory=list)


def _text_of(element: ET.Element, tag: str) -> str | None:
    """A child element's stripped text — Tally's squashed tag names
    (``OPENINGBALANCE``, ``BASEUNITS``); None when absent or empty."""
    found = element.find(tag)
    if found is None or found.text is None:
        return None
    stripped = found.text.strip()
    return stripped or None


def _decimal_of(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def _tally_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y%m%d").date()
    except ValueError:
        return None


def parse_tally_xml(text: str) -> TallyDoc:
    """Parse the export. Malformed XML is a 422; unknown shapes are simply
    not collected (the receipt's counts tell the story)."""
    try:
        envelope = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValidationError(f"tally: the file is not well-formed XML — {exc}") from exc

    doc = TallyDoc()
    for message in envelope.iter("TALLYMESSAGE"):
        for group in message.iter("GROUP"):
            name = (group.get("NAME") or _text_of(group, "NAME") or "").strip()
            if not name:
                continue
            doc.groups.append(
                TallyGroup(name=name, parent=_text_of(group, "PARENT") or _ROOT_GROUP)
            )
        for ledger in message.iter("LEDGER"):
            name = (ledger.get("NAME") or _text_of(ledger, "NAME") or "").strip()
            if not name:
                continue
            doc.ledgers.append(
                TallyLedger(
                    name=name,
                    parent=_text_of(ledger, "PARENT") or _ROOT_GROUP,
                    opening=_decimal_of(_text_of(ledger, "OPENINGBALANCE")),
                    currency=_text_of(ledger, "CURRENCY"),
                )
            )
        for item in message.iter("STOCKITEM"):
            name = (item.get("NAME") or _text_of(item, "NAME") or "").strip()
            if not name:
                continue
            doc.stock_items.append(
                TallyStockItem(
                    name=name,
                    unit=_text_of(item, "BASEUNITS"),
                    track_batches=(_text_of(item, "ISBATCHWISEON") or "").lower() == "yes",
                    track_serials=(_text_of(item, "HASSERIALNUM") or "").lower() == "yes"
                    or (_text_of(item, "HASSERIALNO") or "").lower() == "yes",
                    opening_qty=_decimal_of(_text_of(item, "OPENINGBALANCE")),
                )
            )
        for voucher in message.iter("VOUCHER"):
            vchtype = (voucher.get("VCHTYPE") or "").strip()
            voucher_date = _tally_date(voucher.get("DATE"))
            if not vchtype or voucher_date is None:
                continue
            if (_text_of(voucher, "ISCANCELLED") or "").lower() == "yes":
                continue
            identity = (
                voucher.get("GUID")
                or _text_of(voucher, "GUID")
                or voucher.get("VCHNO")
                or _text_of(voucher, "VCHNO")
                or f"#{len(doc.vouchers) + 1}"
            )
            parsed = TallyVoucher(
                vchtype=vchtype,
                voucher_date=voucher_date,
                narration=_text_of(voucher, "NARRATION"),
                identity=identity,
            )
            for entry in voucher.iter("ALLLEDGERENTRIES.LIST"):
                ledger_name = (_text_of(entry, "LEDGERNAME") or "").strip()
                if not ledger_name:
                    continue
                amount = _decimal_of(_text_of(entry, "AMOUNT"))
                if amount is None:
                    continue
                deemed = (_text_of(entry, "ISDEEMEDPOSITIVE") or "").lower() == "yes"
                # Tally's convention: debits are negative AND deemed-positive.
                # Either signal is sufficient — ISDEEMEDPOSITIVE is the verdict,
                # the sign corroborates (Tally emits both, occasionally at odds).
                dc = "D" if (deemed or amount < 0) else "C"
                currency = _text_of(entry, "CURRENCY")
                amount_fx = _decimal_of(_text_of(entry, "FOREXAMOUNT"))
                fx_rate = _decimal_of(_text_of(entry, "EXCHANGERATE"))
                trio = (currency, amount_fx, fx_rate)
                if not all(trio):
                    trio = (None, None, None)
                parsed.lines.append(
                    TallyLine(
                        ledger=ledger_name,
                        dc=dc,
                        amount=abs(amount),
                        currency=trio[0],
                        amount_fx=trio[1],
                        fx_rate=trio[2],
                    )
                )
            if parsed.lines:
                doc.vouchers.append(parsed)
    return doc


# --------------------------------------------------------------- source fold
def source_trial_balance(
    doc: TallyDoc, balance_to: str | None, residual: Decimal
) -> dict[str, Decimal]:
    """The trial balance folded directly from the XML: every ledger's
    opening (Tally-signed, negative = debit → Dr-positive) plus every
    voucher line, all vouchers, mapped or not — the source is the source.
    The ``balance_to`` residual joins the source side so a balanced import
    of an unbalanced source still compares at zero (ADR-030)."""
    balances: dict[str, Decimal] = {}

    def _move(ledger: str, signed: Decimal) -> None:
        balances[ledger] = balances.get(ledger, Decimal(0)) + signed

    for ledger in doc.ledgers:
        if ledger.opening:
            _move(ledger.name, -ledger.opening)  # negative = debit → Dr-positive
    for voucher in doc.vouchers:
        for line in voucher.lines:
            _move(line.ledger, line.amount if line.dc == "D" else -line.amount)
    if balance_to and residual:
        # the balancing line the import will book: a Dr surplus is absorbed
        # by a Cr (and vice versa), so the source side carries -residual —
        # exactly what lands, keeping the comparison at zero (ADR-030)
        _move(balance_to, -residual)
    return balances


def imported_trial_balance(session: Session, voucher_ids: list[int]) -> dict[str, Decimal]:
    """The trial balance of exactly what this import posted — the opening
    journal plus every mapped voucher, folded from the engine's own
    entries inside the import transaction."""
    if not voucher_ids:
        return {}
    rows = session.execute(
        select(Ledger.name, VoucherEntry.dc, VoucherEntry.amount)
        .join(VoucherEntry, VoucherEntry.ledger_id == Ledger.id)
        .where(VoucherEntry.voucher_id.in_(voucher_ids))
    ).all()
    balances: dict[str, Decimal] = {}
    for name, dc, amount in rows:
        balances[name] = balances.get(name, Decimal(0)) + (amount if dc == "D" else -amount)
    return balances


def _render(signed: Decimal) -> str:
    """Dr-positive, exact digits at the money scale (cents — the engine's
    own Law-3 precision), the report's whole convention."""
    return format(signed.quantize(Decimal("0.01")), "f")


# ------------------------------------------------------------------ the drive
def import_tally(
    session: Session,
    *,
    book: Book,
    manager: PluginManager,
    settings: Settings,
    xml: str,
    source: str | None = None,
    dry_run: bool = False,
    type_map: dict[str, str] | None = None,
    balance_to: str | None = None,
) -> dict:
    """The one front door for TallyPrime exports (ADR-030). Returns the
    receipt with the verification report — the operator's proof."""
    doc = parse_tally_xml(xml)
    if not (doc.groups or doc.ledgers or doc.stock_items or doc.vouchers):
        raise ValidationError(
            "tally: the export carries no TALLYMESSAGE objects LedgerForge "
            "recognizes (GROUP, LEDGER, STOCKITEM, VOUCHER)"
        )
    fingerprint = hashlib.sha256(xml.encode("utf-8", "surrogateescape")).hexdigest()
    type_map = {**TALLY_TYPE_MAP, **{k.lower(): v.upper() for k, v in (type_map or {}).items()}}

    emissions: list[dict] = []
    with import_transaction(session, dry_run=dry_run) as inner:
        import_book = inner.get(Book, book.id)
        assert import_book is not None
        receipt = _import_document(
            inner,
            book=import_book,
            manager=manager,
            settings=settings,
            doc=doc,
            fingerprint=fingerprint,
            type_map=type_map,
            balance_to=balance_to,
            emissions=emissions,
        )

    if not dry_run:
        for payload in emissions:
            manager.emit("voucher.posted", payload)

    receipt.update({"kind": "tally", "source": source, "dry_run": dry_run})
    return receipt


def _group_by_name(session: Session, book: Book, name: str) -> Group | None:
    return session.scalar(select(Group).where(Group.book_id == book.id, Group.name == name))


def _ensure_group(
    session: Session, book: Book, name: str, *, default_parent: str, seen: set[str] | None = None
) -> Group:
    """Create-if-absent, recursing through unknown ancestors (Tally's tree
    is deeper than the seeded primaries; a missing parent lands under the
    default — never a guessed nature)."""
    existing = _group_by_name(session, book, name)
    if existing is not None:
        return existing
    seen = seen or set()
    if name in seen:
        raise ValidationError(f"tally: group cycle through {name!r} — the file is unresolvable")
    seen.add(name)
    parent = _group_by_name(session, book, default_parent)
    if parent is None:
        parent = _ensure_group(session, book, default_parent, default_parent=_ROOT_GROUP, seen=seen)
    group = Group(
        book_id=book.id,
        name=name,
        parent_id=parent.id,
        nature=parent.nature,
        affects_gross=parent.affects_gross,
    )
    session.add(group)
    session.flush()  # the closure trigger runs; the tree is walkable at once
    return group


def _import_document(
    session: Session,
    *,
    book: Book,
    manager: PluginManager,
    settings: Settings,
    doc: TallyDoc,
    fingerprint: str,
    type_map: dict[str, str],
    balance_to: str | None,
    emissions: list[dict],
) -> dict:
    counts = {
        "groups_created": 0,
        "ledgers_created": 0,
        "existing": 0,
        "stock_items_created": 0,
        "stock_items_existing": 0,
    }
    auto_ledgers: list[str] = []
    stock_openings: list[dict] = []

    # ---- masters: groups (unknown parents chain under Primary) ---------
    for group in doc.groups:
        if _group_by_name(session, book, group.name) is not None:
            counts["existing"] += 1
            continue
        _ensure_group(session, book, group.name, default_parent=group.parent or _ROOT_GROUP)
        counts["groups_created"] += 1

    # ---- masters: ledgers ----------------------------------------------
    ledger_openings: list[tuple[str, str, Decimal]] = []  # (name, dc, amount)
    for ledger in doc.ledgers:
        found = session.scalar(
            select(Ledger).where(Ledger.book_id == book.id, Ledger.name == ledger.name)
        )
        if found is not None:
            counts["existing"] += 1
        else:
            parent = _group_by_name(session, book, ledger.parent)
            if parent is None:
                # a ledger under a group the export never described: Primary
                # keeps it in the book; the report says where it went
                parent = _group_by_name(session, book, _ROOT_GROUP)
                assert parent is not None
            session.add(Ledger(book_id=book.id, name=ledger.name, group_id=parent.id))
            session.flush()
            counts["ledgers_created"] += 1
        if ledger.opening:
            ledger_openings.append(
                (ledger.name, "D" if ledger.opening < 0 else "C", abs(ledger.opening))
            )

    # ---- masters: stock items -------------------------------------------
    for item in doc.stock_items:
        found = session.scalar(
            select(StockItem).where(StockItem.book_id == book.id, StockItem.name == item.name)
        )
        if found is not None:
            counts["stock_items_existing"] += 1
        else:
            payload: dict[str, Any] = {"name": item.name}
            if item.unit:
                payload["unit"] = item.unit
            if item.track_batches:
                payload["track_batches"] = True
            if item.track_serials:
                payload["track_serials"] = True
            create_stock_item(session, book, payload)
            session.flush()
            counts["stock_items_created"] += 1
        if item.opening_qty:
            stock_openings.append(
                {
                    "item": item.name,
                    "qty": str(item.opening_qty),
                    "note": "re-key through Inventory (positions are documents, ADR-027)",
                }
            )

    # ---- the opening journal ---------------------------------------------
    opening_receipt: dict = {}
    opening_voucher_ids: list[int] = []
    total_dr = sum((a for _, dc, a in ledger_openings if dc == "D"), Decimal(0))
    total_cr = sum((a for _, dc, a in ledger_openings if dc == "C"), Decimal(0))
    residual = total_dr - total_cr
    balanced_to = None
    if ledger_openings:
        if residual != 0 and not balance_to:
            raise ValidationError(
                "tally: the export's opening balances do not balance — total debit "
                f"{total_dr} against total credit {total_cr} (difference {residual}). "
                "Tally's Profit & Loss opening or a mis-keyed master is usually the "
                "cause; pass balance_to (classically 'Suspense A/c') to absorb it "
                "explicitly, or fix the export."
            )
        entries = [
            VoucherLineIn(ledger_name=name, dc=dc, amount=amount)
            for name, dc, amount in ledger_openings
        ]
        if residual != 0:
            from .dataexchange import _resolve_balance_anchor

            _anchor, _created = _resolve_balance_anchor(session, book, balance_to)
            balanced_to = balance_to
            entries.append(
                VoucherLineIn(
                    ledger_name=balance_to,
                    dc="C" if residual > 0 else "D",
                    amount=abs(residual),
                    narration="opening difference absorbed by the Tally import",
                )
            )
        payload = OpeningCreate(
            voucher_date=doc.vouchers[0].voucher_date if doc.vouchers else date.today(),
            narration="Opening balances (TallyPrime import)",
            client_token=uuid.uuid5(IMPORT_NS, f"tally:{fingerprint}:openings"),
            entries=entries,
        )
        voucher, created = post_opening(session, payload, manager, settings, book, emit=False)
        opening_voucher_ids = [voucher.id]
        if created:
            emissions.append(
                {
                    "voucher_id": voucher.id,
                    "voucher_no": voucher.voucher_no,
                    "uuid": str(voucher.uuid),
                    "book_id": book.id,
                    "voucher_type": "OPEN",
                    "voucher_date": voucher.voucher_date.isoformat(),
                    "meta": dict(voucher.meta or {}),
                }
            )
        opening_receipt = {
            "voucher_no": voucher.voucher_no,
            "posted": created,
            "replayed": not created,
            "total_debit": str(total_dr + (0 if residual > 0 else -residual)),
            "total_credit": str(total_cr + (residual if residual > 0 else 0)),
            "balanced_to": balanced_to,
        }

    # ---- vouchers ---------------------------------------------------------
    skipped: list[dict] = []
    posted = replayed = 0
    posted_ids: list[int] = list(opening_voucher_ids)
    known_codes = set(
        session.scalars(select(VoucherType.code).where(VoucherType.book_id == book.id))
    )
    for voucher in doc.vouchers:
        code = type_map.get(voucher.vchtype.lower())
        if code is None or code not in known_codes:
            skipped.append(
                {
                    "vchtype": voucher.vchtype,
                    "date": voucher.voucher_date.isoformat(),
                    "identity": voucher.identity,
                    "reason": (
                        f"voucher type {voucher.vchtype!r} has no LedgerForge mapping "
                        "(extend type_map) — skipped, never guessed"
                        if code is None
                        else f"voucher type maps to {code!r}, which does not exist in "
                        f"book {book.name!r} — skipped"
                    ),
                }
            )
            continue
        entries: list[VoucherLineIn] = []
        for line in voucher.lines:
            found = session.scalar(
                select(Ledger).where(Ledger.book_id == book.id, Ledger.name == line.ledger)
            )
            if found is None:
                # a voucher naming a ledger the master section omitted:
                # create under Suspense A/c and report — never die mid-file
                suspense = _group_by_name(session, book, _SUSPENSE_GROUP)
                if suspense is None:
                    suspense = _group_by_name(session, book, _ROOT_GROUP)
                    assert suspense is not None
                session.add(Ledger(book_id=book.id, name=line.ledger, group_id=suspense.id))
                session.flush()
                auto_ledgers.append(line.ledger)
            entries.append(
                VoucherLineIn(
                    ledger_name=line.ledger,
                    dc=line.dc,
                    amount=line.amount,
                    currency=line.currency,
                    amount_fx=line.amount_fx,
                    fx_rate=line.fx_rate,
                )
            )
        token = uuid.uuid5(
            IMPORT_NS, f"tally:{fingerprint}:{voucher.identity}:{voucher.voucher_date}"
        )
        payload = VoucherCreate(
            voucher_type=code,
            voucher_date=voucher.voucher_date,
            narration=voucher.narration,
            client_token=token,
            entries=entries,
        )
        posted_voucher, created = post_voucher(
            session, payload, manager, settings, book, emit=False
        )
        posted_ids.append(posted_voucher.id)
        if created:
            posted += 1
            emissions.append(
                {
                    "voucher_id": posted_voucher.id,
                    "voucher_no": posted_voucher.voucher_no,
                    "uuid": str(posted_voucher.uuid),
                    "book_id": book.id,
                    "voucher_type": code,
                    "voucher_date": posted_voucher.voucher_date.isoformat(),
                    "meta": dict(posted_voucher.meta or {}),
                }
            )
        else:
            replayed += 1

    # ---- the verification report -------------------------------------------
    source_tb = source_trial_balance(doc, balanced_to, residual)
    imported_tb = imported_trial_balance(session, posted_ids)
    rows: list[dict] = []
    differences = 0
    for ledger in sorted(set(source_tb) | set(imported_tb)):
        source_balance = source_tb.get(ledger, Decimal(0))
        imported_balance = imported_tb.get(ledger, Decimal(0))
        difference = source_balance - imported_balance
        if difference != 0:
            differences += 1
        rows.append(
            {
                "ledger": ledger,
                "source": _render(source_balance),
                "imported": _render(imported_balance),
                "difference": _render(difference),
            }
        )
    verification = {
        # Dr-positive signed decimals; a balanced trial balance totals 0.00
        "source_total": _render(sum(source_tb.values(), Decimal(0))),
        "imported_total": _render(sum(imported_tb.values(), Decimal(0))),
        "differences": differences,
        "rows": rows,
    }

    return {
        "masters": counts,
        "auto_ledgers": auto_ledgers,
        "stock_openings": stock_openings,
        "openings": opening_receipt,
        "vouchers": {
            "in_file": len(doc.vouchers),
            "mapped": posted + replayed,
            "posted": posted,
            "replayed": replayed,
            "skipped": skipped,
        },
        "verification": verification,
    }
