#!/usr/bin/env python3
"""Verify the frozen Alembic baseline (db/migrations/versions/0001_baseline.py).

Policy (ADR-006, amended 2026-11-06): the baseline revision is FROZEN — the
first forward revisions (0002_multi_currency, 0003_period_locking) shipped
against it, so it can never be regenerated from the live SQL files again.
This script now proves the opposite invariant:

  0001_baseline.py still embeds, byte-for-byte, the schema as it stood at the
  freeze — the snapshot preserved under db/migrations/baseline/.

db/schema.sql + db/functions.sql remain the flattened *current* state and are
kept equivalent to `alembic upgrade head` by the structural drift guard in
tests/test_migrations.py (a much stronger check than byte comparison).

Usage:  python scripts/regen_baseline.py --check   (CI; exit 1 on tampering)

The old regenerate behavior is intentionally gone: rewriting the baseline
would desynchronize every already-migrated database. If the baseline really
must change, that is a new decision — read ADR-006 first.
"""

from __future__ import annotations

import difflib
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = REPO_ROOT / "db" / "migrations" / "baseline"
SNAPSHOTS = (SNAPSHOT_DIR / "schema.sql", SNAPSHOT_DIR / "functions.sql")
BASELINE = REPO_ROOT / "db" / "migrations" / "versions" / "0001_baseline.py"

#: what the baseline module must still contain, verbatim
_EMBED_RE = {
    "SCHEMA_SQL": re.compile(r'SCHEMA_SQL = """\\\n(.*?)"""\n', re.S),
    "FUNCTIONS_SQL": re.compile(r'FUNCTIONS_SQL = """\\\n(.*?)"""\n', re.S),
}


def _fail(diff: str) -> int:
    print(
        "0001_baseline.py no longer embeds the frozen snapshot under "
        "db/migrations/baseline/ — the baseline is frozen (ADR-006); schema "
        "changes ship as NEW revisions that also update db/schema.sql + "
        "db/functions.sql.\n",
        file=sys.stderr,
    )
    print(diff, file=sys.stderr)
    return 1


def main() -> int:
    baseline = BASELINE.read_text()
    problems = 0
    for path, (name, _) in zip(SNAPSHOTS, _EMBED_RE.items(), strict=True):
        snapshot = path.read_text().rstrip("\n")
        match = _EMBED_RE[name].search(baseline)
        if match is None:
            print(f"baseline file has no {name} block", file=sys.stderr)
            problems += 1
            continue
        embedded = match.group(1)
        if embedded != snapshot:
            diff = "\n".join(
                difflib.unified_diff(
                    embedded.splitlines(),
                    snapshot.splitlines(),
                    f"0001_baseline.py ({name})",
                    f"{path.relative_to(REPO_ROOT)} (frozen)",
                    lineterm="",
                )
            )
            _fail(diff)
            problems += 1
    if problems:
        return 1
    print("baseline is frozen and intact (matches db/migrations/baseline/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
