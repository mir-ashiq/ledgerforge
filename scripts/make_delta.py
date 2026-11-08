#!/usr/bin/env python3
"""Build a file-level delta between two application trees (ADR-031).

    python scripts/make_delta.py \
        --old-dir extracted/0.27.0/LedgerForge \
        --new-dir extracted/0.28.0/LedgerForge \
        --from 0.27.0 --to 0.28.0 \
        --out dist/LedgerForge-0.27.0-to-0.28.0-win64-delta.zip

The delta zip carries ``delta.json`` (the contract: every changed/added
file with its SHA-256, plus the removals list) and the changed files
themselves. The updater verifies strict set-equality against the manifest
before staging anything — see ``ledgerforge/updating.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--old-dir", type=Path, required=True)
    parser.add_argument("--new-dir", type=Path, required=True)
    parser.add_argument("--from", dest="from_version", required=True)
    parser.add_argument("--to", dest="to_version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    for name, path in (("--old-dir", args.old_dir), ("--new-dir", args.new_dir)):
        if not path.is_dir():
            print(f"error: {name} is not a directory: {path}", file=sys.stderr)
            return 1

    from ledgerforge.updating import build_delta

    document = build_delta(args.old_dir, args.new_dir, args.from_version, args.to_version, args.out)
    changed = len(document["files"])
    removed = len(document["removed"])
    size = args.out.stat().st_size
    print(
        f"wrote {args.out} ({size} bytes): {changed} changed/added, "
        f"{removed} removed — {args.from_version} -> {args.to_version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
