"""Plugins API: what loaded, and what was refused (ADR-016).

Plugin inventory is operator territory — the trust model's visibility
half. ``GET /api/v1/plugins`` answers, for the running server: which
packs loaded, through which channel (first-party/entry-point vs
drop-in), on what trust basis (trusted/signed/acknowledged), and — just
as important — which plugins were **refused** at boot and why. A refused
plugin that stays refused is the system working; a refused plugin nobody
noticed is not.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import AdminDep

router = APIRouter(prefix="/plugins", tags=["plugins"])


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
