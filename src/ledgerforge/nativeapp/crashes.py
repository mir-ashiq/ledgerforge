"""Crash capture — local by default, yours forever (ADR-031).

The pledge, structurally: a crash is *always* captured — to the user's own
data directory, as plain JSON they can read — and *sending* it anywhere
requires the operator to configure an endpoint (``LF_CRASH_ENDPOINT``) and
the user to act (the Help menu's *Crash reports…*, or the explicit
``LF_CRASH_REPORTING=1`` opt-in that sends new reports automatically). The
payload is an allowlist: version, platform, traceback, the tail of the
engine log — never book data, never the database, never the command line.
The distribution ships no collector; the default sends nothing, anywhere,
ever.
"""

from __future__ import annotations

import contextlib
import faulthandler
import json
import os
import platform as _platform
import sys
import threading
import traceback as _traceback
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

#: The keys a crash report may carry — the allowlist IS the contract; the
#: sender posts this object and nothing else, and the tests assert it.
REPORT_KEYS = (
    "app",
    "version",
    "platform",
    "machine",
    "python",
    "frozen",
    "occurred_at",
    "kind",
    "traceback",
    "engine_log_tail",
)

#: How many lines of the engine's log ride along — context, not contents:
#: the log runs at warning level (boot facts), and books live in the
#: database, not in this file.
ENGINE_LOG_TAIL_LINES = 40


def crash_dir(data_dir: Path | str) -> Path:
    path = Path(data_dir) / "crashes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _engine_log_tail(data_dir: Path) -> str | None:
    log = Path(data_dir) / "engine.log"
    if not log.is_file():
        return None
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-ENGINE_LOG_TAIL_LINES:]) if lines else None


def _format_traceback(tb_text: str | None, value: BaseException | None) -> str:
    """Pre-rendered text wins (the caller may be mid-crash); a bare
    exception still gets its one-line form."""

    if tb_text:
        return tb_text
    if value is not None:
        return "".join(_traceback.format_exception_only(type(value), value))
    return ""


def report_payload(
    kind: str,
    value: BaseException | None,
    tb_text: str | None,
    data_dir: Path | str,
) -> dict:
    """The allowlisted report document. Deliberately takes pre-rendered
    traceback text: the caller may be mid-crash, and formatting here is
    one less thing that can fail."""

    from .. import __version__

    return {
        "app": "ledgerforge-native",
        "version": __version__,
        "platform": f"{_platform.system()} {_platform.release()}",
        "machine": _platform.machine(),
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "occurred_at": datetime.now(UTC).isoformat(),
        "kind": kind,
        "traceback": _format_traceback(tb_text, value),
        "engine_log_tail": _engine_log_tail(Path(data_dir)),
    }


def capture(
    kind: str,
    value: BaseException | None,
    tb_text: str | None,
    data_dir: Path | str,
) -> Path:
    """Write one crash report; returns its path."""

    payload = report_payload(kind, value, tb_text, data_dir)
    name = f"crash-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}.json"
    path = crash_dir(data_dir) / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ------------------------------------------------------------------ handlers

_previous_hook = None
_previous_thread_hook = None
_fault_file = None


def install_handlers(data_dir: Path | str):
    """Capture unhandled exceptions (main thread, any thread) and fatal
    native signals, as reports in ``<data>/crashes``.

    Returns a restore callable (tests and the engine child use it to put
    the process back). The GUI layer may layer its own dialog on top by
    deferring to the next event-loop pass — never from inside the hook.
    """

    global _previous_hook, _previous_thread_hook, _fault_file

    directory = crash_dir(data_dir)

    def _hook(exc_type, exc_value, exc_tb):
        text = "".join(_traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            path = capture("unhandled-exception", exc_value, text, data_dir)
            print(f"LedgerForge: crash report written to {path}", file=sys.stderr)
        except Exception:  # noqa: BLE001 — the reporter must never crash
            print(text, file=sys.stderr)
        if _previous_hook is not None:
            with contextlib.suppress(Exception):
                _previous_hook(exc_type, exc_value, exc_tb)

    def _thread_hook(args):  # threading.excepthook's ExcepthookArgs
        text = "".join(_traceback.format_exception(*args[:3]))
        try:
            path = capture(
                f"unhandled-exception:{args[0].name if args[0] else 'thread'}",
                args[1],
                text,
                data_dir,
            )
            print(f"LedgerForge: crash report written to {path}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            print(text, file=sys.stderr)

    _previous_hook = sys.excepthook
    _previous_thread_hook = threading.excepthook
    sys.excepthook = _hook
    threading.excepthook = _thread_hook

    # fatal native signals (segfault & friends) dump the Python traceback
    # to a fault file — the best a Python process can do, and enough to
    # see which thread died where
    try:
        _fault_file = open(  # noqa: SIM115 — lives for the process lifetime
            directory / f"fault-{os.getpid()}.log", "a", encoding="utf-8"
        )
        faulthandler.enable(file=_fault_file, all_threads=True)
    except (OSError, RuntimeError):  # noqa: BLE001 — optional diagnostics
        pass

    def restore():
        global _previous_hook, _previous_thread_hook, _fault_file
        sys.excepthook = _previous_hook or sys.__excepthook__
        threading.excepthook = _previous_thread_hook or threading.__excepthook__
        faulthandler.disable()
        if _fault_file is not None:
            with contextlib.suppress(OSError):
                _fault_file.close()
            _fault_file = None

    return restore


def enable_engine_faults(data_dir: Path | str) -> Path:
    """The engine child's fault dump file — fatal signals in the child land
    in ``<data>/crashes/engine-fault-<pid>.log`` so a hard native crash is
    diagnosable next to the GUI's reports."""

    path = crash_dir(data_dir) / f"engine-fault-{os.getpid()}.log"
    try:
        handle = open(path, "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(file=handle, all_threads=True)
    except (OSError, RuntimeError):  # noqa: BLE001 — optional diagnostics
        pass
    return path


# ------------------------------------------------------------------- sending


def endpoint_from_env() -> str:
    """The operator-configured collector — empty means "send nowhere",
    which is the default and the pledge."""

    return os.environ.get("LF_CRASH_ENDPOINT", "").strip()


def autosend_enabled() -> bool:
    """The explicit machine-wide opt-in (``LF_CRASH_REPORTING=1``): new
    reports are POSTed automatically at the next launch. Off by default."""

    return os.environ.get("LF_CRASH_REPORTING", "").strip().lower() in ("1", "true", "yes")


def send_report(path: Path | str, endpoint: str, *, transport=None) -> None:
    """POST one report to the operator's endpoint. Raises on any failure —
    the caller decides whether that is worth a dialog. Never called with
    an empty endpoint (the GUI refuses first), but refuses anyway."""

    if not endpoint:
        raise ValueError("no crash endpoint is configured — reports stay local")
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(payload) - set(REPORT_KEYS):  # the allowlist is load-bearing
        raise ValueError("refusing to send a report with unknown fields")

    import httpx

    with httpx.Client(transport=transport, timeout=30.0) as client:
        response = client.post(endpoint, json=payload)
        response.raise_for_status()


def send_pending(data_dir: Path | str, endpoint: str, *, transport=None) -> int:
    """Send every not-yet-sent report (the opt-in path, at launch). Sent
    reports are marked with a ``.sent`` sidecar and never re-sent."""

    sent = 0
    for report in sorted(crash_dir(data_dir).glob("crash-*.json")):
        if report.with_suffix(".json.sent").exists():
            continue
        try:
            send_report(report, endpoint, transport=transport)
        except Exception:  # noqa: BLE001 — one bad report never stops the rest
            continue
        report.with_suffix(".json.sent").write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
        sent += 1
    return sent
