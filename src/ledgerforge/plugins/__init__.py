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
        register(self.registry)
        self.plugins.append(LoadedPlugin(manifest=manifest, source=source, trust=trust))
        logger.info("loaded plugin %s %s (%s, %s)", manifest.name, manifest.version, source, trust)

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
        for raw in filter(None, [p.strip() for p in extra_path.split(":")]):
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
