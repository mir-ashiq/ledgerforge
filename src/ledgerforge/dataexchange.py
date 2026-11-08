"""The import pipeline (ADR-030, roadmap Phase 9): masters, opening
balances, vouchers and stock masters arrive by file — every import a
previewable, idempotent, all-or-nothing transaction, and the engine never
grows a bulk lane.

The discipline (the bank-statement precedent, ADR-026, restated for the
accounting side):

- **One transaction or nothing.** The service opens ONE outer transaction
  on its own connection; every voucher posts through ``post_voucher``
  whose ``commit()`` degrades to a savepoint release
  (``join_transaction_mode="create_savepoint"``), masters flush inside the
  same transaction — so a bad row in the middle rolls the whole file back,
  and the after-commit events fire only after the OUTER commit (the
  ``emit=False`` seam, ADR-030).
- **Law 1 walks the whole path.** An imported voucher meets the plugin
  transformers, the exact-Decimal balance check, the FX rules, the period
  gate, gapless numbering and the deferred trigger — exactly what a
  keyboard voucher meets. Nothing is bulk-INSERTed.
- **Idempotency is content-addressed.** Masters are create-if-absent by
  ``(book, name)`` — an existing name is reported, never mutated. Voucher
  client tokens are ``uuid5`` of the file's fingerprint plus the row's
  ``ref`` — a re-import of the same file replays as no-ops; a *corrected*
  file is a new import (append-only history, Law 2), never an edit.
- **``dry_run`` is the real thing, rolled back.** The preview runs the
  full pipeline inside the transaction and rolls it back — every trigger
  and guard included, nothing written.

Books are strangers (ADR-009): an import lands in exactly one book.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .engine.opening import post_opening
from .engine.posting import post_voucher
from .errors import ValidationError
from .inventory import create_item as create_stock_item
from .models import Book, Group, Ledger, StockItem
from .plugins import PluginManager
from .schemas import OpeningCreate, VoucherCreate, VoucherLineIn
from .settings import Settings

#: The importable kinds. ``bank`` routes to ADR-026's own machinery.
IMPORT_KINDS = ("masters", "openings", "vouchers", "stock", "bank")

#: The tabular codecs (ADR-030). xlsx is the only Excel dialect.
TABULAR_FORMATS = ("csv", "xlsx")

#: uuid5 namespace — the import pipeline's own, stable across releases.
IMPORT_NS = uuid.UUID("b0e04888-3ab7-4d84-9c02-2f4bd7f8a911")

_NATURES = ("asset", "liability", "income", "expense", "equity")
_ROOT_GROUP = "Primary"


def norm_header(name: str) -> str:
    """A header made comparable: case, spaces and punctuation die — the
    bank parser's convention (``Ledger Name`` ≡ ``ledger_name``)."""
    return re.sub(r"[^a-z0-9]+", "", name.strip().lower())


def decode_tabular(fmt: str, content: str) -> list[dict[str, str]]:
    """``csv`` (text, first row headers) or ``xlsx`` (base64, first sheet,
    first row headers) → rows keyed by normalized header. Values stay
    strings; every field parser downstream is explicit and names its row.
    """
    if fmt not in TABULAR_FORMATS:
        raise ValidationError(f"dataexchange: unknown table format {fmt!r}")
    if fmt == "csv":
        try:
            reader = csv.reader(io.StringIO(content))
            header = next(reader, None)
        except csv.Error as exc:
            raise ValidationError(f"dataexchange: unreadable CSV — {exc}") from exc
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    else:
        try:
            raw = base64.b64decode(content, validate=True)
        except Exception as exc:
            raise ValidationError("dataexchange: the xlsx payload is not valid base64") from exc
        try:
            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            worksheet = workbook.worksheets[0]
            matrix = [
                [("" if cell is None else str(cell).strip()) for cell in row]
                for row in worksheet.iter_rows(values_only=True)
            ]
            workbook.close()
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError(f"dataexchange: unreadable xlsx — {exc}") from exc
        matrix = [row for row in matrix if any(cell for cell in row)]
        if not matrix:
            raise ValidationError("dataexchange: the workbook's first sheet is empty")
        header, rows = matrix[0], matrix[1:]

    if not header or not any(str(cell).strip() for cell in header):
        raise ValidationError("dataexchange: the first row must carry column headers")
    keys = [norm_header(str(cell)) for cell in header]
    seen: set[str] = set()
    for key in keys:
        if not key:
            raise ValidationError("dataexchange: every column needs a header")
        if key in seen:
            raise ValidationError(f"dataexchange: duplicate column {key!r} in the header row")
        seen.add(key)
    return [
        {
            key: (row[index].strip() if index < len(row) and row[index] is not None else "")
            for index, key in enumerate(keys)
        }
        for row in rows
    ]


# ------------------------------------------------------------- field parsers
def _field(row: dict[str, str], name: str) -> str:
    """A field's value, looked up by its NORMALIZED header (``Ledger
    Name`` ≡ ``ledger_name`` ≡ the row key ``ledgername``) — the bank
    parser's convention, applied to every lookup so callers spell fields
    naturally."""
    return (row.get(norm_header(name)) or "").strip()


def _text(row: dict[str, str], key: str, *, row_no: int, required: bool = False) -> str:
    value = _field(row, key)
    if not value and required:
        raise ValidationError(f"dataexchange: row {row_no}: {key.replace('_', ' ')} is required")
    return value


def _decimal(
    row: dict[str, str], key: str, *, row_no: int, required: bool = False
) -> Decimal | None:
    raw = _field(row, key)
    if not raw:
        if required:
            raise ValidationError(
                f"dataexchange: row {row_no}: {key.replace('_', ' ')} is required"
            )
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ValidationError(
            f"dataexchange: row {row_no}: {key.replace('_', ' ')} {raw!r} is not a number"
        ) from None


def _yes_no(row: dict[str, str], key: str, *, row_no: int) -> bool | None:
    raw = _field(row, key).lower()
    if not raw:
        return None
    if raw in ("yes", "y", "true", "1"):
        return True
    if raw in ("no", "n", "false", "0"):
        return False
    raise ValidationError(
        f"dataexchange: row {row_no}: {key.replace('_', ' ')} must be yes or no, got {raw!r}"
    )


def _date_field(raw: str, *, row_no: int, key: str = "date") -> date:
    raw = raw.strip()
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%Y%m%d", "%d-%b-%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    raise ValidationError(
        f"dataexchange: row {row_no}: {key} {raw!r} is not a date (use YYYY-MM-DD)"
    ) from None


def _fingerprint(kind: str, fmt: str, content: str) -> str:
    """The file's identity — the unit of import idempotency."""
    hasher = hashlib.sha256()
    hasher.update(kind.encode())
    hasher.update(b"\x00")
    hasher.update(fmt.encode())
    hasher.update(b"\x00")
    hasher.update(content.encode("utf-8", "surrogateescape"))
    return hasher.hexdigest()


def _import_token(kind: str, fingerprint: str, ref: str) -> uuid.UUID:
    """The deterministic client token: same file + same ref → replay."""
    return uuid.uuid5(IMPORT_NS, f"{kind}:{fingerprint}:{ref}")


# ------------------------------------------------------------------- masters
def _group_by_name(session: Session, book: Book, name: str) -> Group | None:
    return session.scalar(select(Group).where(Group.book_id == book.id, Group.name == name))


def import_masters(session: Session, *, book: Book, rows: list[dict[str, str]]) -> dict:
    """Groups and ledgers, create-if-absent by name. An existing name is
    reported ``existing`` — an import never edits a master behind the
    operator's back (PATCH is the edit seam, with its own audit trail)."""
    created_groups = created_ledgers = existing = 0
    for row_no, row in enumerate(rows, start=2):  # +1 for the header row
        kind = _text(row, "kind", row_no=row_no, required=True).lower()
        name = _text(row, "name", row_no=row_no, required=True)
        parent_name = _text(row, "parent", row_no=row_no) or _ROOT_GROUP
        if kind not in ("group", "ledger"):
            raise ValidationError(
                f"dataexchange: row {row_no}: kind must be 'group' or 'ledger', got {kind!r}"
            )
        parent = _group_by_name(session, book, parent_name)
        if parent is None:
            raise ValidationError(
                f"dataexchange: row {row_no}: parent group {parent_name!r} does not exist "
                f"in book {book.name!r} — create it first (or import it earlier in this file)"
            )
        if kind == "group":
            if _group_by_name(session, book, name) is not None:
                existing += 1
                continue
            nature = _text(row, "nature", row_no=row_no).lower()
            if nature and nature not in _NATURES:
                raise ValidationError(
                    f"dataexchange: row {row_no}: nature must be one of "
                    f"{', '.join(_NATURES)}, got {nature!r}"
                )
            affects = _yes_no(row, "affects_gross", row_no=row_no)
            session.add(
                Group(
                    book_id=book.id,
                    name=name,
                    parent_id=parent.id,
                    nature=nature or parent.nature,
                    affects_gross=affects if affects is not None else parent.affects_gross,
                )
            )
            session.flush()  # the closure trigger runs; later rows may parent here
            created_groups += 1
        else:
            found = session.scalar(
                select(Ledger).where(Ledger.book_id == book.id, Ledger.name == name)
            )
            if found is not None:
                existing += 1
                continue
            session.add(Ledger(book_id=book.id, name=name, group_id=parent.id))
            session.flush()
            created_ledgers += 1
    return {
        "rows": len(rows),
        "groups_created": created_groups,
        "ledgers_created": created_ledgers,
        "existing": existing,
    }


# ------------------------------------------------------------------ openings
def _resolve_balance_anchor(session: Session, book: Book, balance_to: str) -> tuple[Ledger, bool]:
    """The ``balance_to`` anchor: an existing ledger by name, or — when the
    name matches an existing GROUP (classically *Suspense A/c*, a seeded
    group with no seeded ledger) — a ledger created under that group.
    The operator named the anchor explicitly; honouring the instruction
    is not a guess. Returns (ledger, created)."""
    anchor = session.scalar(
        select(Ledger).where(Ledger.book_id == book.id, Ledger.name == balance_to)
    )
    if anchor is not None:
        return anchor, False
    group = _group_by_name(session, book, balance_to)
    if group is not None:
        anchor = Ledger(book_id=book.id, name=balance_to, group_id=group.id)
        session.add(anchor)
        session.flush()
        return anchor, True
    raise ValidationError(
        f"dataexchange: balance_to ledger {balance_to!r} does not exist in "
        f"book {book.name!r} — create it first, or name a group whose ledger "
        "may be created (classically 'Suspense A/c')"
    )


def import_openings(
    session: Session,
    *,
    book: Book,
    manager: PluginManager,
    settings: Settings,
    rows: list[dict[str, str]],
    fingerprint: str,
    balance_to: str | None,
) -> tuple[dict, list[dict]]:
    """All rows post as ONE Opening Journal (type OPEN) — Law 1 applies to
    openings exactly. An unbalanced file is a 422 naming the difference;
    ``balance_to`` (a ledger name, classically *Suspense A/c*) absorbs a
    residual explicitly, never silently."""
    if not rows:
        raise ValidationError("dataexchange: the file carries no opening rows")
    entries: list[VoucherLineIn] = []
    total_dr = total_cr = Decimal(0)
    opening_date: date | None = None
    for row_no, row in enumerate(rows, start=2):
        ledger = _text(row, "ledger", row_no=row_no, required=True)
        dc = _text(row, "dc", row_no=row_no, required=True).upper()
        if dc not in ("D", "C"):
            raise ValidationError(f"dataexchange: row {row_no}: dc must be D or C, got {dc!r}")
        amount = _decimal(row, "amount", row_no=row_no, required=True)
        assert amount is not None
        if amount <= 0:
            raise ValidationError(
                f"dataexchange: row {row_no}: opening amounts must be positive "
                "(the dc column carries the side)"
            )
        raw_date = _text(row, "date", row_no=row_no)
        if raw_date:
            row_date = _date_field(raw_date, row_no=row_no)
            if opening_date is None:
                opening_date = row_date
            elif row_date != opening_date:
                raise ValidationError(
                    "dataexchange: one opening journal per file — every row must "
                    f"carry the same date (row {row_no} says {row_date}, the file "
                    f"started with {opening_date})"
                )
        entries.append(VoucherLineIn(ledger_name=ledger, dc=dc, amount=amount))
        if dc == "D":
            total_dr += amount
        else:
            total_cr += amount

    residual = total_dr - total_cr
    balanced_to = None
    if residual != 0:
        if not balance_to:
            raise ValidationError(
                "dataexchange: the opening file does not balance — total debit "
                f"{total_dr} against total credit {total_cr} (difference {residual}). "
                "Fix the rows, or pass balance_to with a ledger name (classically "
                "'Suspense A/c') to absorb the difference explicitly."
            )
        _anchor, _created = _resolve_balance_anchor(session, book, balance_to)
        balanced_to = balance_to
        entries.append(
            VoucherLineIn(
                ledger_name=balance_to,
                dc="C" if residual > 0 else "D",
                amount=abs(residual),
                narration="opening difference absorbed by import",
            )
        )
        if residual > 0:
            total_cr += residual
        else:
            total_dr += -residual

    payload = OpeningCreate(
        voucher_date=opening_date or date.today(),
        narration="Opening balances (imported)",
        client_token=_import_token("openings", fingerprint, "openings"),
        entries=entries,
    )
    voucher, created = post_opening(session, payload, manager, settings, book, emit=False)
    emission = {
        "voucher_id": voucher.id,
        "voucher_no": voucher.voucher_no,
        "uuid": str(voucher.uuid),
        "book_id": book.id,
        "voucher_type": "OPEN",
        "voucher_date": voucher.voucher_date.isoformat(),
        "meta": dict(voucher.meta or {}),
    }
    receipt = {
        "rows": len(rows),
        "voucher_no": voucher.voucher_no,
        "posted": created,
        "replayed": not created,
        "total_debit": str(total_dr),
        "total_credit": str(total_cr),
        "balanced_to": balanced_to,
    }
    return receipt, ([emission] if created else [])


# ------------------------------------------------------------------ vouchers
def import_vouchers(
    session: Session,
    *,
    book: Book,
    manager: PluginManager,
    settings: Settings,
    rows: list[dict[str, str]],
    fingerprint: str,
) -> tuple[dict, list[dict]]:
    """Rows grouped by ``ref`` into vouchers; each posts through the full
    engine path. A group that does not balance is a 422 naming the ref —
    and the whole file rolls back with it."""
    groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for row_no, row in enumerate(rows, start=2):
        ref = _text(row, "ref", row_no=row_no, required=True)
        groups.setdefault(ref, []).append((row_no, row))
    if not groups:
        raise ValidationError("dataexchange: the file carries no voucher rows")

    emissions: list[dict] = []
    posted = replayed = 0
    for ref, group in groups.items():
        first_no, first = group[0]
        raw_date = _text(first, "date", row_no=first_no, required=True)
        voucher_date = _date_field(raw_date, row_no=first_no)
        type_code = _text(first, "type", row_no=first_no, required=True).upper()
        narration = _text(first, "narration", row_no=first_no) or None
        entries: list[VoucherLineIn] = []
        for row_no, row in group:
            ledger = _text(row, "ledger", row_no=row_no, required=True)
            dc = _text(row, "dc", row_no=row_no, required=True).upper()
            if dc not in ("D", "C"):
                raise ValidationError(
                    f"dataexchange: row {row_no} (ref {ref!r}): dc must be D or C, got {dc!r}"
                )
            amount = _decimal(row, "amount", row_no=row_no, required=True)
            assert amount is not None
            currency = _text(row, "currency", row_no=row_no).upper() or None
            amount_fx = _decimal(row, "amount_fx", row_no=row_no)
            fx_rate = _decimal(row, "fx_rate", row_no=row_no)
            fx_count = sum(field is not None for field in (currency, amount_fx, fx_rate))
            if fx_count not in (0, 3):
                raise ValidationError(
                    f"dataexchange: row {row_no} (ref {ref!r}): foreign-currency lines "
                    "need currency, amount_fx and fx_rate together"
                )
            entries.append(
                VoucherLineIn(
                    ledger_name=ledger,
                    dc=dc,
                    amount=amount,
                    narration=_text(row, "line_narration", row_no=row_no) or None,
                    currency=currency,
                    amount_fx=amount_fx,
                    fx_rate=fx_rate,
                )
            )
        payload = VoucherCreate(
            voucher_type=type_code,
            voucher_date=voucher_date,
            narration=narration,
            client_token=_import_token("vouchers", fingerprint, ref),
            entries=entries,
        )
        voucher, created = post_voucher(session, payload, manager, settings, book, emit=False)
        if created:
            posted += 1
            emissions.append(
                {
                    "voucher_id": voucher.id,
                    "voucher_no": voucher.voucher_no,
                    "uuid": str(voucher.uuid),
                    "book_id": book.id,
                    "voucher_type": type_code,
                    "voucher_date": voucher.voucher_date.isoformat(),
                    "meta": dict(voucher.meta or {}),
                }
            )
        else:
            replayed += 1
    return {
        "rows": len(rows),
        "vouchers": len(groups),
        "posted": posted,
        "replayed": replayed,
    }, emissions


# --------------------------------------------------------------------- stock
def import_stock(session: Session, *, book: Book, rows: list[dict[str, str]]) -> dict:
    """Stock item masters (core tables, ADR-027): name, unit, tracking
    modes, re-order levels. Create-if-absent by ``(book, name)`` — stock
    *quantities* are pack folds, never rows: positions arrive through
    documents, never through a file."""
    created = existing = 0
    for row_no, row in enumerate(rows, start=2):
        name = _text(row, "name", row_no=row_no, required=True)
        found = session.scalar(
            select(StockItem).where(StockItem.book_id == book.id, StockItem.name == name)
        )
        if found is not None:
            existing += 1
            continue
        payload: dict[str, Any] = {"name": name}
        unit = _text(row, "unit", row_no=row_no)
        if unit:
            payload["unit"] = unit
        batches = _yes_no(row, "track_batches", row_no=row_no)
        if batches is not None:
            payload["track_batches"] = batches
        serials = _yes_no(row, "track_serials", row_no=row_no)
        if serials is not None:
            payload["track_serials"] = serials
        for field in ("reorder_level", "reorder_quantity"):
            value = _decimal(row, field, row_no=row_no)
            if value is not None:
                payload[field] = value
        notes = _text(row, "notes", row_no=row_no)
        if notes:
            payload["notes"] = notes
        create_stock_item(session, book, payload)
        session.flush()
        created += 1
    return {"rows": len(rows), "created": created, "existing": existing}


# ------------------------------------------------------------------ the drive
def import_transaction(session: Session, *, dry_run: bool = False):
    """One outer transaction on a dedicated connection; every inner
    ``commit()`` degrades to a savepoint release (ADR-030's one-
    transaction-or-nothing). Leaving the block commits — unless
    ``dry_run``, in which case the whole pipeline is rolled back after
    running (the preview is the real thing, discarded), and any exception
    rolls back whatever ran. Usage::

        with import_transaction(session) as inner:
            post_voucher(inner, ..., emit=False)
    """
    from contextlib import contextmanager

    bind = session.get_bind()

    @contextmanager
    def _scope():
        with bind.connect() as connection:
            outer = connection.begin()
            inner = Session(
                bind=connection,
                join_transaction_mode="create_savepoint",
                expire_on_commit=False,
            )
            try:
                yield inner
                if dry_run:
                    outer.rollback()
                else:
                    outer.commit()
            except BaseException:
                outer.rollback()
                raise
            finally:
                inner.close()

    return _scope()


def run_import(
    session: Session,
    *,
    book: Book,
    manager: PluginManager,
    settings: Settings,
    kind: str,
    fmt: str,
    content: str,
    source: str | None = None,
    dry_run: bool = False,
    balance_to: str | None = None,
    ledger_id: int | None = None,
) -> dict:
    """The one front door (ADR-030). Decodes, validates, and posts — one
    transaction or nothing; ``dry_run`` runs the whole pipeline and rolls
    back. Returns the receipt every UI shows."""
    if kind not in IMPORT_KINDS:
        raise ValidationError(
            f"dataexchange: unknown import kind {kind!r} — use one of: {', '.join(IMPORT_KINDS)}"
        )

    # ---- the bank kind: ADR-026's own machinery, unchanged -------------
    if kind == "bank":
        from .banking import import_statement, resolve_bank_ledger

        if ledger_id is None:
            raise ValidationError(
                "dataexchange: bank imports need ledger_id (the bank ledger the "
                "statement belongs to)"
            )
        if fmt not in ("csv", "mt940"):
            raise ValidationError("dataexchange: bank statements arrive as csv or mt940")
        ledger = resolve_bank_ledger(session, book, ledger_id)
        receipt = import_statement(
            session,
            book=book,
            ledger=ledger,
            fmt=fmt,
            text=content,
            source=source,
            dry_run=dry_run,
        )
        receipt["kind"] = "bank"
        receipt["dry_run"] = dry_run
        return receipt

    rows = decode_tabular(fmt, content)
    fingerprint = _fingerprint(kind, fmt, content)

    emissions: list[dict] = []
    with import_transaction(session, dry_run=dry_run) as inner:
        # the book row, seen through the import's own transaction
        import_book = inner.get(Book, book.id)
        assert import_book is not None
        if kind == "masters":
            receipt = import_masters(inner, book=import_book, rows=rows)
        elif kind == "openings":
            receipt, emissions = import_openings(
                inner,
                book=import_book,
                manager=manager,
                settings=settings,
                rows=rows,
                fingerprint=fingerprint,
                balance_to=balance_to,
            )
        elif kind == "vouchers":
            receipt, emissions = import_vouchers(
                inner,
                book=import_book,
                manager=manager,
                settings=settings,
                rows=rows,
                fingerprint=fingerprint,
            )
        else:  # stock
            receipt = import_stock(inner, book=import_book, rows=rows)

    # after-commit events — only after the outer commit (ADR-030); a
    # dry run emits nothing (nothing landed)
    if not dry_run:
        for payload in emissions:
            manager.emit("voucher.posted", payload)

    receipt.update({"kind": kind, "source": source, "dry_run": dry_run})
    return receipt
