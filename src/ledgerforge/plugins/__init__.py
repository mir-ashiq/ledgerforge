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

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import PLUGIN_API_VERSION
from ..errors import ValidationError
from ..models import Group, Ledger

logger = logging.getLogger("ledgerforge.plugins")

#: Hook signatures
Transformer = Callable[["VoucherDraft", "Resolver"], "VoucherDraft"]
Validator = Callable[["VoucherDraft", "Resolver"], None]
EventHandler = Callable[["DomainEvent"], None]


def _api_compatible(declared: str) -> bool:
    """A plugin declares the plugin-API minor it targets (e.g. '0.1')."""
    supported = PLUGIN_API_VERSION.split(".")[:2]
    try:
        target = declared.split(".")[:2]
    except AttributeError:
        return False
    return target == supported


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


class Resolver:
    """Inside-transaction helper passed to hooks: look up (or safely create)
    masters without breaking the posting transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def ledger(self, *, id: int | None = None, name: str | None = None) -> Ledger | None:
        stmt = select(Ledger)
        if id is not None:
            stmt = stmt.where(Ledger.id == id)
        elif name is not None:
            stmt = stmt.where(Ledger.name == name)
        else:
            return None
        return self._session.scalar(stmt)

    def group(self, name: str) -> Group | None:
        return self._session.scalar(select(Group).where(Group.name == name))

    def ensure_ledger(self, name: str, group_name: str) -> Ledger:
        """Return the named ledger, creating it under ``group_name`` if absent."""
        existing = self.ledger(name=name)
        if existing is not None:
            return existing
        group = self.group(group_name)
        if group is None:
            raise ValidationError(f"Plugin: group {group_name!r} does not exist")
        ledger = Ledger(name=name, group_id=group.id)
        self._session.add(ledger)
        self._session.flush()
        return ledger


class PluginRegistry:
    """The only sanctioned integration surface (semver: PLUGIN_API_VERSION)."""

    def __init__(self) -> None:
        self.transformers: list[Transformer] = []
        self.validators: list[Validator] = []
        self.handlers: dict[str, list[EventHandler]] = {}

    def register_transformer(self, fn: Transformer) -> None:
        self.transformers.append(fn)

    def register_validator(self, fn: Validator) -> None:
        self.validators.append(fn)

    def on(self, event: str, handler: EventHandler) -> None:
        self.handlers.setdefault(event, []).append(handler)


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
