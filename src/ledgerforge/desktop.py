"""LedgerForge Desktop — the zero-config, single-process local edition (ADR-021).

One command boots the whole product on a laptop: an embedded PostgreSQL (the
pgserver portable distribution — the same partitions, triggers and exact
NUMERIC the engine demands, no separate install), a provisioned-or-migrated
book database, the FastAPI engine, and the built web UI served same-origin on
a loopback port. The default browser opens on it. Ctrl+C stops everything;
the data directory survives.

    ledgerforge-desktop                  # or: ledgerforge desktop
    ledgerforge-desktop --smoke          # headless self-check, exit 0/1
    ledgerforge-desktop --data-dir D --port 8420 --no-browser

The Windows binary is produced by ``desktop/ledgerforge.spec`` (PyInstaller,
onedir) and published by the ``release-windows`` workflow on every ``v*``
tag. The same code path runs from source on Linux and macOS — pgserver ships
binaries for all three, and only Windows is packaged today (ADR-021).

Single-user contract: the desktop binds ``127.0.0.1`` only, serves one
browser session, and runs with ``LF_AUTH_MODE=disabled`` — anonymous-admin on
a loopback port nobody else can reach. Network deployments keep the v0.16
production guard and OIDC (docs/deployment.md); the desktop never exposes a
port.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__

#: The preferred loopback port (busy ports fall back to an ephemeral one).
#: 8420 — "LF" on a telephone keypad, and nothing well-known lives there.
DEFAULT_PORT = 8420

#: src/ledgerforge/desktop.py -> repo root (source checkouts only; frozen
#: bundles resolve everything through sys._MEIPASS instead).
REPO_ROOT = Path(__file__).resolve().parents[2]

_DESKTOP_ENV_KEYS = (
    "LF_DATABASE_URL",
    "LF_AUTH_MODE",
    "LF_PLUGIN_PATH",
    "LF_STATIC_DIR",
)


# ------------------------------------------------------------- locations


def default_data_dir() -> Path:
    """Where the desktop keeps its PostgreSQL cluster and drop-in plugins.

    ``LF_DESKTOP_DATA`` wins; otherwise the platform convention:
    ``%LOCALAPPDATA%\\LedgerForge`` (Windows),
    ``~/Library/Application Support/LedgerForge`` (macOS),
    ``$XDG_DATA_HOME/ledgerforge`` (Linux).
    """
    override = os.environ.get("LF_DESKTOP_DATA")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / "LedgerForge"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LedgerForge"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "ledgerforge"


def default_static_dir() -> Path | None:
    """The built web UI: bundled first, then a source checkout's ``dist``.

    In a PyInstaller bundle the frontend rides along as data files under
    ``sys._MEIPASS``; from source, ``npm --prefix frontend/web run build``
    produces the same tree. Returns None when neither exists (API-only boot,
    a loud warning) — the engine itself never requires the UI.
    """
    candidates = []
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidates.append(Path(bundled) / "frontend" / "web" / "dist")
    candidates.append(REPO_ROOT / "frontend" / "web" / "dist")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


# ----------------------------------------------------------------- network


def pick_port(preferred: int | None = None) -> int:
    """A free loopback port: the preferred one when bindable, else ephemeral."""
    if preferred is not None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", preferred))
            except OSError:
                print(f"port {preferred} is busy — picking a free one instead")
            else:
                return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------- database


def boot_database(data_dir: Path) -> str:
    """Start (or adopt) the embedded PostgreSQL cluster; return its DSN.

    ``pgserver`` ships the full server, so the desktop keeps Law-grade
    PostgreSQL semantics — DEFERRABLE constraint triggers, NUMERIC(26,8),
    closure-table triggers, the lot — with nothing to install (ADR-021).
    ``cleanup_mode='stop'`` stops the server when this process exits and
    *keeps* the data directory; a hard kill leaves a stale postmaster.pid
    that the next boot detects and recovers from.
    """
    try:
        import pgserver
    except ImportError as exc:  # pragma: no cover - exercised by the extra
        raise SystemExit(
            "the desktop edition needs the pgserver package — pip install 'ledgerforge[desktop]'"
        ) from exc
    data_dir.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(data_dir / "pgdata", cleanup_mode="stop")
    return server.get_uri()


def ensure_schema(dsn: str, *, quiet: bool = False) -> None:
    """Provision a fresh database, or migrate an existing one. Idempotent.

    Fresh (no ``books`` table): the SQL bootstrap — flattened head schema,
    functions, the default book and its chart of accounts, Alembic stamped
    to head (ADR-006/009). Existing: ``alembic upgrade head`` runs the real
    migrations; the seed never re-runs over live books.
    """
    import psycopg

    with psycopg.connect(dsn) as conn:
        fresh = conn.execute("SELECT to_regclass('public.books') IS NULL").fetchone()[0]
    if fresh:
        from . import bootstrap

        bootstrap.apply_sql_files(dsn, echo=not quiet)
        return
    from alembic import command as alembic_command

    from .migrations import make_alembic_config

    cfg = make_alembic_config()
    cfg.attributes["url"] = dsn
    alembic_command.upgrade(cfg, "head")
    if not quiet:
        print("database migrated to head")


def _sqlalchemy_url(uri: str) -> str:
    """``postgresql://`` -> ``postgresql+psycopg://`` (SQLAlchemy spelling)."""
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# ------------------------------------------------------------ app assembly


def configure_env(dsn: str, *, static_dir: Path | None, dropin_dir: Path) -> None:
    """Point the engine at the embedded database *before* the app imports.

    ``ledgerforge.main`` builds the app at import time, so the environment
    must be final first. The desktop is a loopback single-user server:
    anonymous-admin is the contract (ADR-021); drop-in plugins load from
    ``<data>/plugins`` through the unchanged ADR-016 trust gate.
    """
    os.environ["LF_DATABASE_URL"] = _sqlalchemy_url(dsn)
    os.environ["LF_AUTH_MODE"] = "disabled"
    os.environ.pop("LF_REQUIRE_AUTH", None)
    dropin_dir.mkdir(parents=True, exist_ok=True)
    paths = [p for p in (os.environ.get("LF_PLUGIN_PATH", ""), str(dropin_dir)) if p]
    os.environ["LF_PLUGIN_PATH"] = os.pathsep.join(dict.fromkeys(paths))
    if static_dir is not None:
        os.environ["LF_STATIC_DIR"] = str(static_dir)
    else:
        os.environ.pop("LF_STATIC_DIR", None)
    # Re-configure a live process (tests, repeated serve() calls): the
    # settings cache and the pooled engine both belong to the previous DSN.
    from .db import reset_engine
    from .settings import get_settings

    get_settings.cache_clear()
    reset_engine()


def build_server(
    dsn: str,
    *,
    host: str = "127.0.0.1",
    port: int,
    static_dir: Path | None,
    data_dir: Path,
    log_level: str = "info",
):
    """Assemble the uvicorn server for the desktop session."""
    configure_env(dsn, static_dir=static_dir, dropin_dir=data_dir / "plugins")

    from .main import create_app

    app = create_app()

    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level=log_level)
    return uvicorn.Server(config)


# ------------------------------------------------------------------- modes


def run(
    data_dir: Path | None = None,
    port: int | None = None,
    *,
    open_browser: bool = True,
) -> int:
    """The interactive desktop session: boot, serve, browse, stop on Ctrl+C."""
    data = data_dir or default_data_dir()
    static_dir = default_static_dir()
    if static_dir is None:
        print(
            "note: no built web UI found (frontend/web/dist) — serving the API only;\n"
            "      build it with: npm --prefix frontend/web run build"
        )
    print(f"LedgerForge {__version__} desktop — data directory: {data}")
    dsn = boot_database(data)
    ensure_schema(dsn)
    port = pick_port(port if port is not None else DEFAULT_PORT)
    server = build_server(dsn, port=port, static_dir=static_dir, data_dir=data)
    url = f"http://127.0.0.1:{port}/"
    print(f"serving on {url}  (Ctrl+C stops the app, your data stays in {data})")
    if open_browser:
        threading.Timer(1.5, webbrowser.open, args=[url]).start()
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl+C in the console window
        server.run()
    print("LedgerForge desktop stopped.")
    return 0


def smoke(
    data_dir: Path | None = None,
    port: int | None = None,
    *,
    static_dir: Path | None = None,
    timeout: float = 300.0,
) -> int:
    """Headless self-check: boot everything, prove /health and the UI, exit.

    The release workflow runs ``LedgerForge.exe --smoke`` on the build
    runner: an exit code of 0 means the shipped binary really booted its
    embedded database, provisioned the schema, and served the web UI —
    not merely that it packaged.
    """
    data = data_dir or default_data_dir()
    if static_dir is None:
        static_dir = default_static_dir()
        if static_dir is None:
            print(
                "SMOKE FAILED: no built web UI (frontend/web/dist) — the desktop "
                "bundle must ship one",
                file=sys.stderr,
            )
            return 1
    dsn = boot_database(data)
    ensure_schema(dsn, quiet=True)
    port = pick_port(port if port is not None else DEFAULT_PORT)
    server = build_server(dsn, port=port, static_dir=static_dir, data_dir=data, log_level="warning")
    thread = threading.Thread(target=server.run, name="lf-smoke-server", daemon=True)
    thread.start()

    import httpx

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if not thread.is_alive():
            break
        try:
            health = httpx.get(f"{base}/health", timeout=5)
            if health.status_code == 200:
                ui = httpx.get(base + "/", timeout=10)
                ok = ui.status_code == 200
                print(f"SMOKE OK   /health -> {health.json()}")
                print(
                    f"           /       -> {ui.status_code} "
                    f"({'web UI served' if ok else 'NO web UI'})"
                )
                server.should_exit = True
                thread.join(timeout=60)
                return 0 if ok else 1
        except Exception as exc:  # noqa: BLE001 — retry until the deadline
            last_error = exc
        time.sleep(0.5)
    server.should_exit = True
    print(
        f"SMOKE FAILED: the server did not become healthy within {timeout:.0f}s "
        f"(last error: {last_error})",
        file=sys.stderr,
    )
    return 1


# --------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ledgerforge-desktop",
        description=(
            "LedgerForge Desktop — the zero-config local edition: embedded "
            "PostgreSQL, the engine, and the web UI on 127.0.0.1 (ADR-021)."
        ),
    )
    parser.add_argument("--version", action="version", version=f"LedgerForge Desktop {__version__}")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="where the database and plugins live (default: the platform's app-data dir)",
    )
    parser.add_argument(
        "--port", type=int, default=None, help=f"loopback port (default: {DEFAULT_PORT})"
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open the default browser")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="boot headless, self-check /health and the web UI, exit 0/1 (no browser)",
    )
    args = parser.parse_args(argv)
    if args.smoke:
        return smoke(args.data_dir, args.port)
    return run(args.data_dir, args.port, open_browser=not args.no_browser)


if __name__ == "__main__":  # pragma: no cover - manual entry
    raise SystemExit(main())
