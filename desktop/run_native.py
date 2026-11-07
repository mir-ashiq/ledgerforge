"""LedgerForge Native — the PyInstaller entry point (ADR-022).

A plain top-level script (no package-relative imports) so the frozen
executable boots the native application. ``freeze_support()`` comes first:
the engine runs in a multiprocessing spawn child, and on Windows the
PyInstaller bootloader re-executes this exe for every child — without the
guard each "child" would try to open another window instead of serving.

The result is a windowed executable (GUI subsystem): double-click
LedgerForge.exe, the native window opens. No console, no browser.
"""

import multiprocessing

# MUST be the first statement: frozen spawn children re-enter here.
multiprocessing.freeze_support()

from ledgerforge.nativeapp import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
