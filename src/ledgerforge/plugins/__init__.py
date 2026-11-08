"""LedgerForge plugin runtime (ADR-003).

Two tiers:
- In-process hooks (transformers, validators) run **inside** the posting
  transaction. They may veto or enrich a posting; they can never corrupt one
  (the engine re-validates the financial laws after every hook).
- Domain events (``voucher.posted``, ``master.created``) fire **after commit**;
  handler failures are logged and isolated.

Plugins are discovered via the ``ledgerforge.plugins`` entry-point group or a
drop-in directory on ``LF_PLUGIN_PATH``. See docs/plugin-guide.md.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import PLUGIN_API_VERSION
from ..banking import BankLineRow
from ..errors import ValidationError
from ..models import Book, Group, Ledger, Voucher, VoucherEntry, VoucherType
from .trust import (
    TrustError,
    TrustStore,
    VerifiedFacts,
    fingerprint,
    sha256_hex,
    static_manifest_facts,
    verify_signature,
)

logger = logging.getLogger("ledgerforge.plugins")

#: Hook signatures
Transformer = Callable[["VoucherDraft", "Resolver"], "VoucherDraft"]
Validator = Callable[["VoucherDraft", "Resolver"], None]
EventHandler = Callable[["DomainEvent"], None]
DocumentViewFactory = Callable[["DocumentContext"], "DocumentView | None"]
PackReportFactory = Callable[["Resolver", "dict[str, str]"], "PackReport"]
#: An inbound HTTP hook a pack claims (plugin API 0.11, ADR-026): served at
#: ``POST /api/v1/hooks/{key}`` — book-scoped by the standard header,
#: bookkeeper role (webhooks write) — the handler runs in the request
#: transaction with a book-scoped ``Resolver`` and returns the JSON body of
#: the response. The banking gateway seam: a per-bank adapter is a drop-in
#: that claims its own key and translates its bank's payload; the kernel
#: never learns a bank's name (ADR-026 D3).
WebhookHandler = Callable[["Resolver", dict[str, Any]], "dict[str, Any]"]


def _api_compatible(declared: str) -> bool:
    """A plugin declares the plugin-API minor it targets (e.g. '0.2').
    The declaration is a **floor, not a pin** (ADR-011): a 0.2-era plugin
    loads on a 0.3 build — additions are backwards compatible — while a
    0.3 plugin refuses on a 0.2 build, loudly, never silently misbehaving."""
    supported = PLUGIN_API_VERSION.split(".")[:2]
    try:
        target = declared.split(".")[:2]
    except AttributeError:
        return False
    if len(target) != 2 or len(supported) != 2:
        return False
    try:
        t_major, t_minor = int(target[0]), int(target[1])
        s_major, s_minor = int(supported[0]), int(supported[1])
    except ValueError:
        return False
    return t_major == s_major and t_minor <= s_minor


@dataclass(frozen=True)
class SlotSpec:
    """One sandboxed UI slot a pack declares (plugin API 0.7, ADR-019).

    Exactly one of ``html`` / ``url``: inline HTML is served by the host at
    ``GET /api/v1/slots/{key}`` (the web UI embeds it in a hardened iframe
    — ``sandbox="allow-scripts"``, plus a CSP ``sandbox`` header on the
    response); a URL is embedded directly. Either way the slot is UNTRUSTED
    surface: it speaks the versioned REST API through the host's
    postMessage relay, never receives credentials, and never receives
    keyboard scope — it registers commands with the host and the host binds
    keys to them.
    """

    key: str
    title: str
    html: str | None = None
    url: str | None = None

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self.key):
            raise ValueError(f"slot key {self.key!r} must be a short lowercase slug (a-z, 0-9, -)")
        if not self.title or len(self.title) > 120:
            raise ValueError(f"slot {self.key!r} needs a title (1-120 chars)")
        if (self.html is None) == (self.url is None):
            raise ValueError(f"slot {self.key!r} must declare exactly one of html / url")
        if self.html is not None:
            if len(self.html.encode()) > 131_072:
                raise ValueError(
                    f"slot {self.key!r} inline HTML exceeds 128 KiB — host it and use url="
                )
        elif not str(self.url).startswith(("http://", "https://")):
            raise ValueError(f"slot {self.key!r} url must be http(s)")


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    api_compat: str
    #: Sandboxed UI slots (plugin API 0.7, ADR-019) — manifest-declared,
    #: host-rendered, keyboard-scope-free.
    slots: tuple[SlotSpec, ...] = ()


#: Why a loaded plugin was allowed to register (ADR-016):
#: - ``trusted`` — the operator's own channel: first-party packs or pip
#:   entry-points (code the operator chose to install);
#: - ``signed`` — a drop-in with a valid detached signature by a publisher
#:   approved in the drop-in directory's trust store;
#: - ``acknowledged`` — a drop-in whose exact bytes the operator pinned
#:   after review (``ledgerforge plugins ack``).
LoadedTrust = Literal["trusted", "signed", "acknowledged"]


@dataclass(frozen=True)
class LoadedPlugin:
    """One plugin that loaded — its manifest, its channel, its trust basis."""

    manifest: PluginManifest
    source: Literal["entry-point", "drop-in"]
    trust: LoadedTrust


@dataclass(frozen=True)
class PluginRefusal:
    """One plugin (or directory) that did not load, and why."""

    module: str
    reason: str


def _import_plugin_file(path: Path) -> Any:
    """Import a drop-in module by file location — the directory is never on
    ``sys.path``, so a plugin cannot shadow a real package (and vice versa)."""
    spec = importlib.util.spec_from_file_location(f"ledgerforge.dropin.{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import plugin file {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class DraftCostAllocation:
    """One centre × amount split riding a :class:`DraftLine` (plugin API
    0.10, ADR-025). ``dc`` is NOT carried here — it is inherited from the
    line (allocations preserve the entry's Dr/Cr; the database enforces
    it). Transformers that split or rebuild lines must carry the line's
    allocations along, split to the new amounts — the engine re-validates
    Σallocation = amount on every line after every hook."""

    centre_id: int | None = None
    centre_name: str | None = None
    amount: Decimal = Decimal(0)


@dataclass
class DraftLine:
    """One voucher line while still a draft (before ledger resolution).

    FX snapshot (plugin API 0.2, issue #4): ``currency``/``amount_fx``/
    ``fx_rate`` are all-or-none; ``amount`` is always the base-currency
    figure. Transformers that rebuild or split lines must either keep the
    trio intact or drop it — an inconsistent trio is rejected by the engine
    (and by the database) even if a plugin produced it.

    Cost allocations (plugin API 0.10, ADR-025): optional per line; when
    present, their sum must equal ``amount`` exactly after every hook —
    a line is fully allocated or left unallocated, never partly.
    """

    dc: Literal["D", "C"]
    amount: Decimal
    ledger_id: int | None = None
    ledger_name: str | None = None
    narration: str | None = None
    currency: str | None = None
    amount_fx: Decimal | None = None
    fx_rate: Decimal | None = None
    cost_allocations: list[DraftCostAllocation] = field(default_factory=list)


@dataclass
class VoucherDraft:
    """Mutable-by-transformers voucher draft."""

    voucher_type: str
    voucher_date: date
    lines: list[DraftLine]
    narration: str | None = None
    client_token: UUID | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    reversal_of: int | None = None

    def with_lines(self, lines: list[DraftLine]) -> VoucherDraft:
        return replace(self, lines=lines)


@dataclass(frozen=True)
class DomainEvent:
    name: str
    occurred_at: datetime
    data: dict[str, Any]


@dataclass(frozen=True)
class EntryLine:
    """One posted voucher entry as seen by a document-view factory (API
    0.5, ADR-013): the flat print-side projection of a ``VoucherEntry``."""

    line_no: int
    ledger_name: str
    dc: Literal["D", "C"]
    amount: Decimal
    narration: str | None = None


@dataclass(frozen=True)
class DocumentColumn:
    """One column of a document section: relative ``width`` (the row's
    columns share the content width proportionally) and alignment."""

    header: str
    width: float = 1.0
    align: Literal["left", "right"] = "left"


@dataclass(frozen=True)
class DocumentSection:
    """A titled table inside a document — components, items, entries…"""

    title: str
    columns: tuple[DocumentColumn, ...]
    rows: tuple[tuple[str, ...], ...] = ()  # pre-formatted cell strings


@dataclass(frozen=True)
class DocumentView:
    """A printable business document, described neutrally (API 0.5,
    ADR-013): *what* it says, never *where it lands on the page* — the
    layout engine in ``reporting.printing`` owns pagination and geometry,
    so every pack's documents share one visual language.

    Factories return ``None`` to decline a document (``payroll`` declines
    payouts, for instance) and the voucher-slip fallback takes over."""

    title: str
    subtitle: str = ""
    reference: str = ""  # the voucher number, top-right
    fields: tuple[tuple[str, str], ...] = ()  # header label/value pairs
    sections: tuple[DocumentSection, ...] = ()
    totals: tuple[tuple[str, str], ...] = ()  # the summary block; last row bold
    footnotes: tuple[str, ...] = ()
    filename: str = ""  # download name hint, sans extension


@dataclass(frozen=True)
class DocumentContext:
    """Everything a document-view factory may read (API 0.5, ADR-013): the
    posted voucher (immutable — Law 2), its entries, the book it belongs
    to, a book-scoped ``Resolver`` for derived reads, and the caller's
    query parameters (a payroll run needs ``employee`` to pick its
    payslip's subject)."""

    book_name: str
    voucher_no: str
    voucher_type: str
    voucher_date: date
    posted_at: datetime | None
    narration: str | None
    reversal_of: int | None
    meta: dict[str, Any]
    entries: tuple[EntryLine, ...]
    params: dict[str, str]
    resolver: Resolver


@dataclass(frozen=True)
class PackReport:
    """A pack-computed, book-scoped report (API 0.6, ADR-014; ``data`` added
    by API 0.8, ADR-023): a ``DocumentView`` renders to PDF through the
    shared print-bridge layout engine (the same visual language as every
    printed document), ``csv`` is pre-rendered text for machine consumption
    (a statutory portal's upload format), and ``data`` is a JSON payload —
    the structured rows a UI consumes (``?format=json``), the same fold
    the PDF renders from. Factories validate their own parameters and
    raise ``ValidationError`` for caller errors; the kernel stays
    report-blind exactly as it stays meta-blind."""

    title: str
    filename: str  # download name hint, sans extension
    view: DocumentView | None = None
    csv: str | None = None
    media_type: str = "text/csv"
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class PriorDocument:
    """One stored document as seen by a plugin (API 0.3, ADR-011): the
    meta payload exactly as persisted, with the provenance a fold needs —
    which voucher carried it and, for reversals, which one it undoes."""

    voucher_id: int
    voucher_no: str
    voucher_date: date
    reversal_of: int | None
    meta: dict[str, Any]


@dataclass(frozen=True)
class StockItemMaster:
    """A stock-item master as seen by a plugin (API 0.12, ADR-027): the
    tracking modes (TallyPrime's "maintain in batches / serial numbers"
    toggles) and the re-order levels, as an immutable snapshot taken
    inside the caller's transaction. ``None`` from ``stock_item`` means
    the item has no master — ADR-011's string-item freedom stands; depth
    is opted into, never demanded."""

    id: int
    name: str
    unit: str | None
    track_batches: bool
    track_serials: bool
    reorder_level: Decimal | None
    reorder_quantity: Decimal | None


@dataclass(frozen=True)
class StockBomMaster:
    """A bill of materials as seen by a plugin (API 0.12, ADR-027): the
    output item and per-run quantity, the components as
    ``(item name, qty per output unit)`` pairs in recipe order — the
    manufacturing journal's validation input, frozen at read time."""

    id: int
    name: str
    output_item: str
    output_qty: Decimal
    components: tuple[tuple[str, Decimal], ...]


@dataclass(frozen=True)
class LedgerEntryLine:
    """One posted ledger entry with its voucher's provenance (plugin API
    0.8, ADR-023): the engine-side row a ledger statement folds — amount
    and direction exactly as posted, the carrying voucher's number/date/
    reversal link, and that voucher's ``meta`` so a pack can correlate its
    own documents (bill links) with the accounting truth."""

    voucher_id: int
    voucher_no: str
    voucher_date: date
    reversal_of: int | None
    dc: Literal["D", "C"]
    amount: Decimal
    narration: str | None
    meta: dict[str, Any]


class Resolver:
    """Inside-transaction helper passed to hooks: look up (or safely
    create) masters without breaking the posting transaction. Every lookup
    and creation is scoped to the book being posted into (ADR-009): a plugin
    can never resolve — or mint — a master of another book.

    Plugin API 0.11 (ADR-026): the ``session`` and ``book`` properties are
    the **webhook handler's** handles — a hook claimed through
    ``register_webhook`` runs in a host request transaction and may call
    kernel services with them (the gateway statement import does).
    Transformers and validators never need them: they already run inside
    the posting transaction, and the engine re-proves the financial laws
    after every hook."""

    def __init__(self, session: Session, book: Book) -> None:
        self._session = session
        self._book = book

    @property
    def session(self) -> Session:
        """The transaction-scoped session this resolver wraps (plugin API
        0.11) — the webhook handler's write handle."""
        return self._session

    @property
    def book(self) -> Book:
        """The book every lookup and creation is scoped to (plugin API
        0.11) — webhook handlers pass it to kernel services."""
        return self._book

    def ledger(self, *, id: int | None = None, name: str | None = None) -> Ledger | None:
        stmt = select(Ledger).where(Ledger.book_id == self._book.id)
        if id is not None:
            stmt = stmt.where(Ledger.id == id)
        elif name is not None:
            stmt = stmt.where(Ledger.name == name)
        else:
            return None
        return self._session.scalar(stmt)

    def group(self, name: str) -> Group | None:
        return self._session.scalar(
            select(Group).where(Group.book_id == self._book.id, Group.name == name)
        )

    @property
    def fy_start_month(self) -> int:
        """The book's fiscal-year start month, 1–12 (plugin API 0.4, ADR-012).
        Read-only: the one fact a year-to-date fold needs to window its
        accumulators — the months >= ``fy_start_month`` open a fiscal year.
        Immutable once the book has vouchers, so the value a plugin reads
        inside the posting transaction is the value every prior document
        was posted under."""
        return self._book.fy_start_month

    def ensure_ledger(self, name: str, group_name: str) -> Ledger:
        """Return the named ledger of the book, creating it under
        ``group_name`` (of the same book) if absent."""
        existing = self.ledger(name=name)
        if existing is not None:
            return existing
        group = self.group(group_name)
        if group is None:
            raise ValidationError(
                f"Plugin: group {group_name!r} does not exist in book {self._book.name!r}"
            )
        ledger = Ledger(book_id=self._book.id, name=name, group_id=group.id)
        self._session.add(ledger)
        self._session.flush()
        return ledger

    def ensure_voucher_type(
        self, code: str, name: str, prefix: str | None = None, *, restart_yearly: bool = True
    ) -> VoucherType:
        """Return the book's voucher type ``code``, creating it if absent
        (plugin API 0.9, ADR-024) — book-scoped and idempotent, the
        ``ensure_ledger`` pattern applied to document numbering. The engine
        resolves a draft's voucher type *after* transformers run, so a
        transformer may rewrite ``draft.voucher_type`` to the returned type's
        code and its documents number in their own gapless series
        (``prefix-FY-seq``, yearly when ``restart_yearly``)."""
        code = code.strip().upper()
        existing = self._session.scalar(
            select(VoucherType).where(
                VoucherType.book_id == self._book.id, VoucherType.code == code
            )
        )
        if existing is not None:
            return existing
        vtype = VoucherType(
            book_id=self._book.id,
            code=code,
            name=name,
            number_prefix=prefix if prefix is not None else code,
            restart_yearly=restart_yearly,
            is_system=False,
        )
        self._session.add(vtype)
        self._session.flush()
        return vtype

    def prior_documents(self, key: str) -> list[PriorDocument]:
        """Every posted voucher of this book whose ``meta`` carries ``key``,
        plus every voucher reversing one of those, oldest first (API 0.3,
        ADR-011). The kernel stays meta-blind: payloads are returned exactly
        as stored, and interpretation belongs to the plugin. Reversals ride
        along with empty meta and ``reversal_of`` set so a fold can net an
        undone document against its correction."""
        carried = [
            PriorDocument(*row)
            for row in self._session.execute(
                select(
                    Voucher.id,
                    Voucher.voucher_no,
                    Voucher.voucher_date,
                    Voucher.reversal_of,
                    Voucher.meta,
                ).where(Voucher.book_id == self._book.id, Voucher.meta.has_key(key))
            )
        ]
        if not carried:
            return []
        carried_ids = {d.voucher_id for d in carried}
        reversals = [
            PriorDocument(*row)
            for row in self._session.execute(
                select(
                    Voucher.id,
                    Voucher.voucher_no,
                    Voucher.voucher_date,
                    Voucher.reversal_of,
                    Voucher.meta,
                ).where(
                    Voucher.book_id == self._book.id,
                    Voucher.reversal_of.in_(carried_ids),
                )
            )
        ]
        return sorted(carried + reversals, key=lambda d: d.voucher_id)

    def ledger_entries(self, ledger_id: int) -> list[LedgerEntryLine]:
        """Every posted entry of this book on one ledger, oldest first
        (plugin API 0.8, ADR-023). The engine-side half of a ledger
        statement: rows are returned exactly as posted (reversal vouchers
        included as their own rows, ``reversal_of`` set — the caller decides
        how to present them), with the carrying voucher's ``meta`` so a
        pack can join its documents to the accounting lines. Book-scoped
        like every Resolver read: a ledger of another book answers empty."""
        ledger = self._session.get(Ledger, ledger_id)
        if ledger is None or ledger.book_id != self._book.id:
            return []
        rows = self._session.execute(
            select(
                VoucherEntry.voucher_id,
                Voucher.voucher_no,
                Voucher.voucher_date,
                Voucher.reversal_of,
                VoucherEntry.dc,
                VoucherEntry.amount,
                VoucherEntry.narration,
                Voucher.meta,
            )
            .join(Voucher, Voucher.id == VoucherEntry.voucher_id)
            .where(VoucherEntry.ledger_id == ledger_id)
            .order_by(Voucher.voucher_date, Voucher.id, VoucherEntry.line_no)
        )
        return [
            LedgerEntryLine(
                voucher_id=r.voucher_id,
                voucher_no=r.voucher_no,
                voucher_date=r.voucher_date,
                reversal_of=r.reversal_of,
                dc=r.dc,
                amount=r.amount,
                narration=r.narration,
                meta=r.meta or {},
            )
            for r in rows
        ]

    def lock(self, name: str) -> None:
        """Transaction-scoped advisory lock on ``name`` (API 0.3, ADR-011),
        namespaced to this book — the same serialization discipline the
        engine uses for numbering counters, available to plugins that need
        to serialize on a name (a stock item, an invoice series, …).
        Released automatically at commit/rollback. Deadlock discipline is
        the caller's: lock a voucher's names in one sorted pass."""
        self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"{self._book.id}:{name}", 0)))
        )

    def bank_lines(self, ledger_id: int) -> list[BankLineRow]:
        """Every imported bank-statement line of one ledger with its live
        (or last) match state (plugin API 0.11, ADR-026) — the banking
        pack's fold input: the cheque register derives *presented* from
        lines and *cleared* from live matches, and the reconciliation
        report reads the same rows the UIs list. Book-scoped like every
        Resolver read: another book's ledger answers empty."""
        from ..banking import bank_lines as _bank_lines

        return _bank_lines(self._session, book=self._book, ledger_id=ledger_id)

    # ------------------------------------------- stock masters (API 0.12)
    def stock_item(self, *, name: str) -> StockItemMaster | None:
        """The stock-item master a document names, or None (plugin API
        0.12, ADR-027) — the ``ledger``/``group`` discipline applied to
        stock masters. Book-scoped; an immutable snapshot; ``None`` keeps
        ADR-011's string-item freedom (no master, no demands)."""
        from ..inventory import item_master_by_name

        row = item_master_by_name(self._session, self._book, str(name).strip())
        if row is None:
            return None
        return StockItemMaster(
            id=row.id,
            name=row.name,
            unit=row.unit,
            track_batches=bool(row.track_batches),
            track_serials=bool(row.track_serials),
            reorder_level=row.reorder_level,
            reorder_quantity=row.reorder_quantity,
        )

    def stock_items(self) -> list[StockItemMaster]:
        """Every stock-item master of the book, name order (plugin API
        0.12, ADR-027) — the shortfall report's fold input."""
        from sqlalchemy import select as _select

        from ..models import StockItem

        rows = self._session.scalars(
            _select(StockItem).where(StockItem.book_id == self._book.id).order_by(StockItem.name)
        ).all()
        return [
            StockItemMaster(
                id=r.id,
                name=r.name,
                unit=r.unit,
                track_batches=bool(r.track_batches),
                track_serials=bool(r.track_serials),
                reorder_level=r.reorder_level,
                reorder_quantity=r.reorder_quantity,
            )
            for r in rows
        ]

    def stock_bom(self, *, name: str) -> StockBomMaster | None:
        """The bill of materials a manufacturing journal names, with its
        components resolved to names (plugin API 0.12, ADR-027) — the
        recipe the journal validates against. Book-scoped; None when the
        book knows no such BOM (an explicit journal needs no recipe)."""
        from ..inventory import bom_by_name, bom_components
        from ..models import StockItem

        bom = bom_by_name(self._session, self._book, str(name).strip())
        if bom is None:
            return None
        output = self._session.get(StockItem, bom.output_item_id)
        return StockBomMaster(
            id=bom.id,
            name=bom.name,
            output_item=output.name if output else "",
            output_qty=bom.output_qty,
            components=tuple(
                (item_name, qty) for _id, item_name, qty in bom_components(self._session, bom)
            ),
        )

    def stock_price(
        self, item: str, party: str | None = None, on: date | None = None
    ) -> Decimal | None:
        """The effective price-list rate for ``(item, party, date)``
        (plugin API 0.12, ADR-027): the latest ``effective_from <= on``
        among the applicable rows, a party-specific row beating the
        generic list price at the same date. ``None`` is the honest "no
        suggestion" — suggestions never commit (the matchmaker's law);
        the selling rate stays the operator's explicit entry."""
        from ..inventory import effective_price, item_master_by_name

        master = item_master_by_name(self._session, self._book, str(item).strip())
        if master is None:
            return None
        party_id = None
        if party:
            ledger = self.ledger(name=str(party).strip())
            if ledger is None:
                return None  # unknown party: no price applies
            party_id = ledger.id
        return effective_price(self._session, self._book, master, party_id, on)


class PluginRegistry:
    """The only sanctioned integration surface (semver: PLUGIN_API_VERSION)."""

    def __init__(self) -> None:
        self.transformers: list[Transformer] = []
        self.validators: list[Validator] = []
        self.handlers: dict[str, list[EventHandler]] = {}
        self.document_views: dict[str, DocumentViewFactory] = {}
        self.report_views: dict[str, PackReportFactory] = {}
        #: Inbound HTTP hooks, plugin API 0.11 (ADR-026): key -> handler,
        #: served by ``POST /api/v1/hooks/{key}``. One handler per key; a
        #: later registration replaces an earlier one, mirroring the
        #: document/report registries.
        self.webhooks: dict[str, WebhookHandler] = {}

    def register_transformer(self, fn: Transformer) -> None:
        self.transformers.append(fn)

    def register_validator(self, fn: Validator) -> None:
        self.validators.append(fn)

    def on(self, event: str, handler: EventHandler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def register_document_view(self, key: str, factory: DocumentViewFactory) -> None:
        """Claim the print layout for ``meta[key]`` documents (API 0.5,
        ADR-013). One factory per key — a later registration replaces an
        earlier one (last loaded wins), mirroring transformer behavior."""
        self.document_views[key] = factory

    def register_report(self, key: str, factory: PackReportFactory) -> None:
        """Publish a book-scoped report under ``key`` (API 0.6, ADR-014),
        served by ``GET /api/v1/reports/pack/{key}`` — as PDF (the shared
        layout engine) or as the pack's machine format (``PackReport.csv``).
        One factory per key; a later registration replaces an earlier one,
        mirroring the other registries."""
        self.report_views[key] = factory

    def register_webhook(self, key: str, handler: WebhookHandler) -> None:
        """Claim the inbound hook ``key`` (API 0.11, ADR-026): requests to
        ``POST /api/v1/hooks/{key}`` run ``handler(resolver, payload)`` in
        the request transaction (book-scoped, bookkeeper role) and return
        its dict. One handler per key — last loaded wins, the registries'
        rule. Webhooks WRITE (the gateway statement import does); a
        read-only integration should register a report instead."""
        self.webhooks[key] = handler


def split_plugin_path(extra_path: str) -> list[str]:
    """LF_PLUGIN_PATH entries, split on the platform's path separator.

    ``os.pathsep``, never a hard-coded ``':'``: Windows paths carry a
    drive-letter colon (``C:\\...\\plugins``) which a ``':'`` split would
    tear apart — the desktop edition (ADR-021) loads drop-ins from
    ``%LOCALAPPDATA%\\LedgerForge\\plugins``, so the separator must match
    the platform.
    """
    return [entry for entry in (part.strip() for part in extra_path.split(os.pathsep)) if entry]


class PluginManager:
    def __init__(self) -> None:
        self.registry = PluginRegistry()
        #: Everything that loaded, with its source channel and trust basis.
        self.plugins: list[LoadedPlugin] = []
        #: Everything that was refused at boot, with the reason — surfacing
        #: refusals is half the trust model's value (ADR-016).
        self.refusals: list[PluginRefusal] = []

    @property
    def loaded(self) -> list[PluginManifest]:
        """Compat view: the manifests of everything that loaded."""
        return [record.manifest for record in self.plugins]

    # ------------------------------------------------------------ loading
    def load_module(
        self,
        module: Any,
        *,
        source: Literal["entry-point", "drop-in"] = "entry-point",
        trust: LoadedTrust = "trusted",
    ) -> None:
        manifest = getattr(module, "PLUGIN", None)
        if not isinstance(manifest, PluginManifest):
            raise RuntimeError(f"{module!r} exposes no PLUGIN manifest")
        if not _api_compatible(manifest.api_compat):
            raise RuntimeError(
                f"Plugin {manifest.name} targets plugin API {manifest.api_compat}; "
                f"this build supports {PLUGIN_API_VERSION} — refusing to load"
            )
        if any(record.manifest.name == manifest.name for record in self.plugins):
            raise RuntimeError(
                f"plugin {manifest.name} is already loaded — refusing the duplicate "
                "(same pack installed twice? uninstall one)"
            )
        register = getattr(module, "register", None)
        if not callable(register):
            raise RuntimeError(f"Plugin {manifest.name} exposes no register()")
        for spec in manifest.slots:
            try:
                spec.validate()
            except ValueError as exc:
                raise RuntimeError(
                    f"Plugin {manifest.name} declares an invalid slot: {exc}"
                ) from exc
            if any(
                other.manifest.name != manifest.name
                and any(s.key == spec.key for s in other.manifest.slots)
                for other in self.plugins
            ):
                raise RuntimeError(
                    f"Plugin {manifest.name} declares slot {spec.key!r} which another "
                    "loaded pack already owns — refusing the duplicate"
                )
        register(self.registry)
        self.plugins.append(LoadedPlugin(manifest=manifest, source=source, trust=trust))
        logger.info("loaded plugin %s %s (%s, %s)", manifest.name, manifest.version, source, trust)

    def slot_specs(self) -> list[tuple[LoadedPlugin, SlotSpec]]:
        """Every declared UI slot with its owning pack (ADR-019) — the
        host's slot registry, in load order."""
        return [(record, spec) for record in self.plugins for spec in record.manifest.slots]

    # ------------------------------------------------------- the trust gate
    @staticmethod
    def _gate_dropin(py_file: Path, store: TrustStore) -> VerifiedFacts | None:
        """ADR-016: decide whether a drop-in may load — **before importing it**.

        Returns the signed facts (the module may load as *signed*), or
        ``None`` (the operator pinned these exact bytes — *acknowledged*).
        Raises :class:`TrustError` when the plugin must be refused: unsigned,
        tampered, by an unapproved publisher, or signed for a different
        release than the one on disk.
        """
        digest = sha256_hex(py_file.read_bytes())
        if store.acknowledgement_matches(py_file.stem, digest):
            return None
        sig_path = py_file.with_name(py_file.name + ".sig")
        if not sig_path.exists():
            raise TrustError(
                "no operator acknowledgement in .trust.json and no detached signature "
                f"({py_file.name}.sig) — see `ledgerforge plugins ack` / `install`"
            )
        try:
            sig_doc = json.loads(sig_path.read_text())
        except json.JSONDecodeError as exc:
            raise TrustError(f"{sig_path.name} is not valid JSON: {exc}") from exc
        facts = verify_signature(sig_doc, py_file.read_bytes())
        if not store.is_approved(facts.public_key):
            raise TrustError(
                f"signature is valid but publisher {fingerprint(facts.public_key)} "
                "is not approved in .trust.json — approve only keys you have verified"
            )
        static = static_manifest_facts(py_file)
        if static is None:
            raise TrustError(
                "PLUGIN manifest is not statically readable (computed or conditional); "
                "a signature cannot pin what the code will declare — acknowledge it instead"
            )
        if (static["name"], static["version"], static["api_compat"]) != (
            facts.name,
            facts.version,
            facts.api_compat,
        ):
            raise TrustError(
                f"signed as {facts.name} {facts.version} (API {facts.api_compat}) but the "
                f"module declares {static['name']} {static['version']} (API "
                f"{static['api_compat']}) — not the release that was signed"
            )
        return facts

    def discover(self, extra_path: str = "") -> None:
        """Entry-point plugins (trusted by installation) + drop-in directories.

        A drop-in module loads only through the ADR-016 gate: a detached
        signature by an approved publisher (checked against the artifact
        bytes and the statically-extracted manifest facts *before the first
        line of plugin code executes*), or an operator acknowledgement
        pinning the exact bytes. Everything else is refused loudly and
        recorded in :attr:`refusals` — never silently ignored, never loaded.
        """
        for ep in entry_points(group="ledgerforge.plugins"):
            try:
                self.load_module(ep.load())
            except Exception as exc:  # noqa: BLE001 — a bad plugin must not kill boot
                logger.exception("failed to load plugin %s", ep.name)
                self.refusals.append(PluginRefusal(module=ep.name, reason=str(exc)))
        for raw in split_plugin_path(extra_path):
            base = Path(raw)
            if not base.is_dir():
                logger.warning("LF_PLUGIN_PATH entry %s is not a directory", base)
                continue
            try:
                store = TrustStore.load(base)
            except TrustError as exc:
                logger.error("refusing all drop-ins in %s: %s", base, exc)
                self.refusals.append(PluginRefusal(module=str(base), reason=str(exc)))
                continue
            for py_file in sorted(base.glob("*.py")):
                module_name = py_file.stem
                if module_name.startswith("_"):
                    continue
                try:
                    facts = self._gate_dropin(py_file, store)
                    module = _import_plugin_file(py_file)
                    if facts is not None:  # signed: the executed manifest must match
                        manifest = getattr(module, "PLUGIN", None)
                        declared = (
                            (manifest.name, manifest.version, manifest.api_compat)
                            if isinstance(manifest, PluginManifest)
                            else None
                        )
                        if declared != (facts.name, facts.version, facts.api_compat):
                            raise TrustError(  # noqa: TRY301 — the gate's final word
                                f"module executes as {declared} but was signed as "
                                f"{(facts.name, facts.version, facts.api_compat)} — not loading it"
                            )
                    self.load_module(
                        module,
                        source="drop-in",
                        trust="signed" if facts is not None else "acknowledged",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("refused drop-in plugin %s", module_name)
                    self.refusals.append(PluginRefusal(module=module_name, reason=str(exc)))

    # ------------------------------------------------------------ execution
    def transform(self, draft: VoucherDraft, resolver: Resolver) -> VoucherDraft:
        for fn in self.registry.transformers:
            draft = fn(draft, resolver)
        return draft

    def validate(self, draft: VoucherDraft, resolver: Resolver) -> None:
        for fn in self.registry.validators:
            fn(draft, resolver)

    def emit(self, event: str, data: dict[str, Any]) -> None:
        """After-commit event dispatch. Handler failures are isolated."""
        event_obj = DomainEvent(name=event, occurred_at=datetime.now(UTC), data=data)
        for handler in self.registry.handlers.get(event, []):
            try:
                handler(event_obj)
            except Exception:  # noqa: BLE001 — events must never break posting
                logger.exception("event handler failed for %s", event)
