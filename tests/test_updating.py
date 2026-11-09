"""The update protocol, proven without a socket (ADR-031).

Every network touch in ``ledgerforge.updating`` goes through an injectable
``fetch``, so this module exercises the whole contract — canonical bytes,
sign/verify round-trips and every tamper case, version ordering, plan
choice, delta build/download/apply, staging verification, fail-closed
fetches — at the speed of a tmpdir.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from ledgerforge import updating
from ledgerforge.plugins.trust import generate_publisher_keypair


@pytest.fixture()
def keypair():
    private, public = generate_publisher_keypair()
    return private, public


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _signed_manifest(private: str, document: dict) -> tuple[bytes, str]:
    raw = updating.canonical_bytes(document)
    return raw, updating.sign_manifest(document, private)


# ------------------------------------------------------------- versions


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("0.28.0", "0.27.0", True),
        ("1.0.0", "0.28.0", True),
        ("0.27.10", "0.27.9", True),
        ("0.27.0", "0.28.0", False),
        ("0.27.0", "0.27.0", False),
        ("1.0.0", "1.0.0-rc.1", True),  # the release outranks its pre-release
        ("1.0.0-rc.1", "1.0.0", False),
        ("1.0.0-rc.2", "1.0.0-rc.1", True),
        ("1.0.0-1", "1.0.0-alpha", False),  # numeric identifiers rank lower
    ],
)
def test_version_ordering(candidate, current, expected):
    assert updating.is_newer(candidate, current) is expected


def test_version_parse_failures():
    with pytest.raises(updating.UpdateError):
        updating.parse_version("not.a.version")
    with pytest.raises(updating.UpdateError):
        updating.parse_version("")


# ------------------------------------------------------- manifest signing


def test_canonical_bytes_are_one_form():
    left = updating.canonical_bytes({"b": 1, "a": 2})
    right = updating.canonical_bytes({"a": 2, "b": 1})
    assert left == right
    assert b" " not in left  # no whitespace — signatures cover exact bytes


def test_sign_verify_roundtrip(keypair):
    private, public = keypair
    document = {"product": updating.PRODUCT, "version": "0.28.0", "platforms": {}}
    raw, signature = _signed_manifest(private, document)
    assert updating.verify_manifest(raw, signature, public)["version"] == "0.28.0"


def test_verify_refuses_every_tamper(keypair):
    private, public = keypair
    document = {"product": updating.PRODUCT, "version": "0.28.0", "platforms": {}}
    raw, signature = _signed_manifest(private, document)

    # one byte changed anywhere in the document
    tampered = json.loads(raw)
    tampered["version"] = "0.29.0"
    with pytest.raises(updating.UpdateError):
        updating.verify_manifest(updating.canonical_bytes(tampered), signature, public)

    # signature from another key
    _, other_public = generate_publisher_keypair()
    with pytest.raises(updating.UpdateError):
        updating.verify_manifest(raw, signature, other_public)

    # no signature at all / garbage
    with pytest.raises(updating.UpdateError):
        updating.verify_manifest(raw, "", public)
    with pytest.raises(updating.UpdateError):
        updating.verify_manifest(raw, "not base64 !!!", public)

    # a manifest for another product
    impostor = dict(document, product="some-other-app")
    _, impostor_sig = _signed_manifest(private, impostor)
    with pytest.raises(updating.UpdateError):
        updating.verify_manifest(updating.canonical_bytes(impostor), impostor_sig, public)


def test_signature_url_mapping():
    assert (
        updating.signature_url("https://x/y/update_manifest.json")
        == "https://x/y/update_manifest.sig"
    )
    assert updating.signature_url("https://x/manifest") == "https://x/manifest.sig"


# -------------------------------------------------------------- plan choice


def _platform_manifest(delta_from: str | None = None) -> dict:
    entry = {"url": "https://x/full.zip", "sha256": "aa", "size": 10}
    if delta_from:
        entry["deltas"] = {delta_from: {"url": "https://x/delta.zip", "sha256": "bb", "size": 4}}
    return {
        "product": updating.PRODUCT,
        "version": "0.28.0",
        "platforms": {"linux-x64": entry},
    }


def test_check_prefers_the_delta_for_the_running_version():
    plan = updating.check(_platform_manifest("0.27.0"), "0.27.0", platform="linux-x64")
    assert plan is not None and plan.kind == "delta"
    assert plan.url == "https://x/delta.zip"
    assert plan.from_version == "0.27.0"


def test_check_falls_back_to_full_without_a_delta():
    plan = updating.check(_platform_manifest(), "0.27.0", platform="linux-x64")
    assert plan is not None and plan.kind == "full"
    # a delta for a DIFFERENT from-version is not ours
    plan = updating.check(_platform_manifest("0.26.0"), "0.27.0", platform="linux-x64")
    assert plan is not None and plan.kind == "full"
    # and the full can be forced (the download fallback)
    plan = updating.check(
        _platform_manifest("0.27.0"), "0.27.0", platform="linux-x64", allow_delta=False
    )
    assert plan is not None and plan.kind == "full"


def test_check_returns_none_when_not_newer_or_not_offered():
    assert updating.check(_platform_manifest(), "0.28.0", platform="linux-x64") is None
    assert updating.check(_platform_manifest(), "0.99.0", platform="linux-x64") is None
    assert updating.check(_platform_manifest(), "0.27.0", platform="windows-x64") is None


def test_platform_key_is_a_manifest_name():
    assert updating.platform_key() in ("windows-x64", "macos-arm64", "macos-x64", "linux-x64")


# ------------------------------------------------------ fetch: fail closed


def test_http_fetch_refuses_non_https():
    with pytest.raises(updating.UpdateError):
        updating.http_fetch("http://example.com/manifest.json")


# ------------------------------------------------------- deltas, end to end


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    for relpath, content in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


OLD_FILES = {
    "LedgerForge.exe": "the old image",
    "_internal/engine.py": "engine 1",
    "_internal/legacy.py": "to be removed",
}
NEW_FILES = {
    "LedgerForge.exe": "the new image",  # changed
    "_internal/engine.py": "engine 2",  # changed
    "_internal/added.py": "brand new",  # added
}


@pytest.fixture()
def trees(tmp_path):
    old_dir = _write_tree(tmp_path / "old", OLD_FILES)
    new_dir = _write_tree(tmp_path / "new", NEW_FILES)
    return old_dir, new_dir


def _delta_bytes(old_dir: Path, new_dir: Path, tmp_path: Path) -> bytes:
    out = tmp_path / "delta.zip"
    document = updating.build_delta(old_dir, new_dir, "0.27.0", "0.28.0", out)
    assert set(document["files"]) == {
        "LedgerForge.exe",
        "_internal/engine.py",
        "_internal/added.py",
    }
    assert document["removed"] == ["_internal/legacy.py"]
    return out.read_bytes()


def test_delta_download_and_apply(trees, tmp_path):
    old_dir, new_dir = trees
    artifact = _delta_bytes(old_dir, new_dir, tmp_path)
    plan = updating.Plan(
        kind="delta",
        to_version="0.28.0",
        from_version="0.27.0",
        url="https://x/delta.zip",
        sha256=_sha(artifact),
        size=len(artifact),
    )

    data_dir = tmp_path / "data"
    pending = updating.download(plan, data_dir, fetch=lambda url: artifact)
    assert pending["to"] == "0.28.0"
    assert updating.pending_update(data_dir)["kind"] == "delta"

    applied = updating.apply_pending(data_dir, install_dir=old_dir)
    assert applied == "0.28.0"

    # the tree IS the new tree, to the byte
    for relpath, content in NEW_FILES.items():
        assert (old_dir / relpath).read_text(encoding="utf-8") == content
    assert not (old_dir / "_internal/legacy.py").exists()  # the removal happened
    assert not (data_dir / "update-staging").exists()  # staging cleaned
    assert updating.apply_pending(data_dir, install_dir=old_dir) is None  # nothing left


def test_delta_download_refuses_a_hash_mismatch(trees, tmp_path):
    old_dir, new_dir = trees
    artifact = _delta_bytes(old_dir, new_dir, tmp_path)
    plan = updating.Plan(
        kind="delta",
        to_version="0.28.0",
        from_version="0.27.0",
        url="https://x/delta.zip",
        sha256="00" * 32,  # not the artifact's hash
        size=len(artifact),
    )
    data_dir = tmp_path / "data"
    with pytest.raises(updating.UpdateError):
        updating.download(plan, data_dir, fetch=lambda url: artifact)
    assert updating.pending_update(data_dir) is None  # nothing staged


def test_delta_staging_refuses_unlisted_files(trees, tmp_path):
    old_dir, new_dir = trees
    artifact = _delta_bytes(old_dir, new_dir, tmp_path)
    # an extra file rides along inside the archive — the strict
    # set-equality check must refuse the whole delta
    smuggled = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(artifact)) as source, zipfile.ZipFile(smuggled, "w") as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
        target.writestr("_internal/smuggled.py", "print('pwned')")

    plan = updating.Plan(
        kind="delta",
        to_version="0.28.0",
        from_version="0.27.0",
        url="https://x/delta.zip",
        sha256=_sha(smuggled.getvalue()),
        size=smuggled.tell(),
    )
    with pytest.raises(updating.UpdateError):
        updating.download(plan, tmp_path / "data", fetch=lambda url: smuggled.getvalue())


def test_delta_staging_refuses_zip_slip(trees, tmp_path):
    old_dir, new_dir = trees
    artifact = _delta_bytes(old_dir, new_dir, tmp_path)
    evil = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(artifact)) as source, zipfile.ZipFile(evil, "w") as target:
        for name in source.namelist():
            if name == "delta.json":
                document = json.loads(source.read(name))
                document["files"]["../escape.py"] = "outside"
                target.writestr("delta.json", updating.canonical_bytes(document))
            else:
                target.writestr(name, source.read(name))
        target.writestr("../escape.py", "outside")

    plan = updating.Plan(
        kind="delta",
        to_version="0.28.0",
        from_version="0.27.0",
        url="https://x/delta.zip",
        sha256=_sha(evil.getvalue()),
        size=evil.tell(),
    )
    with pytest.raises(updating.UpdateError):
        updating.download(plan, tmp_path / "data", fetch=lambda url: evil.getvalue())
    assert not (tmp_path / "escape.py").exists()


def test_full_download_and_apply(trees, tmp_path):
    old_dir, new_dir = trees
    # the full package: one root folder, the PyInstaller onedir shape
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        for relpath, content in NEW_FILES.items():
            archive.writestr(f"LedgerForge/{relpath}", content)
    artifact = package.getvalue()

    plan = updating.Plan(
        kind="full",
        to_version="0.28.0",
        from_version=None,
        url="https://x/full.zip",
        sha256=_sha(artifact),
        size=len(artifact),
    )
    data_dir = tmp_path / "data"
    # an install dir with an extra stale file — the full update removes it
    stale = old_dir / "_internal/stale-from-before.py"
    stale.write_text("old junk", encoding="utf-8")

    pending = updating.download(plan, data_dir, fetch=lambda url: artifact)
    assert pending["kind"] == "full"
    assert updating.apply_pending(data_dir, install_dir=old_dir) == "0.28.0"

    for relpath, content in NEW_FILES.items():
        assert (old_dir / relpath).read_text(encoding="utf-8") == content
    assert not (old_dir / "_internal/legacy.py").exists()
    assert not stale.exists()  # not in the new tree — gone


def test_full_package_must_have_one_root(tmp_path):
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("LedgerForge/a.txt", "a")
        archive.writestr("stray.txt", "not allowed")
    plan = updating.Plan(
        kind="full",
        to_version="0.28.0",
        from_version=None,
        url="https://x/full.zip",
        sha256=_sha(package.getvalue()),
        size=package.tell(),
    )
    with pytest.raises(updating.UpdateError):
        updating.download(plan, tmp_path / "data", fetch=lambda url: package.getvalue())


def test_pending_survives_a_corrupt_staging_state(tmp_path):
    staging = tmp_path / "update-staging"
    staging.mkdir()
    (staging / "pending.json").write_text("{ not json", encoding="utf-8")
    assert updating.pending_update(tmp_path) is None  # ignored, never applied
    assert updating.apply_pending(tmp_path, install_dir=tmp_path) is None


def test_apply_is_idempotent_after_an_interrupted_run(trees, tmp_path):
    """The journal exists, the payload is staged, and apply runs again —
    the result is the same tree, no error (a power cut mid-apply is
    repaired by the next relaunch)."""

    old_dir, new_dir = trees
    artifact = _delta_bytes(old_dir, new_dir, tmp_path)
    plan = updating.Plan(
        kind="delta",
        to_version="0.28.0",
        from_version="0.27.0",
        url="https://x/delta.zip",
        sha256=_sha(artifact),
        size=len(artifact),
    )
    data_dir = tmp_path / "data"
    updating.download(plan, data_dir, fetch=lambda url: artifact)

    # simulate: the first apply copied ONE file and died
    staging = data_dir / "update-staging"
    (staging / "apply.json").write_text('{"to": "0.28.0"}', encoding="utf-8")
    shutil_copy = (staging / "payload" / "_internal" / "engine.py").read_text(encoding="utf-8")
    (old_dir / "_internal" / "engine.py").write_text(shutil_copy, encoding="utf-8")

    assert updating.apply_pending(data_dir, install_dir=old_dir) == "0.28.0"
    for relpath, content in NEW_FILES.items():
        assert (old_dir / relpath).read_text(encoding="utf-8") == content
    assert not (old_dir / "_internal/legacy.py").exists()


# ------------------------------------------------ the release pipeline's
# own scripts — the workflow runs these verbatim in its publish job, so
# the CLI seams are proven here too (in-process: the pinned key stays
# patchable, which the signed round-trip needs).


def _run_script(name: str, *argv: str, monkeypatch, capsys) -> tuple[int, str, str]:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(f"lf_script_{name}", root / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr("sys.argv", [name, *argv])
    code = module.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_delta_script_builds_the_file_level_delta(tmp_path, monkeypatch, capsys):
    old = tmp_path / "old"
    new = tmp_path / "new"
    (old / "_internal").mkdir(parents=True)
    (new / "_internal").mkdir(parents=True)
    (old / "_internal" / "kept.py").write_text("same", encoding="utf-8")
    (old / "_internal" / "gone.py").write_text("bye", encoding="utf-8")
    (new / "_internal" / "kept.py").write_text("same", encoding="utf-8")
    (new / "_internal" / "changed.py").write_text("v2", encoding="utf-8")
    out = tmp_path / "delta.zip"

    code, out_text, _err = _run_script(
        "make_delta.py",
        "--old-dir",
        str(old),
        "--new-dir",
        str(new),
        "--from",
        "0.27.0",
        "--to",
        "0.28.0",
        "--out",
        str(out),
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert code == 0, out_text
    assert "1 changed/added, 1 removed" in out_text
    with zipfile.ZipFile(out) as archive:
        document = json.loads(archive.read("delta.json"))
        assert document["removed"] == ["_internal/gone.py"]
        assert document["files"] == {"_internal/changed.py": _sha(b"v2")}
        assert archive.read("_internal/changed.py") == b"v2"


def test_manifest_script_writes_unsigned_with_a_loud_warning(tmp_path, monkeypatch, capsys):
    package = tmp_path / "pkg.zip"
    package.write_bytes(b"pkg")
    out = tmp_path / "update_manifest.json"

    code, out_text, out_err = _run_script(
        "make_update_manifest.py",
        "--tag",
        "v0.28.0",
        "--version",
        "0.28.0",
        "--artifact",
        f"linux-x64={package}",
        "--out",
        str(out),
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert code == 0, out_err
    assert "UNSIGNED" in out_text
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["platforms"]["linux-x64"]["sha256"] == _sha(b"pkg")
    assert document["platforms"]["linux-x64"]["size"] == 3


def test_manifest_script_refuses_a_key_that_is_not_the_pin(tmp_path, keypair, monkeypatch, capsys):
    """A manifest signed by any other key is dead on arrival — every client
    refuses it — so the script refuses to write one: fail at the pipeline,
    not in the field."""

    private_pem, _public = keypair
    package = tmp_path / "pkg.zip"
    package.write_bytes(b"pkg")
    key = tmp_path / "release-key.pem"
    key.write_text(private_pem, encoding="utf-8")

    code, _out_text, out_err = _run_script(
        "make_update_manifest.py",
        "--tag",
        "v0.28.0",
        "--version",
        "0.28.0",
        "--artifact",
        f"linux-x64={package}",
        "--key",
        str(key),
        "--out",
        str(tmp_path / "update_manifest.json"),
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert code == 1
    assert "not the pinned update key" in out_err
    assert not (tmp_path / "update_manifest.sig").exists()


def test_manifest_script_signs_and_the_client_verifies(tmp_path, keypair, monkeypatch, capsys):
    """The pipeline seam end to end: the script signs with the pinned key's
    own half (the pin swapped for the test keypair), and the updater's own
    verifier accepts what the script wrote — same bytes, same contract."""

    private_pem, public_b64 = keypair
    monkeypatch.setattr(updating, "PINNED_UPDATE_KEY", public_b64)
    package = tmp_path / "pkg.zip"
    package.write_bytes(b"pkg")
    key = tmp_path / "release-key.pem"
    key.write_text(private_pem, encoding="utf-8")
    out = tmp_path / "update_manifest.json"

    code, out_text, out_err = _run_script(
        "make_update_manifest.py",
        "--tag",
        "v0.28.0",
        "--version",
        "0.28.0",
        "--artifact",
        f"linux-x64={package}",
        "--key",
        str(key),
        "--out",
        str(out),
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert code == 0, out_err
    assert "Ed25519" in out_text

    document = updating.verify_manifest(
        out.read_bytes(), (tmp_path / "update_manifest.sig").read_text(encoding="utf-8")
    )
    assert document["version"] == "0.28.0"
    assert document["platforms"]["linux-x64"]["sha256"] == _sha(b"pkg")
