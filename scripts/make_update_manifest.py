#!/usr/bin/env python3
"""Build + sign the release's update manifest (ADR-031).

    python scripts/make_update_manifest.py \
        --tag v0.28.0 --version 0.28.0 \
        --artifact windows-x64=dist/LedgerForge-v0.28.0-win64.zip \
        --artifact macos-arm64=dist/LedgerForge-v0.28.0-macos-arm64.zip \
        --artifact linux-x64=dist/LedgerForge-v0.28.0-linux-x64.tar.gz \
        --delta windows-x64=0.27.0=dist/LedgerForge-0.27.0-to-0.28.0-win64-delta.zip \
        --key /path/to/release-private.pem \
        --out dist/update_manifest.json

The manifest's artifact URLs derive from the repository's release download
path and the file's own name; SHA-256 and size are measured from the bytes
on disk. ``--key`` signs (Ed25519, the pinned key's other half — the
workflow reads it from the ``LF_UPDATE_SIGNING_KEY`` secret); without a key
the manifest is written unsigned with a loud warning, because local test
runs need the format, and the client refuses unsigned manifests anyway —
fail closed at the far end.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = "mir-ashiq/ledgerforge"
RELEASE_BASE = f"https://github.com/{REPO}/releases/download"


def _digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True, help="the release tag, e.g. v0.28.0")
    parser.add_argument("--version", required=True, help="the version, e.g. 0.28.0")
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="PLATFORM=PATH",
        help="a release package, e.g. windows-x64=dist/LedgerForge-v0.28.0-win64.zip",
    )
    parser.add_argument(
        "--delta",
        action="append",
        default=[],
        metavar="PLATFORM=FROM=PATH",
        help="a delta package, e.g. windows-x64=0.27.0=dist/delta.zip",
    )
    parser.add_argument("--key", type=Path, default=None, help="the release signing key (PEM)")
    parser.add_argument("--out", type=Path, default=Path("update_manifest.json"))
    args = parser.parse_args()

    platforms: dict[str, dict] = {}
    for spec in args.artifact:
        platform, _, path_text = spec.partition("=")
        path = Path(path_text)
        if not path.is_file():
            print(f"error: no such artifact: {path}", file=sys.stderr)
            return 1
        sha, size = _digest(path)
        platforms[platform] = {
            "url": f"{RELEASE_BASE}/{args.tag}/{path.name}",
            "sha256": sha,
            "size": size,
            "format": "zip" if path.suffix == ".zip" else "tar.gz",
        }
    for spec in args.delta:
        # PLATFORM=FROM=PATH — two '=' signs; split from the right for the path
        left, _, path_text = spec.rpartition("=")
        platform, _, from_version = left.partition("=")
        if not platform or not from_version:
            print(f"error: bad --delta (want PLATFORM=FROM=PATH): {spec}", file=sys.stderr)
            return 1
        path = Path(path_text)
        if not path.is_file():
            print(f"error: no such delta: {path}", file=sys.stderr)
            return 1
        sha, size = _digest(path)
        platforms.setdefault(platform, {}).setdefault("deltas", {})[from_version] = {
            "url": f"{RELEASE_BASE}/{args.tag}/{path.name}",
            "sha256": sha,
            "size": size,
        }

    manifest = {
        "product": "ledgerforge-native",
        "version": args.version,
        "released_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "platforms": platforms,
        "notes_url": f"https://github.com/{REPO}/releases/tag/{args.tag}",
    }

    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    if args.key is None:
        print(
            "WARNING: unsigned manifest (no --key) — the client will refuse it; "
            "releases sign with the LF_UPDATE_SIGNING_KEY secret",
            file=sys.stderr,
        )
        print(f"wrote {args.out} (UNSIGNED)")
        return 0

    from ledgerforge.updating import KEY_FINGERPRINT, sign_manifest

    signature = sign_manifest(manifest, args.key.read_text())
    sig_path = args.out.with_suffix(".sig")
    sig_path.write_text(signature, encoding="utf-8")
    print(f"wrote {args.out} + {sig_path} (Ed25519 {KEY_FINGERPRINT})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
