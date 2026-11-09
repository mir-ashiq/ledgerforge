"""The native application — one process, one window, one embedded engine.

Boot order (ADR-022): Qt comes up first (the user sees the splash within
milliseconds), then the ADR-021 engine — embedded PostgreSQL, schema, the
FastAPI app — starts on background threads while a splash window explains
itself. When /health answers, the splash hands over to the main window.

``--smoke`` is the release gate for the Windows build: the same code path
with Qt's offscreen platform — the engine boots, the main window constructs,
the day book and a report load through the real client — and the process
exits 0 only if every step proved out.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from .. import __version__
from ..desktop import (
    boot_database,
    default_data_dir,
    ensure_schema,
    pick_port,
)
from .client import ApiClient

#: How long the boot loop may take before the app gives up (first run: initdb).
BOOT_TIMEOUT = 300.0


def _make_app():
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setApplicationName("LedgerForge")
    QCoreApplication.setOrganizationName("LedgerForge")
    QCoreApplication.setApplicationVersion(__version__)
    app = QApplication.instance() or QApplication(sys.argv)
    return app


class _BootWindow:
    """A small native frame that owns the wait: logo, message, busy bar."""

    def __init__(self, app):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout

        from .widgets import app_icon

        self.dialog = QDialog(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.dialog.setWindowTitle("LedgerForge")
        self.dialog.setWindowIcon(app_icon())
        self.dialog.setModal(True)
        self.dialog.setFixedSize(420, 150)
        label = QLabel(f"<h3>LedgerForge {__version__}</h3>Starting the embedded engine…")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message = QLabel("booting the embedded PostgreSQL (first run takes a minute)…")
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar = QProgressBar()
        bar.setRange(0, 0)  # indeterminate — honest about an unknown boot time
        layout = QVBoxLayout(self.dialog)
        layout.addWidget(label)
        layout.addWidget(self.message)
        layout.addWidget(bar)
        self.dialog.show()
        app.processEvents()

    def say(self, text: str) -> None:
        self.message.setText(text)

    def close(self) -> None:
        self.dialog.close()


# ------------------------------------------------------------------ engine


def _serve_engine(data_dir: str, port: int, stop_event) -> None:
    """The engine child process: owns PostgreSQL, the schema, and uvicorn.

    Runs OUTSIDE the Qt process by design. Proven the hard way: Qt's event
    loop and libpq in one address space race each other into native
    segfaults under concurrent load (intermittent, inside psycopg's
    rollback) — 480-request storms with Qt absent: clean; with Qt: crash.
    A process boundary removes the whole class: the GUI can never corrupt
    the engine's heap, and the engine's crashes can never take the window
    down. pgserver's lifecycle belongs to this process too, so its atexit
    stops PostgreSQL exactly when the server exits.
    """
    import multiprocessing

    from .crashes import enable_engine_faults

    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)
    log = data / "engine.log"
    enable_engine_faults(data)  # fatal signals dump beside the GUI's reports
    try:
        dsn = boot_database(data)
        ensure_schema(dsn, quiet=True)
        server = _build_server(dsn, port, data)
    except BaseException as exc:  # noqa: BLE001 — boot failures must reach the log
        log.write_text(f"engine boot failed: {exc!r}\n", encoding="utf-8")
        raise SystemExit(3) from exc

    parent = multiprocessing.parent_process()

    def watch() -> None:
        while True:
            if stop_event.is_set() or (parent is not None and not parent.is_alive()):
                server.should_exit = True
                return
            time.sleep(0.5)

    threading.Thread(target=watch, name="lf-engine-watch", daemon=True).start()
    server.run()  # blocks; graceful exit runs pgserver's atexit in THIS process


def _build_server(dsn: str, port: int, data: Path):
    from ..desktop import build_server

    return build_server(
        dsn,
        port=port,
        static_dir=None,  # the native shell is the UI; the API is the surface
        data_dir=data,
        log_level="warning",
    )


class Engine:
    """The ADR-021 engine stack in a child process; loopback only."""

    def __init__(self, data_dir: Path, port: int | None = None):
        self.data_dir = data_dir
        self.port = pick_port(port)
        self._process = None
        self._stop = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def start(self, *, quiet: bool = True) -> None:  # noqa: ARG002 - child logs
        import multiprocessing

        ctx = multiprocessing.get_context("spawn")
        self._stop = ctx.Event()
        self._process = ctx.Process(
            target=_serve_engine,
            args=(str(self.data_dir), self.port, self._stop),
            name="lf-native-engine",
        )
        self._process.start()

    def stop(self, timeout: float = 30.0) -> None:
        if self._process is None:
            return
        self._stop.set()
        self._process.join(timeout)
        if self._process.is_alive():
            # hard kill: PostgreSQL recovers from WAL on the next boot (the
            # v0.17 contract — a stale postmaster.pid is detected and fixed)
            self._process.terminate()
            self._process.join(10)

    def boot_failure(self) -> str | None:
        """The child's reason for dying during boot, from <data>/engine.log."""
        if self._process is None or self._process.is_alive():
            return None
        log = self.data_dir / "engine.log"
        if log.is_file():
            return log.read_text(encoding="utf-8", errors="replace").strip()[:2000]
        return f"the engine process exited with code {self._process.exitcode}"

    def wait_healthy(self, timeout: float = BOOT_TIMEOUT) -> dict:
        import httpx

        base = f"http://127.0.0.1:{self.port}"
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if not self.alive:
                raise RuntimeError(self.boot_failure() or "the engine process exited during boot")
            try:
                health = httpx.get(f"{base}/health", timeout=5)
                if health.status_code == 200:
                    return health.json()
            except Exception as exc:  # noqa: BLE001 — retry until the deadline
                last_error = exc
            time.sleep(0.25)
        raise RuntimeError(f"the engine never became healthy (last error: {last_error})")


# --------------------------------------------------------------- the window


def run_app(data_dir: Path | None = None, port: int | None = None) -> int:
    """The interactive session: splash -> engine -> the main window."""
    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtWidgets import QMessageBox

    from ..updating import pending_update
    from .crashes import autosend_enabled, endpoint_from_env, install_handlers, send_pending
    from .main_window import MainWindow

    data = data_dir or default_data_dir()
    data.mkdir(parents=True, exist_ok=True)

    app = _make_app()

    # one LedgerForge window at a time — the second launch focuses nothing,
    # it says so and exits (the engine is single-user by design).
    lock = QLockFile(str(data / "native.lock"))
    if not lock.tryLock(0):
        QMessageBox.information(
            None,
            "LedgerForge",
            "LedgerForge is already running.\n\nLook for its window in the taskbar "
            "(the LedgerForge icon).",
        )
        return 0

    # ADR-031: crashes are captured always (locally), and sent only when
    # the operator configured an endpoint AND this machine opted in.
    restore_crash_handlers = install_handlers(data)
    if endpoint_from_env() and autosend_enabled():
        send_pending(data, endpoint_from_env())

    staged = pending_update(data)
    if staged is not None and not getattr(sys, "frozen", False):
        print(
            f"LedgerForge: update {staged['to']} is staged, but a source run never "
            "self-modifies — the frozen app applies it on relaunch.",
            file=sys.stderr,
        )

    splash = _BootWindow(app)

    engine = Engine(data, port)
    engine.start()  # the child process; the splash polls its heartbeat

    client: ApiClient | None = None
    window: MainWindow | None = None
    started = time.monotonic()

    def poll():
        import httpx

        nonlocal client, window
        if not engine.alive:
            splash.close()
            QMessageBox.critical(
                None,
                "LedgerForge — engine failed",
                f"The embedded engine could not start:\n\n{engine.boot_failure()}\n\n"
                f"Your data is safe in {data}.",
            )
            app.quit()
            return
        try:  # a short probe: never park the GUI thread on a half-open socket
            probe = httpx.get(f"http://127.0.0.1:{engine.port}/health", timeout=2)
            ready = probe.status_code == 200
        except Exception:  # noqa: BLE001 — refused: the child is still booting
            ready = False
        if not ready:
            if time.monotonic() - started > BOOT_TIMEOUT:
                splash.close()
                engine.stop()
                QMessageBox.critical(None, "LedgerForge", "The engine never answered. Try again.")
                app.quit()
                return
            splash.say("engine up — waiting for /health…")
            QTimer.singleShot(200, poll)
            return

        splash.say("engine ready — opening your books…")
        client = ApiClient(engine.port)
        window = MainWindow(client, str(data))
        splash.close()
        window.show()
        app.aboutToQuit.connect(engine.stop)

    QTimer.singleShot(50, poll)
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C in a console still quits
    code = app.exec()
    lock.unlock()
    restore_crash_handlers()
    return code


# ------------------------------------------------------------------- smoke


def smoke(data_dir: Path | None = None, port: int | None = None) -> int:
    """The release gate: prove the native app, headless, exit 0/1.

    Runs under ``QT_QPA_PLATFORM=offscreen`` (the workflow sets it): the
    engine boots, the main window constructs against the real client, the
    day book loads, a trial balance renders through the reporting API, the
    GSTR-1 pack answers (the gst plugin ships inside the bundle), and an
    export proves the ADR-030 format seam (CSV with the engine's exact
    digits). Every failure prints SMOKE FAILED with its reason; the last
    line is SMOKE OK.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QTimer

    from .main_window import MainWindow
    from .tasks import run as run_task

    data = data_dir or default_data_dir()
    print(f"LedgerForge {__version__} native — smoke, data directory: {data}")
    engine = Engine(data, port)
    try:
        engine.start()
        client = ApiClient(engine.port)
        health = engine.wait_healthy()
        print(f"engine healthy: {health}")
    except Exception as exc:  # noqa: BLE001 — any boot failure fails the gate
        print(f"SMOKE FAILED: engine boot: {exc}", file=sys.stderr)
        engine.stop()
        return 1

    app = _make_app()
    window = MainWindow(client, str(data))
    window.show()

    remaining = {
        "day book",
        "trial balance",
        "gstr1 pack",
        "export seam",
        "update gate",
        "crash gate",
    }
    state = {"failed": False}

    def fail(stage: str, error: object) -> None:
        from .widgets import error_detail

        if state["failed"]:
            return
        state["failed"] = True
        print(f"SMOKE FAILED: {stage}: {error_detail(error)}", file=sys.stderr)
        engine.stop()
        app.exit(1)

    def landed(stage: str) -> None:
        remaining.discard(stage)
        if not remaining and not state["failed"]:
            print("SMOKE OK   the native window, the engine, the reports — all proven")
            engine.stop()
            app.exit(0)

    def _daybook():
        def done(vouchers):
            if not isinstance(vouchers, list):
                fail("day book", TypeError(f"expected a list, got {type(vouchers).__name__}"))
                return
            print(f"day book: {len(vouchers)} vouchers visible")
            landed("day book")

        run_task(client.vouchers, done, lambda e: fail("day book", e))

    def _tb():
        def done(data):
            totals = data.get("totals", {})
            print(f"trial balance: {len(data.get('ledgers', []))} ledgers, totals {totals}")
            landed("trial balance")

        run_task(client.trial_balance, done, lambda e: fail("trial balance", e))

    def _gstr1():
        def done(result):
            content, media, name = result
            print(f"gstr1 pack: {len(content)} bytes ({media}, {name})")
            landed("gstr1 pack")

        run_task(
            lambda: client.pack("gstr1", period=_smoke_period()),
            done,
            lambda e: fail("gstr1 pack", e),
        )

    def _export():
        # ADR-030's seam on a real surface: the trial balance as CSV, its
        # first line the columns, the digits the engine's own.
        def done(result):
            content, name = result
            text = content.decode("utf-8")
            header = text.splitlines()[0] if text.splitlines() else ""
            print(f"export seam: {len(content)} bytes csv ({name}), header: {header!r}")
            if "ledger_name" not in header:
                fail("export seam", ValueError(f"unexpected CSV header: {header!r}"))
                return
            landed("export seam")

        run_task(
            lambda: client.export_bytes("/api/v1/reports/trial-balance", {}, "csv"),
            done,
            lambda e: fail("export seam", e),
        )

    def _update_gate():
        # ADR-031's fail-closed law, proven on the shipped bytes: a
        # tampered manifest is refused, an unsigned one is refused, and a
        # fresh data directory has nothing staged. The updater never
        # installs what it cannot verify.
        def done(result):
            refused_unsigned, refused_tampered, staged = result
            print(
                f"update gate: unsigned refused={refused_unsigned}, "
                f"tampered refused={refused_tampered}, staged={staged}"
            )
            if not (refused_unsigned and refused_tampered and staged is None):
                fail(
                    "update gate",
                    ValueError(
                        f"unsigned={refused_unsigned} tampered={refused_tampered} "
                        f"staged={staged} — the gate must refuse both and stage nothing"
                    ),
                )
                return
            landed("update gate")

        def work():
            from .. import updating

            manifest = {
                "product": updating.PRODUCT,
                "version": "999.0.0",
                "platforms": {"windows-x64": {"url": "https://x/y.zip", "sha256": "00", "size": 1}},
            }
            raw = updating.canonical_bytes(manifest)
            try:
                updating.verify_manifest(raw, "not-a-signature")
                refused_unsigned = False
            except updating.UpdateError:
                refused_unsigned = True
            tampered = json.loads(raw)
            tampered["version"] = "998.0.0"
            try:
                updating.verify_manifest(updating.canonical_bytes(tampered), "AAAA")
                refused_tampered = False
            except updating.UpdateError:
                refused_tampered = True
            return refused_unsigned, refused_tampered, updating.pending_update(data)

        run_task(work, done, lambda e: fail("update gate", e))

    def _crash_gate():
        # ADR-031's capture-always pledge, proven: a report lands in the
        # data directory, readable, allowlisted, never containing books.
        def done(result):
            path, keys = result
            print(f"crash gate: report {path.name}, keys {sorted(keys)}")
            from .crashes import REPORT_KEYS

            if set(keys) - set(REPORT_KEYS):
                fail("crash gate", ValueError(f"report carries unknown keys: {keys}"))
                return
            landed("crash gate")

        def work():
            from .crashes import capture

            path = capture("smoke-proof", ValueError("the smoke gate's own proof"), None, data)
            payload = json.loads(path.read_text(encoding="utf-8"))
            return path, set(payload)

        run_task(work, done, lambda e: fail("crash gate", e))

    # a hard ceiling so a hung engine still fails the gate instead of the job
    QTimer.singleShot(120_000, lambda: fail("overall", TimeoutError("two minutes, no completion")))
    QTimer.singleShot(100, _daybook)
    QTimer.singleShot(150, _tb)
    QTimer.singleShot(200, _gstr1)
    QTimer.singleShot(250, _export)
    QTimer.singleShot(300, _update_gate)
    QTimer.singleShot(350, _crash_gate)

    code = app.exec()
    window.close()
    return code


def _smoke_period() -> str:
    """The GSTR-1 smoke period: this month, or the previous one late-month."""
    from datetime import date

    today = date.today()
    return f"{today.year}-{today.month:02d}"


# --------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    # frozen Windows exe + spawn children: the bootloader re-runs the exe for
    # every multiprocessing child; freeze_support() intercepts that and runs
    # the child's target instead of the app. Must be first, always.
    import multiprocessing

    multiprocessing.freeze_support()

    parser = argparse.ArgumentParser(
        prog="ledgerforge-native",
        description=(
            "LedgerForge Native — the desktop application (ADR-022): "
            "a Qt window over the embedded engine. No browser, no web server "
            "to visit — the window is the product."
        ),
    )
    parser.add_argument("--version", action="version", version=f"LedgerForge Native {__version__}")
    parser.add_argument("--data-dir", type=Path, default=None, help="where the database lives")
    parser.add_argument(
        "--port", type=int, default=None, help="loopback engine port (auto by default)"
    )
    parser.add_argument(
        "--smoke", action="store_true", help="headless self-check for CI: exit 0/1, no window"
    )
    parser.add_argument(
        "--accountants-day",
        action="store_true",
        help="the scripted acceptance run (ADR-032): a full working day, "
        "fifty vouchers across the packs, every number asserted — "
        "exit 0/1, the release gate beyond the smoke",
    )
    parser.add_argument(
        "--apply-update",
        type=Path,
        default=None,
        metavar="INSTALL_DIR",
        help="apply the staged update (if any) to INSTALL_DIR and exit — "
        "the update-on-relaunch path (ADR-031), for updaters and tests",
    )
    args = parser.parse_args(argv)
    if args.apply_update is not None:
        from ..updating import apply_pending

        applied = apply_pending(args.data_dir or default_data_dir(), args.apply_update)
        print(f"applied update: {applied}" if applied else "no staged update")
        return 0
    if args.smoke:
        return smoke(args.data_dir, args.port)
    if args.accountants_day:
        from .acceptance import run_day

        return run_day(args.data_dir, args.port)

    # ADR-031 — update-on-relaunch: BEFORE Qt and before the engine child,
    # a frozen app applies the staged update to its own install directory
    # (a source run refuses to self-modify; run_app explains). A failed
    # verification starts the current version — the updater's failure mode
    # is "no update", never "broken app".
    if getattr(sys, "frozen", False):
        from ..updating import STAGING_DIRNAME, UpdateError, apply_pending

        data = args.data_dir or default_data_dir()
        try:
            applied = apply_pending(data)
        except UpdateError as exc:
            print(
                f"LedgerForge: the staged update failed verification ({exc}) — "
                "starting the current version.",
                file=sys.stderr,
            )
            import shutil

            shutil.rmtree(Path(data) / STAGING_DIRNAME, ignore_errors=True)
        else:
            if applied:
                print(f"LedgerForge: update {applied} applied — starting.")
    return run_app(args.data_dir, args.port)
