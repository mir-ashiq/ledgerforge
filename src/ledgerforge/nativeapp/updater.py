"""The update controller — the updater's visible half (ADR-031).

Owns no UI of consequence: a check on boot (when enabled), a quiet
download of the staged update, and words for the status bar. All the
protocol lives in :mod:`ledgerforge.updating` (verified manifests,
hash-checked staging, apply-on-relaunch); this class only marshals it off
the GUI thread — the ADR-022 discipline, ``tasks.run`` included — and
never interrupts bookkeeping with a dialog. A delta that fails to download
falls back to the full package without a word.

Configuration (the same ``LF_`` convention as the engine): ``LF_UPDATE_URL``
(override the default GitHub manifest), ``LF_UPDATE_CHECKS=0`` (disable the
automatic check; the Help menu still works). Egress is exactly one manifest
fetch plus one artifact download per release, to the URL configured —
nothing else, ever.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from .. import __version__, updating


def read_config() -> dict:
    """The updater's environment: url, whether boot checks run."""

    return {
        "url": os.environ.get("LF_UPDATE_URL", "").strip() or updating.DEFAULT_UPDATE_URL,
        "checks": os.environ.get("LF_UPDATE_CHECKS", "1").strip().lower()
        not in ("0", "false", "no", "off"),
    }


class UpdateController:
    """Check, verify, download and stage an update in the background;
    report in words. Results arrive on the GUI thread through
    ``tasks.run`` callbacks, matching every other page."""

    def __init__(self, data_dir: Path | str, on_status: Callable[[str], None] | None = None):
        self._data_dir = Path(data_dir)
        self._on_status: Callable[[str], None] = on_status or (lambda text: None)
        self._config = read_config()

    # ------------------------------------------------------------ the flow
    def check_on_boot(self) -> None:
        """The automatic check — only when the user has not disabled it."""

        if not self._config["checks"]:
            staged = updating.pending_update(self._data_dir)
            if staged:
                self._on_status(f"update {staged['to']} staged — it applies when you restart")
            return
        self.check()

    def check(self) -> None:
        """Fetch the manifest, verify it, compare versions, download (delta
        preferred, full as the fallback), and stage — all off-thread."""

        from .tasks import run  # imported lazily: pulls Qt into the process

        url = self._config["url"]

        def work():
            raw = updating.http_fetch(url, timeout=20)
            signature = updating.http_fetch(updating.signature_url(url), timeout=20)
            manifest = updating.verify_manifest(raw, signature)
            plan = updating.check(manifest, __version__)
            if plan is None:
                return None
            try:
                updating.download(plan, self._data_dir)
            except updating.UpdateError:
                if plan.kind != "delta":
                    raise
                full = updating.check(manifest, __version__, allow_delta=False)
                if full is None:
                    raise
                updating.download(full, self._data_dir)
            return plan

        run(work, self._checked, self._failed)

    # ---------------------------------------------------------- callbacks
    def _checked(self, plan: updating.Plan | None) -> None:
        if plan is None:
            staged = updating.pending_update(self._data_dir)
            if staged:
                self._on_status(f"update {staged['to']} staged — it applies when you restart")
            else:
                self._on_status(f"LedgerForge {__version__} is up to date")
        else:
            self._on_status(f"update {plan.to_version} downloaded — it applies when you restart")

    def _failed(self, error: object) -> None:
        # a failed update check is never a failed app: say it and move on
        self._on_status(f"update check unavailable ({getattr(error, 'detail', error)})")
