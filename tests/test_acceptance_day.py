"""The accountant's day, end to end — the release gate beyond the smoke
(ADR-032).

Exactly what the release workflow runs on every platform leg after
``--smoke``: one fresh embedded engine, one book, fifty vouchers across
every pack in one continuous session, the reconciliation proof, the GST
statements, the payroll run, the year close, and the window itself
rendering the day. Exit 0 and ``DAY OK``, or the release does not ship.
"""

from __future__ import annotations

import os
import subprocess
import sys

DAY_TIMEOUT = 600


def test_the_accountants_day(tmp_path_factory):
    """First boot provisions (initdb + seed) and runs the whole day; the
    second run takes the migrate path over the surviving cluster — the
    day must hold on both, exactly like a user's second launch."""
    data = tmp_path_factory.mktemp("lf-day-data")
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH", ""),
    }

    for attempt in (1, 2):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ledgerforge.nativeapp",
                "--accountants-day",
                "--data-dir",
                str(data),
            ],
            capture_output=True,
            text=True,
            timeout=DAY_TIMEOUT,
            env=env,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"the day, attempt {attempt}, failed:\n{combined}"
        assert "DAY OK" in result.stdout
