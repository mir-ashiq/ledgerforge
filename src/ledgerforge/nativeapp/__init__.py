"""LedgerForge Native — the Qt 6 desktop application (ADR-022).

The v0.17 desktop edition booted the engine and pointed a browser tab at it;
this package replaces that tab with a real native program: a Qt main window
with its own menus, tables and file dialogs, keyboard-first like the engine
it drives. No browser is launched, no URL is opened — the window IS the
product.

Architecture (ADR-022): the embedded PostgreSQL + FastAPI engine from ADR-021
keeps running in-process on a loopback port — every rule, trigger and report
stays server-side, one code path for web, Docker and native — and the Qt
shell talks to it over ``127.0.0.1`` like any other API client.

    ledgerforge-native                     # the window
    ledgerforge-native --smoke             # offscreen self-check, exit 0/1

The Windows binary is produced by ``desktop/ledgerforge.spec`` (PyInstaller,
GUI subsystem — no console window) and published by ``release-windows`` on
every ``v*`` tag.
"""

from .app import main

__all__ = ["main"]
