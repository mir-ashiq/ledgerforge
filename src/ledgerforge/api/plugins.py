"""Plugins API: what loaded, what was refused (ADR-016), and the
inbound webhook seam (plugin API 0.11, ADR-026).

Plugin inventory is operator territory — the trust model's visibility
half. ``GET /api/v1/plugins`` answers, for the running server: which
packs loaded, through which channel (first-party/entry-point vs
drop-in), on what trust basis (trusted/signed/acknowledged), and — just
as important — which plugins were **refused** at boot and why. A refused
plugin that stays refused is the system working; a refused plugin nobody
noticed is not.

``POST /api/v1/hooks/{key}`` is the banking gateway seam generalised:
the kernel owns the route, the book scoping and the role gate; a plugin
owns the payload. A per-bank adapter is a drop-in behind the ADR-016
trust gate claiming its own key — the kernel never learns a bank's name
(ADR-026 D3).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..errors import NotFoundError
from ..plugins import Resolver
from . import AdminDep, BookDep, BookkeeperDep, SessionDep

router = APIRouter(prefix="/plugins", tags=["plugins"])
#: The inbound webhook seam (plugin API 0.11, ADR-026) — its own
#: prefix: a hook is not plugin inventory, it is a plugin's claimed
#: HTTP surface.
hooks_router = APIRouter(prefix="/hooks", tags=["hooks"])


class PluginOut(BaseModel):
    name: str
    version: str
    api_compat: str
    source: str  # "entry-point" (first-party or pip) | "drop-in"
    trust: str  # "trusted" | "signed" | "acknowledged"


class PluginRefusalOut(BaseModel):
    module: str
    reason: str


class PluginsOut(BaseModel):
    plugins: list[PluginOut]
    refused: list[PluginRefusalOut]


@router.get("", response_model=PluginsOut)
def list_plugins(request: Request, principal: AdminDep) -> PluginsOut:
    manager = request.app.state.plugin_manager
    return PluginsOut(
        plugins=[
            PluginOut(
                name=record.manifest.name,
                version=record.manifest.version,
                api_compat=record.manifest.api_compat,
                source=record.source,
                trust=record.trust,
            )
            for record in manager.plugins
        ],
        refused=[PluginRefusalOut(module=r.module, reason=r.reason) for r in manager.refusals],
    )


# ------------------------------------------------------------- webhook seam
@hooks_router.post("/{key}")
def call_hook(
    key: str,
    payload: dict[str, Any],
    request: Request,
    session: SessionDep,
    book: BookDep,
    _: BookkeeperDep,
):
    """Deliver a payload to the plugin that claimed ``key`` (plugin API
    0.11, ADR-026): the handler runs with a book-scoped ``Resolver`` in
    this request's transaction and its dict is the response. Webhooks
    WRITE (the gateway statement import does), hence the bookkeeper role;
    a real bank's gateway credentials belong to the adapter or a proxy in
    front of it, never to the kernel (D3)."""
    handler = request.app.state.plugin_manager.registry.webhooks.get(key)
    if handler is None:
        known = ", ".join(sorted(request.app.state.plugin_manager.registry.webhooks)) or "none"
        raise NotFoundError(f"No plugin claimed the hook {key!r} (claimed: {known})")
    return handler(Resolver(session, book), payload)
