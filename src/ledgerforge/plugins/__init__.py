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

import importlib
import logging
import pkgutil
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
from ..errors import ValidationError
from ..models import Book, Group, Ledger, Voucher

logger = logging.getLogger("ledgerforge.plugins")

#: Hook signatures
Transformer = Callable[["VoucherDraft", "Resolver"], "VoucherDraft"]
Validator = Callable[["VoucherDraft", "Resolver"], None]
EventHandler = Callable[["DomainEvent"], None]
DocumentViewFactory = Callable[["DocumentContext"], "DocumentView | None"]
PackReportFactory = Callable[["Resolver", "dict[str, str]"], "PackReport"]


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
class PluginManifest:
    name: str
    version: str
    api_compat: str


@dataclass
class DraftLine:
    """One voucher line while still a draft (before ledger resolution).

    FX snapshot (plugin API 0.2, issue #4): ``currency``/``amount_fx``/
    ``fx_rate`` are all-or-none; ``amount`` is always the base-currency
    figure. Transformers that rebuild or split lines must either keep the
    trio intact or drop it — an inconsistent trio is rejected by the engine
    (and by the database) even if a plugin produced it.
    """

    dc: Literal["D", "C"]
    amount: Decimal
    ledger_id: int | None = None
    ledger_name: str | None = None
    narration: str | None = None
    currency: str | None = None
    amount_fx: Decimal | None = None
    fx_rate: Decimal | None = None


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
    """A pack-computed, book-scoped report (API 0.6, ADR-014): either a
    ``DocumentView`` — rendered to PDF by the shared print-bridge layout
    engine, the same visual language as every printed document — or
    pre-rendered ``csv`` text for machine consumption (a statutory
    portal's upload format). Factories validate their own parameters and
    raise ``ValidationError`` for caller errors; the kernel stays
    report-blind exactly as it stays meta-blind."""

    title: str
    filename: str  # download name hint, sans extension
    view: DocumentView | None = None
    csv: str | None = None
    media_type: str = "text/csv"


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


class Resolver:
    """Inside-transaction helper passed to hooks: look up (or safely
    create) masters without breaking the posting transaction. Every lookup
    and creation is scoped to the book being posted into (ADR-009): a plugin
    can never resolve — or mint — a master of another book."""

    def __init__(self, session: Session, book: Book) -> None:
        self._session = session
        self._book = book

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


class PluginRegistry:
    """The only sanctioned integration surface (semver: PLUGIN_API_VERSION)."""

    def __init__(self) -> None:
        self.transformers: list[Transformer] = []
        self.validators: list[Validator] = []
        self.handlers: dict[str, list[EventHandler]] = {}
        self.document_views: dict[str, DocumentViewFactory] = {}
        self.report_views: dict[str, PackReportFactory] = {}

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


class PluginManager:
    def __init__(self) -> None:
        self.registry = PluginRegistry()
        self.loaded: list[PluginManifest] = []

    # ------------------------------------------------------------ loading
    def load_module(self, module: Any) -> None:
        manifest = getattr(module, "PLUGIN", None)
        if not isinstance(manifest, PluginManifest):
            raise RuntimeError(f"{module!r} exposes no PLUGIN manifest")
        if not _api_compatible(manifest.api_compat):
            raise RuntimeError(
                f"Plugin {manifest.name} targets plugin API {manifest.api_compat}; "
                f"this build supports {PLUGIN_API_VERSION} — refusing to load"
            )
        register = getattr(module, "register", None)
        if not callable(register):
            raise RuntimeError(f"Plugin {manifest.name} exposes no register()")
        register(self.registry)
        self.loaded.append(manifest)
        logger.info("loaded plugin %s %s", manifest.name, manifest.version)

    def discover(self, extra_path: str = "") -> None:
        """Entry-point plugins + drop-in directories."""
        for ep in entry_points(group="ledgerforge.plugins"):
            try:
                self.load_module(ep.load())
            except Exception:  # noqa: BLE001 — a bad plugin must not kill boot
                logger.exception("failed to load plugin %s", ep.name)
        for raw in filter(None, [p.strip() for p in extra_path.split(":")]):
            base = Path(raw)
            if not base.is_dir():
                logger.warning("LF_PLUGIN_PATH entry %s is not a directory", base)
                continue
            for mod_info in pkgutil.iter_modules([str(base)]):
                try:
                    module = importlib.import_module(mod_info.name)
                    self.load_module(module)
                except Exception:  # noqa: BLE001
                    logger.exception("failed to load drop-in plugin %s", mod_info.name)

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
