"""The desktop edition (ADR-021): data locations, environment wiring, the
same-origin web UI mount, and a full smoke boot — embedded PostgreSQL
cluster -> schema provisioning -> engine -> uvicorn -> /health + / — the
exact code path the Windows binary self-checks at release time."""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

import pytest

from ledgerforge import __version__, desktop

ENV_KEYS = (
    "LF_DATABASE_URL",
    "LF_AUTH_MODE",
    "LF_PLUGIN_PATH",
    "LF_STATIC_DIR",
    "LF_DESKTOP_DATA",
)


@pytest.fixture(autouse=True)
def desktop_env_guard():
    """Snapshot the LF_* environment around every desktop test, restore after.

    The desktop configures the engine through the environment (main.py
    builds the app at import time); the suite's own session fixtures own
    those variables, so every test must hand them back pristine — settings
    cache and engine pool included.
    """
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    from ledgerforge.db import reset_engine
    from ledgerforge.settings import get_settings

    get_settings.cache_clear()
    reset_engine()


@pytest.fixture()
def fake_ui(tmp_path: Path) -> Path:
    """A stand-in built frontend: an index.html plus one hashed asset."""
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><title>LedgerForge</title><h1>desktop ui</h1>\n")
    (ui / "assets" / "app.js").write_text("console.log('lf')\n")
    return ui


# ------------------------------------------------------------ locations


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LF_DESKTOP_DATA", str(tmp_path / "override"))
    assert desktop.default_data_dir() == tmp_path / "override"


def test_data_dir_windows_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    assert desktop.default_data_dir() == tmp_path / "appdata" / "LedgerForge"


def test_data_dir_linux_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert desktop.default_data_dir() == tmp_path / "xdg" / "ledgerforge"


def test_static_dir_prefers_the_bundle(monkeypatch, tmp_path):
    bundled = tmp_path / "bundle" / "frontend" / "web" / "dist"
    bundled.mkdir(parents=True)
    (bundled / "index.html").write_text("x")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    monkeypatch.setattr(desktop, "REPO_ROOT", tmp_path / "no-repo-build")
    assert desktop.default_static_dir() == bundled


def test_static_dir_none_when_nothing_is_built(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "empty-bundle"), raising=False)
    monkeypatch.setattr(desktop, "REPO_ROOT", tmp_path / "no-repo-build")
    assert desktop.default_static_dir() is None


# ----------------------------------------------------------------- network


def test_pick_port_returns_bindable():
    port = desktop.pick_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))  # must not raise


def test_pick_port_falls_back_when_busy():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy = blocker.getsockname()[1]
        assert desktop.pick_port(busy) != busy


# ------------------------------------------------------------ environment


def test_configure_env_points_the_engine_at_the_desktop_db(tmp_path):
    desktop.configure_env(
        "postgresql://postgres@127.0.0.1:5555/postgres",
        static_dir=tmp_path / "ui",
        dropin_dir=tmp_path / "data" / "plugins",
    )
    assert os.environ["LF_DATABASE_URL"] == "postgresql+psycopg://postgres@127.0.0.1:5555/postgres"
    assert os.environ["LF_AUTH_MODE"] == "disabled"
    assert str(tmp_path / "data" / "plugins") in os.environ["LF_PLUGIN_PATH"]
    assert (tmp_path / "data" / "plugins").is_dir()
    assert os.environ["LF_STATIC_DIR"] == str(tmp_path / "ui")


def test_configure_env_without_static_dir_pops_it(tmp_path):
    os.environ["LF_STATIC_DIR"] = "/stale"
    desktop.configure_env(
        "postgresql://x@127.0.0.1:1/x", static_dir=None, dropin_dir=tmp_path / "plugins"
    )
    assert "LF_STATIC_DIR" not in os.environ


def test_plugin_path_split_survives_windows_drive_letters(monkeypatch):
    """The ADR-021 regression: a drive-letter colon must not split a path."""
    from ledgerforge.plugins import split_plugin_path

    # POSIX default: colon-separated, exactly as before (os.pathsep == ':').
    assert split_plugin_path("a:/drop/b: :") == ["a", "/drop/b"]
    # Windows reality: ';' separates, ':' lives inside r"C:\...\plugins".
    monkeypatch.setattr(os, "pathsep", ";")
    win = r"C:\Users\me\AppData\Local\LedgerForge\plugins"
    assert split_plugin_path(f"{win};D:\\other") == [win, "D:\\other"]


def test_cli_desktop_group_builds_argv(monkeypatch):
    from ledgerforge.__main__ import cmd_desktop

    captured: dict[str, list[str]] = {}

    def fake_main(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr("ledgerforge.desktop.main", fake_main)
    ns = argparse.Namespace(data_dir=Path("/data"), port=8420, no_browser=True, smoke=True)
    assert cmd_desktop(ns) == 0
    assert captured["argv"] == [
        "--data-dir",
        "/data",
        "--port",
        "8420",
        "--no-browser",
        "--smoke",
    ]


# ------------------------------------------------- the same-origin web UI


@pytest.fixture()
def ui_app(monkeypatch, sqlalchemy_url, fake_ui):
    from ledgerforge.db import reset_engine
    from ledgerforge.main import create_app
    from ledgerforge.settings import get_settings

    monkeypatch.setenv("LF_STATIC_DIR", str(fake_ui))
    get_settings.cache_clear()
    reset_engine()
    app = create_app()
    yield app
    get_settings.cache_clear()
    reset_engine()


def test_web_ui_served_same_origin(ui_app, fake_ui):
    """LF_STATIC_DIR mounts the built frontend at '/': index, hashed assets,
    and the API on the same port — the desktop contract (ADR-021)."""
    from fastapi.testclient import TestClient

    with TestClient(ui_app) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "desktop ui" in index.text

        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "lf" in asset.text

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {
            "status": "ok",
            "version": __version__,
            "auth_mode": "disabled",
        }


# --------------------------------------------------------- the smoke boot


def test_smoke_boots_the_whole_desktop(tmp_path_factory, fake_ui):
    """Two boots against one data directory, the release gate in miniature:

    1. first boot  — pgserver initdb, SQL bootstrap, default book, uvicorn,
       /health answers with this build's version and '/' serves the web UI;
    2. second boot — the existing database takes the ``alembic upgrade head``
       path instead of re-seeding, and the app still comes up healthy.
    """
    data = tmp_path_factory.mktemp("lf-desktop-data")
    assert desktop.smoke(data_dir=data, static_dir=fake_ui, timeout=300) == 0
    assert desktop.smoke(data_dir=data, static_dir=fake_ui, timeout=300) == 0

    # The first boot provisioned a real book; prove the schema and seed
    # landed in the embedded cluster (and that books survive a re-boot).
    import psycopg

    from ledgerforge.desktop import boot_database

    dsn = boot_database(data)
    with psycopg.connect(dsn) as conn:
        books = conn.execute("SELECT name FROM books ORDER BY id").fetchall()
        assert books, "the desktop bootstrap must seed the default book"
