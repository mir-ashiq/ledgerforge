"""The native smoke, end to end — the release gate, run as a subprocess.

Exactly what the release workflow runs on the Windows runner: boot the
embedded PostgreSQL, provision the schema, start the engine, construct the
Qt main window offscreen, and pull the day book + trial balance + the
GSTR-1 pack through the real client. Exit 0 and SMOKE OK or the build does
not ship.
"""

from __future__ import annotations

import os
import subprocess
import sys

from ledgerforge import __version__

SMOKE_TIMEOUT = 600


def test_native_smoke_boots_the_full_app(tmp_path_factory):
    """First boot provisions (initdb + seed); the second takes the migrate
    path over the surviving cluster — both must prove the window, exactly
    like a user's second launch."""
    data = tmp_path_factory.mktemp("lf-native-data")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}

    for attempt in (1, 2):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ledgerforge.nativeapp",
                "--smoke",
                "--data-dir",
                str(data),
            ],
            capture_output=True,
            text=True,
            timeout=SMOKE_TIMEOUT,
            env=env,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"smoke attempt {attempt} failed:\n{combined}"
        assert "SMOKE OK" in result.stdout
        assert __version__ in result.stdout
