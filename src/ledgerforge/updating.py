"""Application updates — a manifest that verifies itself (ADR-031).

The protocol, end to end:

1. ``check(url, current_version)`` fetches ``update_manifest.json`` and its
   detached ``.sig``, and verifies the Ed25519 signature against
   :data:`PINNED_UPDATE_KEY` **before anything else happens** — a manifest
   that does not verify is not compared, not parsed for versions, not
   used. Fail closed is the law here (ADR-016's discipline, aimed at the
   application itself).
2. A :class:`Plan` is chosen: the file-level **delta** for the running
   version when the manifest offers one, else the **full** package. Both
   carry their SHA-256 inside the signed manifest.
3. :func:`download` fetches the artifact, verifies its hash, unpacks and
   re-verifies every file it contains (a delta's ``delta.json`` lists each
   file's hash; strict set-equality — no unlisted byte rides along), and
   writes ``pending.json`` — the only commit point. Any failure leaves
   ``pending.json`` unwritten, and the next boot ignores the staging
   directory entirely.
4. :func:`apply_pending` runs at the next relaunch, before Qt and before
   the engine child: an apply journal, then in-place copies and removals
   (idempotent — a power cut mid-apply is repaired by re-running), then
   cleanup. On Windows a running ``LedgerForge.exe`` cannot be overwritten,
   so it is renamed to ``.old`` first and swept on a later boot.

Every network touch goes through an injectable ``fetch(url) -> bytes``
callable, so the whole protocol is testable without a socket.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import platform
import shutil
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .plugins.trust import load_private_key, load_public_key

#: The product name every manifest must carry — an app updater that would
#: happily install some other product's release is a confused deputy.
PRODUCT = "ledgerforge-native"

#: The pinned Ed25519 public half (base64, raw 32 bytes) of the release
#: signing key. The private half exists only as the GitHub Actions secret
#: ``LF_UPDATE_SIGNING_KEY``; releases sign themselves in the pipeline.
#: Fingerprint: C189:C1B9:CBCB:3C73 (shown in the About box).
PINNED_UPDATE_KEY = "/xjrZX4M6oDz/xCdBhNkCr4WPaOuKAmGoAwj06yz/xA="

#: A short, printable identity for the pinned key (sha256 of the b64 text,
#: first 16 hex, colon-grouped — the ADR-016 fingerprint form).
KEY_FINGERPRINT = ":".join(
    hashlib.sha256(PINNED_UPDATE_KEY.encode()).hexdigest()[:16][i : i + 4].upper()
    for i in range(0, 16, 4)
)

#: Where the published releases live; the GitHub "latest download"
#: redirect points at the newest non-prerelease release's assets.
DEFAULT_UPDATE_URL = (
    "https://github.com/mir-ashiq/ledgerforge/releases/latest/download/update_manifest.json"
)

#: The staging directory (inside the DATA directory, never the install dir).
STAGING_DIRNAME = "update-staging"


class UpdateError(Exception):
    """The update path refused something — fail closed, loudly."""


# ------------------------------------------------------------------ versions


def _identifier_key(identifier: object) -> tuple[int, object]:
    """PEP-440's rule: numeric identifiers always compare below textual
    ones — ``1.0.0-1 < 1.0.0-alpha`` — so each is keyed by kind first."""

    if isinstance(identifier, int):
        return (0, identifier)
    return (1, str(identifier))


@dataclass(frozen=True)
class _Version:
    """Dotted-numeric version with PEP-440-style pre-release ordering:
    ``1.0.0-rc.1 < 1.0.0``, numeric identifiers compare as ints, textual
    ones lexically, and a release outranks any of its pre-releases."""

    release: tuple[int, ...]
    pre: tuple[object, ...]

    def __lt__(self, other: _Version) -> bool:
        if self.release != other.release:
            return self.release < other.release
        if not self.pre:
            return False  # a release is never below a pre-release
        if not other.pre:
            return True  # a pre-release is below its release
        left = [_identifier_key(i) for i in self.pre]
        right = [_identifier_key(i) for i in other.pre]
        return left < right  # pairwise, kind-aware


def parse_version(text: str) -> _Version:
    """``"1.2.0-rc.3"`` → ``_Version((1, 2, 0), ("rc", 3))``."""

    core, _, pre_text = str(text).strip().partition("-")
    numbers: list[int] = []
    for part in core.split("."):
        if not part.isdigit():
            raise UpdateError(f"unparseable version: {text!r}")
        numbers.append(int(part))
    pre: list[object] = []
    if pre_text:
        for part in pre_text.replace("-", ".").split("."):
            pre.append(int(part) if part.isdigit() else part)
    return _Version(tuple(numbers), tuple(pre))


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)  # type: ignore[operator]


# ------------------------------------------------------------ the manifest


def canonical_bytes(document: dict) -> bytes:
    """One canonical form for signing — sorted keys, no whitespace
    (ADR-016's ``canonical_payload`` discipline, restated)."""

    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def sign_manifest(document: dict, private_pem: str) -> str:
    """The detached signature (base64) over the manifest's canonical bytes."""

    private = load_private_key(private_pem)
    return base64.b64encode(private.sign(canonical_bytes(document))).decode()


def verify_manifest(raw: bytes, signature_b64: str, public_key_b64: str | None = None):
    """Verify + parse a manifest. Raises :class:`UpdateError` on any
    tampering, bad format, or missing signature — never returns partial
    trust. The signature covers the document's **canonical form**
    (sorted keys, no whitespace): formatting is not part of the contract,
    content is — the pipeline may pretty-print the file it publishes while
    the signature stays canonical. The key defaults to the pinned release
    key (read at call time, so operators can pin their own distribution's
    key by overriding the module attribute)."""

    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise UpdateError("the update manifest is not valid JSON") from exc
    if not isinstance(document, dict):
        raise UpdateError("the update manifest is not a JSON object")
    try:
        signature = base64.b64decode(signature_b64.strip(), validate=True)
    except Exception as exc:  # noqa: BLE001 — decode failure is refusal
        raise UpdateError("the manifest signature is not valid base64") from exc
    try:
        key = load_public_key(public_key_b64 or PINNED_UPDATE_KEY)
    except Exception as exc:  # noqa: BLE001
        raise UpdateError(f"the pinned update key is unusable: {exc}") from exc
    try:
        key.verify(signature, canonical_bytes(document))
    except Exception as exc:  # noqa: BLE001
        raise UpdateError("the update manifest failed signature verification") from exc
    if document.get("product") != PRODUCT:
        raise UpdateError(f"the manifest is for {document.get('product')!r}, not {PRODUCT!r}")
    parse_version(str(document.get("version", "")))  # a manifest without a
    # parseable version cannot be compared — refuse it now, not later
    return document


def signature_url(manifest_url: str) -> str:
    """``…/update_manifest.json`` → ``…/update_manifest.sig``."""

    if manifest_url.endswith(".json"):
        return manifest_url[: -len(".json")] + ".sig"
    return manifest_url + ".sig"


# ------------------------------------------------------------- the platforms


def platform_key() -> str:
    """The manifest's name for the machine we run on."""

    machine = platform.machine().lower()
    if sys.platform == "win32":
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x64"
    return "linux-x64"


@dataclass(frozen=True)
class Plan:
    """One installable step: what to fetch, what it will do."""

    kind: str  # "delta" | "full"
    to_version: str
    from_version: str | None
    url: str
    sha256: str
    size: int


def check(
    manifest: dict, current_version: str, platform: str | None = None, *, allow_delta: bool = True
) -> Plan | None:
    """Choose the plan from a *verified* manifest: the delta for the running
    version when offered, else the full package; ``None`` when the manifest
    is not newer. Platform entries carry ``deltas`` maps keyed by the
    version they upgrade *from*. ``allow_delta=False`` asks for the full
    package even when a delta exists (the download fallback)."""

    key = platform or platform_key()
    entry = manifest.get("platforms", {}).get(key)
    if not entry:
        return None  # nothing offered for this platform — not an error
    target = str(manifest["version"])
    if not is_newer(target, current_version):
        return None
    delta = (entry.get("deltas") or {}).get(current_version) if allow_delta else None
    if delta:
        return Plan(
            kind="delta",
            to_version=target,
            from_version=current_version,
            url=delta["url"],
            sha256=delta["sha256"],
            size=int(delta.get("size", 0)),
        )
    return Plan(
        kind="full",
        to_version=target,
        from_version=None,
        url=entry["url"],
        sha256=entry["sha256"],
        size=int(entry.get("size", 0)),
    )


# ----------------------------------------------------------------- fetching


def _check_url(url: str) -> None:
    scheme = url.split(":", 1)[0].lower()
    if scheme not in ("https", "file"):
        raise UpdateError(f"refusing non-https update URL: {url!r}")


def http_fetch(url: str, timeout: float = 120.0) -> bytes:
    """The default fetch: httpx with redirects (GitHub's "latest download"
    redirects), https or file only."""

    import httpx

    _check_url(url)
    if url.startswith("file:"):
        from urllib.request import urlopen

        with urlopen(url) as response:  # noqa: S310 - scheme checked above
            return response.read()
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ deltas


def build_delta(
    old_dir: Path, new_dir: Path, from_version: str, to_version: str, out_path: Path
) -> dict:
    """Create a file-level delta zip: every changed/added file (with its
    SHA-256) plus the removals list. ``delta.json`` inside the zip is the
    contract; the updater verifies strict set-equality against it."""

    old_files = {
        str(p.relative_to(old_dir)): _sha256_file(p)
        for p in sorted(old_dir.rglob("*"))
        if p.is_file()
    }
    new_files = {
        str(p.relative_to(new_dir)): _sha256_file(p)
        for p in sorted(new_dir.rglob("*"))
        if p.is_file()
    }
    changed = {p: h for p, h in new_files.items() if old_files.get(p) != h}
    removed = sorted(p for p in old_files if p not in new_files)
    document = {
        "product": PRODUCT,
        "format": "file-level-v1",
        "from": from_version,
        "to": to_version,
        "files": changed,
        "removed": removed,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("delta.json", canonical_bytes(document))
        for relpath in sorted(changed):
            archive.write(new_dir / relpath, relpath)
    return document


# ------------------------------------------------------- staging + applying


def _staging(data_dir: Path) -> Path:
    return data_dir / STAGING_DIRNAME


def pending_update(data_dir: Path) -> dict | None:
    """The staged-and-verified update awaiting the next relaunch, if any."""

    pending = _staging(data_dir) / "pending.json"
    if not pending.is_file():
        return None
    try:
        document = json.loads(pending.read_text(encoding="utf-8"))
        parse_version(str(document.get("to", "")))
    except (ValueError, UpdateError):
        return None  # a corrupt staging state is ignored, never applied
    return document


def download(
    plan: Plan,
    data_dir: Path,
    fetch: Callable[[str], bytes] = http_fetch,
) -> dict:
    """Fetch + verify + stage one plan, and write ``pending.json``.

    A delta that cannot be fetched falls back to the full package (the
    caller passes the full plan); verification failures raise and stage
    nothing. The return value is the pending document."""

    staging = _staging(data_dir)
    if staging.exists():
        shutil.rmtree(staging)  # a fresh download replaces stale staging
    payload = staging / "payload"
    payload.mkdir(parents=True)

    artifact = fetch(plan.url)
    digest = hashlib.sha256(artifact).hexdigest()
    if digest != plan.sha256:
        raise UpdateError(
            f"the {plan.kind} artifact failed SHA-256 verification "
            f"(got {digest}, manifest says {plan.sha256})"
        )

    if plan.kind == "delta":
        document = _stage_delta(artifact, payload)
        pending = {
            "kind": "delta",
            "from": plan.from_version,
            "to": plan.to_version,
            "files": document["files"],
            "removed": document["removed"],
        }
    else:
        files = _stage_full(artifact, payload)
        pending = {"kind": "full", "from": None, "to": plan.to_version, "files": files}
    (staging / "pending.json").write_text(
        json.dumps(pending, sort_keys=True, indent=2), encoding="utf-8"
    )
    return pending


def _check_archive_paths(archive: zipfile.ZipFile) -> None:
    """Zip-slip refused: every member must land inside the payload."""

    for name in archive.namelist():
        if name.startswith(("/", "\\")) or ":" in name.split("/", 1)[0]:
            raise UpdateError(f"refusing an absolute archive path: {name!r}")
        if any(part == ".." for part in Path(name).parts):
            raise UpdateError(f"refusing a traversal archive path: {name!r}")


def _stage_delta(artifact: bytes, payload: Path) -> dict:
    """Unpack + strictly verify a delta zip against its own manifest."""

    import io

    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        _check_archive_paths(archive)
        names = set(archive.namelist())
        if "delta.json" not in names:
            raise UpdateError("the delta archive carries no delta.json")
        document = json.loads(archive.read("delta.json"))
        if document.get("format") != "file-level-v1":
            raise UpdateError(f"unknown delta format: {document.get('format')!r}")
        listed = set(document["files"])
        payload_files = names - {"delta.json"}
        if payload_files != listed:
            extra = sorted(payload_files - listed)
            missing = sorted(listed - payload_files)
            raise UpdateError(
                f"the delta archive does not match its manifest "
                f"(extra: {extra[:3]}, missing: {missing[:3]})"
            )
        archive.extractall(payload)  # names verified against the manifest
    # verify every payload file's hash before pending.json exists
    for relpath, expected in document["files"].items():
        staged = payload / relpath
        if not staged.is_file():
            raise UpdateError(f"the delta is missing its file: {relpath}")
        if _sha256_file(staged) != expected:
            raise UpdateError(f"a delta file failed verification: {relpath}")
    document["removed"] = list(document.get("removed", []))
    return document


def _stage_full(artifact: bytes, payload: Path) -> dict:
    """Extract the full package (a single top-level application folder —
    the PyInstaller onedir layout) into the staging payload, and hash every
    file: the full update rides the same verified-files discipline as a
    delta."""

    import io

    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        _check_archive_paths(archive)
        roots = {name.split("/", 1)[0] for name in archive.namelist() if name.strip()}
        if len(roots) != 1:
            raise UpdateError(f"the full package must have one root folder, got {sorted(roots)}")
        archive.extractall(payload)
    app_dir = payload / next(iter(sorted(roots)))
    files = {
        str(p.relative_to(app_dir)): _sha256_file(p)
        for p in sorted(app_dir.rglob("*"))
        if p.is_file()
    }
    if not files:
        raise UpdateError("the full package extracted to nothing")
    # promote the application folder to the payload root, so delta and
    # full updates share one apply path
    incoming = payload.parent / "incoming"
    shutil.rmtree(incoming, ignore_errors=True)
    shutil.move(str(app_dir), str(incoming))
    shutil.rmtree(payload)
    shutil.move(str(incoming), str(payload))
    return files


def _install_dir() -> Path | None:
    """Where the running application lives, as the *root the release
    package names*. On Windows and Linux that is the onedir folder beside
    the executable; on macOS it is the .app bundle itself — the zip's root
    and the delta's paths are relative to ``LedgerForge.app``, whose
    ``Contents/MacOS`` holds the executable. A source checkout refuses to
    self-modify."""

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if sys.platform == "darwin":
            return exe_dir.parent.parent  # .../LedgerForge.app
        return exe_dir
    return None


def apply_pending(data_dir: Path, install_dir: Path | None = None) -> str | None:
    """Apply the staged update at relaunch. Returns the version applied, or
    ``None`` when nothing was staged. Idempotent: an interrupted apply
    re-runs cleanly (copies and unlinks are), which is what the journal is
    for. In a frozen app the launcher calls this before Qt exists."""

    target = install_dir if install_dir is not None else _install_dir()
    pending = pending_update(data_dir)
    if pending is None or target is None:
        return None
    staging = _staging(data_dir)
    payload = staging / "payload"
    journal = staging / "apply.json"

    files: dict[str, str] = pending["files"]
    removed: list[str] = pending.get("removed", [])
    if pending["kind"] == "full":
        # everything in the install dir that the new tree does not carry
        # goes away — computed here, where the install dir is finally known
        keep = set(files)
        removed = sorted(
            str(p.relative_to(target))
            for p in target.rglob("*")
            if p.is_file() and str(p.relative_to(target)) not in keep
        )

    # verify the payload against pending.json before touching the install
    for relpath, expected in files.items():
        staged = payload / relpath
        if not staged.is_file() or _sha256_file(staged) != expected:
            raise UpdateError(f"the staged update failed verification: {relpath}")

    journal.write_text(
        json.dumps({"to": pending["to"], "files": len(files)}, sort_keys=True),
        encoding="utf-8",
    )
    try:
        for relpath in removed:
            (target / relpath).unlink(missing_ok=True)
        for relpath in sorted(files):
            destination = target / relpath
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_file() and destination.suffix.lower() == ".exe":
                # Windows locks a running image against overwrite, not
                # against rename — the old exe steps aside and is swept
                # on a later boot
                destination.rename(destination.with_suffix(".exe.old"))
            shutil.copyfile(payload / relpath, destination)
    finally:
        journal.unlink(missing_ok=True)
    shutil.rmtree(staging, ignore_errors=True)

    # sweep any .old images left by previous applies (best effort)
    for stale in target.rglob("*.exe.old"):
        with contextlib.suppress(OSError):  # locked today, swept next time
            stale.unlink()
    return str(pending["to"])


def report(data_dir: Path) -> dict | None:
    """What the status bar wants to say: the staged update, if any."""

    return pending_update(data_dir)
