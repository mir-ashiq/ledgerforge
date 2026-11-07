"""LedgerForge Desktop — the PyInstaller entry point (ADR-021).

A plain top-level script (no package-relative imports) so the frozen
executable boots the real desktop CLI; the ``ledgerforge-desktop`` console
script does the same job in a pip install. Everything else — the embedded
PostgreSQL boot, schema provisioning, the same-origin web UI — lives in
``ledgerforge.desktop``.
"""

from ledgerforge.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
