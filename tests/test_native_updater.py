"""The update controller, proven offscreen with a file:// manifest
(ADR-031): the real protocol — fetch, verify, plan, download, stage —
against a signed manifest on disk, no socket, no mock of the protocol
itself. The pinned key is swapped for the test's key via the module
attribute (read at call time, exactly for this)."""

from __future__ import annotations

import hashlib
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ledgerforge import updating  # noqa: E402
from ledgerforge.nativeapp import updater as updater_module  # noqa: E402
from ledgerforge.nativeapp.updater import UpdateController, read_config  # noqa: E402
from ledgerforge.plugins.trust import generate_publisher_keypair  # noqa: E402


@pytest.fixture()
def app():
    yield QApplication.instance() or QApplication([])


def test_read_config_honours_the_environment(monkeypatch):
    monkeypatch.delenv("LF_UPDATE_URL", raising=False)
    monkeypatch.delenv("LF_UPDATE_CHECKS", raising=False)
    config = read_config()
    assert config["url"] == updating.DEFAULT_UPDATE_URL
    assert config["checks"] is True

    monkeypatch.setenv("LF_UPDATE_URL", "file:///tmp/my-manifest.json")
    monkeypatch.setenv("LF_UPDATE_CHECKS", "0")
    config = read_config()
    assert config["url"] == "file:///tmp/my-manifest.json"
    assert config["checks"] is False


def _write_signed_manifest(directory, private: str, manifest: dict):

    path = directory / "update_manifest.json"
    path.write_bytes(updating.canonical_bytes(manifest))
    (directory / "update_manifest.sig").write_text(
        updating.sign_manifest(manifest, private), encoding="utf-8"
    )
    return path


def test_controller_checks_downloads_and_stages(tmp_path, app, monkeypatch, qtbot):
    # our own release key for this test — the controller verifies against
    # the module attribute, read at call time
    private, public = generate_publisher_keypair()
    monkeypatch.setattr(updating, "PINNED_UPDATE_KEY", public)

    # the delta artifact: a tiny real delta of a one-file app
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    (old_dir / "app.py").write_text("v1", encoding="utf-8")
    (new_dir / "app.py").write_text("v2", encoding="utf-8")
    delta_path = tmp_path / "delta.zip"
    updating.build_delta(old_dir, new_dir, "0.0.1", "999.0.0", delta_path)

    # the manifest, signed, served from file:// — the real fetch path
    serve = tmp_path / "serve"
    serve.mkdir()
    delta_bytes = delta_path.read_bytes()
    (serve / "delta.zip").write_bytes(delta_bytes)
    manifest = {
        "product": updating.PRODUCT,
        "version": "999.0.0",
        "platforms": {
            updating.platform_key(): {
                "url": "file:///nowhere/full.zip",
                "sha256": "00" * 32,
                "size": 0,
                "deltas": {
                    "0.0.1": {
                        "url": (serve / "delta.zip").as_uri(),
                        "sha256": hashlib.sha256(delta_bytes).hexdigest(),
                        "size": len(delta_bytes),
                    }
                },
            }
        },
    }
    manifest_path = _write_signed_manifest(serve, private, manifest)

    monkeypatch.setenv("LF_UPDATE_URL", manifest_path.as_uri())
    monkeypatch.setattr(updater_module, "__version__", "0.0.1", raising=False)

    data_dir = tmp_path / "data"
    statuses: list[str] = []
    UpdateController(data_dir, on_status=statuses.append).check()
    qtbot.waitUntil(lambda: any("999.0.0" in text for text in statuses), timeout=10_000)

    staged = updating.pending_update(data_dir)
    assert staged is not None and staged["to"] == "999.0.0"
    assert staged["kind"] == "delta"
    # the staged payload is verified, on disk, ready for the next relaunch
    assert (data_dir / "update-staging" / "payload" / "app.py").read_text(encoding="utf-8") == "v2"


def test_controller_reports_up_to_date_quietly(tmp_path, app, monkeypatch, qtbot):
    private, public = generate_publisher_keypair()
    monkeypatch.setattr(updating, "PINNED_UPDATE_KEY", public)

    serve = tmp_path / "serve"
    serve.mkdir()
    manifest = {
        "product": updating.PRODUCT,
        "version": "0.0.1",  # not newer than the running version
        "platforms": {updating.platform_key(): {"url": "file:///x.zip", "sha256": "00", "size": 1}},
    }
    manifest_path = _write_signed_manifest(serve, private, manifest)
    monkeypatch.setenv("LF_UPDATE_URL", manifest_path.as_uri())

    data_dir = tmp_path / "data"
    statuses: list[str] = []
    UpdateController(data_dir, on_status=statuses.append).check()
    qtbot.waitUntil(lambda: any("up to date" in text for text in statuses), timeout=10_000)
    assert updating.pending_update(data_dir) is None  # nothing staged


def test_boot_check_respects_the_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("LF_UPDATE_CHECKS", "0")
    statuses: list[str] = []
    UpdateController(tmp_path, on_status=statuses.append).check_on_boot()
    assert statuses == []  # disabled means silent: no fetch, no words
