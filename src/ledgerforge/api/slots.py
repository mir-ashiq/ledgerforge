"""Slots API: the host UI's slot registry and the sandboxed documents
(ADR-019, plugin API 0.7).

``GET /api/v1/slots`` lists every slot a loaded pack declares; the web UI
turns each into a palette command. ``GET /api/v1/slots/{key}`` serves an
inline-HTML slot's document for iframe embedding — with ``CSP: sandbox`` so
the browser treats it as untrusted even if an embedder forgets the sandbox
attribute. The slot document runs with an opaque origin (no cookies, no
localStorage, no host DOM); it speaks the REST API through the host's
postMessage relay, never directly.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from ..errors import NotFoundError
from . import ReaderDep

router = APIRouter(prefix="/slots", tags=["slots"])


class SlotOut(BaseModel):
    key: str
    title: str
    #: The owning pack, and its channel/trust basis (visibility parity with
    #: the plugins screen — an operator should know who wrote each panel).
    plugin: str
    source: str
    trust: str
    #: None for inline-HTML slots (the host serves them at /slots/{key});
    #: an http(s) URL the host embeds directly otherwise.
    url: str | None


class SlotsOut(BaseModel):
    slots: list[SlotOut]


@router.get("", response_model=SlotsOut)
def list_slots(request: Request, _: ReaderDep) -> SlotsOut:
    """Every declared UI slot, in load order (ADR-019)."""
    manager = request.app.state.plugin_manager
    return SlotsOut(
        slots=[
            SlotOut(
                key=spec.key,
                title=spec.title,
                plugin=record.manifest.name,
                source=record.source,
                trust=record.trust,
                url=spec.url,
            )
            for record, spec in manager.slot_specs()
        ]
    )


@router.get("/{key}")
def slot_document(key: str, request: Request, _: ReaderDep) -> Response:
    """The sandboxed document for an inline-HTML slot (ADR-019).

    Served with ``Content-Security-Policy: sandbox allow-scripts`` — the
    same posture the web UI's iframe enforces — and ``nosniff``, so the
    document is treated as untrusted sandbox content by the browser no
    matter how it is opened. URL slots are not served here (the host
    embeds their URL directly).
    """
    manager = request.app.state.plugin_manager
    for _record, spec in manager.slot_specs():
        if spec.key == key:
            if spec.html is None:
                raise NotFoundError(f"slot {key!r} is served from its own URL, not by the host")
            return Response(
                content=spec.html,
                media_type="text/html; charset=utf-8",
                headers={
                    "Content-Security-Policy": "sandbox allow-scripts",
                    "X-Content-Type-Options": "nosniff",
                },
            )
    known = ", ".join(sorted(spec.key for _r, spec in manager.slot_specs())) or "none declared"
    raise NotFoundError(f"No slot registered for {key!r} (known: {known})")
