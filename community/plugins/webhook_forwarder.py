"""Webhook forwarder — the community index's demonstration plugin.

The after-commit event tier (plugin guide, "the two tiers"): every
``voucher.posted`` event is POSTed as JSON to the webhook configured in
``LF_WEBHOOK_URL``. This is the e-invoicing-bridge / BI-sync pattern in
miniature — code that never touches the posting transaction, where a
failure can never break a posting (the runtime isolates handler errors
after commit).

This module is also the worked example of *publishing*: it is the sample
entry in the community plugin index (``community/plugins/index.json``),
signed by the curator's publisher key exactly the way a third-party
release would be. Install it with::

    ledgerforge plugins install webhook-forwarder --path ./plugins
    LF_PLUGIN_PATH=./plugins make dev
"""

from __future__ import annotations

import logging
import os

import httpx

from ledgerforge.plugins import PluginManifest

logger = logging.getLogger("ledgerforge.plugins.webhook_forwarder")

PLUGIN = PluginManifest(name="webhook-forwarder", version="0.1.0", api_compat="0.2")

#: The last events forwarded (observability for tests/demos).
FORWARDED: list[dict] = []


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.on("voucher.posted", forward)


def forward(event) -> None:  # noqa: ANN001 — DomainEvent, kept loose for API 0.2
    url = os.environ.get("LF_WEBHOOK_URL")
    if not url:
        return  # not configured: nothing to do, and nothing to complain about
    payload = {"event": event.name, "occurred_at": event.occurred_at.isoformat(), **event.data}
    try:
        response = httpx.post(url, json=payload, timeout=5.0)
        FORWARDED.append(
            {"url": url, "status": response.status_code, "voucher_no": payload.get("voucher_no")}
        )
        logger.info("forwarded %s to %s (HTTP %s)", event.name, url, response.status_code)
    except httpx.HTTPError as exc:  # the runtime isolates us, but we stay quiet anyway
        logger.warning("webhook %s did not take the event: %s", url, exc)
