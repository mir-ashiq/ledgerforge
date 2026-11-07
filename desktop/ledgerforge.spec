# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — LedgerForge Native, Windows x64 (ADR-022).

Build from the repository root, on Windows:

    pyinstaller desktop/ledgerforge.spec --noconfirm

Output: ``dist/LedgerForge/LedgerForge.exe`` (onedir — the folder IS the
application; ship it zipped). ``.github/workflows/release-windows.yml`` runs
exactly this on ``windows-latest`` for every ``v*`` tag, smoke-tests the exe
offscreen (``--smoke``), and publishes the zip as a GitHub Release.

What this is: the NATIVE application — a Qt window (GUI subsystem, no
console), the engine in a spawn child process, an app icon, nothing else.
The v0.17 browser shell is gone: no web UI ships, none is opened.

What rides along, and why:

- ``db/schema.sql``, ``db/functions.sql``, ``db/migrations/`` land at
  ``ledgerforge/sql/...`` — the frozen bootstrap/migrations code finds them
  through its package-relative fallback, so no repo checkout is needed at
  runtime;
- the ``ledgerforge`` dist metadata — plugin discovery enumerates the
  ``ledgerforge.plugins`` entry-point group via importlib.metadata, which
  needs the installed-distribution metadata present in the bundle;
- pgserver's bundled PostgreSQL — the embedded database behind the engine
  child process (collect_all grabs the binaries);
- PySide6 — the window. PyInstaller's PySide6 hooks bundle the Qt plugins
  (platforms — windows AND offscreen for ``--smoke`` —, styles, image
  formats) and the DLLs; ``shiboken6`` rides along automatically;
- the icon — taskbar, title bar, and the exe's resource (version_info
  carries the properties; both generated, never hand-edited).
"""

import os

from PyInstaller.utils.hooks import collect_all, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

pg_datas, pg_binaries, pg_hiddenimports = collect_all("pgserver")

datas = [
    # the SQL bootstrap + the Alembic tree, at the package-relative fallback
    (os.path.join(ROOT, "db", "schema.sql"), "ledgerforge/sql"),
    (os.path.join(ROOT, "db", "functions.sql"), "ledgerforge/sql"),
    (os.path.join(ROOT, "db", "migrations"), "ledgerforge/sql/migrations"),
    # the application icon: the window finds it under _MEIPASS at runtime
    (os.path.join(SPECPATH, "ledgerforge.ico"), "desktop"),
    # dist metadata: entry_points(group="ledgerforge.plugins") at runtime
    *copy_metadata("ledgerforge"),
] + pg_datas

hiddenimports = [
    # entry-point plugins are imported by name at runtime — invisible to trace
    "ledgerforge.contrib.gst",
    "ledgerforge.contrib.invoicing",
    "ledgerforge.contrib.inventory",
    "ledgerforge.contrib.payroll",
    "ledgerforge.contrib.statutory",
    "ledgerforge.contrib.example_slot",
    # uvicorn selects protocol implementations dynamically (engine child)
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools.impl",
    "uvicorn.lifespan.on",
    # the engine child process (spawn) — the target PyInstaller cannot trace
    "ledgerforge.nativeapp.app",
] + pg_hiddenimports

# Generated from ledgerforge.__version__ by scripts/make_version_info.py —
# the .exe properties track the package, never hand-edited.
VERSION_FILE = os.path.join(SPECPATH, "version_info.txt")
ICON_FILE = os.path.join(SPECPATH, "ledgerforge.ico")

a = Analysis(
    [os.path.join(SPECPATH, "run_native.py")],
    pathex=[ROOT, os.path.join(ROOT, "src")],
    binaries=pg_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LedgerForge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI subsystem: no console window, ever
    icon=ICON_FILE if os.path.exists(ICON_FILE) else None,
    version=VERSION_FILE if os.path.exists(VERSION_FILE) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LedgerForge",
)
