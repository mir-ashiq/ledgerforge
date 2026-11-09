# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — LedgerForge Native, three platforms (ADR-022/031).

Build from the repository root, on the target platform:

    pyinstaller desktop/ledgerforge.spec --noconfirm

Output: ``dist/LedgerForge`` (onedir — the folder IS the application).
``.github/workflows/release-native.yml`` runs exactly this on Windows x64,
macOS arm64 and Linux x64 for every ``v*`` tag, smoke-tests the shipped
artifact offscreen (``--smoke``), and publishes the packages as a GitHub
Release with a signed update manifest.

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
- the icon — taskbar, title bar, and (per platform) the exe's resource or
  the .app bundle's icns; both generated from ``ledgerforge.__version__``
  and the glyph, never hand-edited.

Platform branches: the Windows exe carries the version resource (generated
from ``ledgerforge.__version__`` by ``scripts/make_version_info.py``) and
the .ico; macOS wraps the onedir in a .app bundle (BUNDLE) with the .icns
and a proper Info.plist; Linux ships the plain onedir (the .desktop file
and PNG ride beside it in the tarball).
"""

import os
import sys

import pgserver as _pgserver_pkg
from PyInstaller.utils.hooks import collect_all, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

pg_datas, pg_binaries, pg_hiddenimports = collect_all("pgserver")

# PostgreSQL's own distribution is a payload, not a Python dependency —
# ship it verbatim, in the exact layout the wheel carries.
#
# PyInstaller's collect_dynamic_libs matches only `lib*.so` on Unix
# (PY_DYLIB_PATTERNS), but PostgreSQL's runtime-loadable extension
# modules under pginstall/lib/postgresql/ (dict_snowball.so, plpgsql.so,
# pgoutput.so, the utf8_and_* converters...) carry no `lib` prefix — so
# collect_all silently DROPPED them on Linux and macOS, and the frozen
# app died inside initdb ("could not access file $libdir/dict_snowball").
# On Windows `*.dll` happens to match every extension, which is why the
# v0.18–v0.27 Windows builds never tripped this. Bundling the whole tree
# as data files is deterministic on all three platforms, keeps $libdir
# resolution (the postgres binaries' own directory logic) untouched, and
# removes PyInstaller's binary analysis from a tree it does not own.
_pgs_root = os.path.dirname(_pgserver_pkg.__file__)  # .../site-packages/pgserver
_pgs_parent = os.path.dirname(_pgs_root)  # .../site-packages
_pginstall = os.path.join(_pgs_root, "pginstall")
pg_datas = [
    (s, d) for s, d in pg_datas if "pginstall" not in d.replace("\\", "/")
]
pg_binaries = [
    (s, d) for s, d in pg_binaries if "pginstall" not in d.replace("\\", "/")
]
for _root, _dirs, _files in os.walk(_pginstall):
    for _file in _files:
        _src = os.path.join(_root, _file)
        _dest_dir = os.path.dirname(os.path.relpath(_src, _pgs_parent)).replace(os.sep, "/")
        pg_datas.append((_src, _dest_dir))

# --- psycopg's own libpq (macOS only) -----------------------------------
#
# psycopg-binary's macOS wheels (3.2.10+) ship their libpq inside a HIDDEN
# directory — psycopg_binary/.dylibs/ — and the extension modules reference
# it as '@loader_path/.dylibs/libpq.5.dylib'. PyInstaller's binary analysis
# drops dot-directories, so that tree never reached the v0.28.0 bundle; at
# runtime dyld fell back through its search path to pgserver's OWN
# libpq.5.dylib (PostgreSQL 16, no _PQcancelBlocking, which psycopg links),
# and the engine child died at boot with ImportError("no pq wrapper
# available") — Windows and Linux were green only because their wheels use
# a non-hidden psycopg_binary.libs/ that PyInstaller follows via $ORIGIN.
#
# Collect the wheel's dylibs verbatim under psycopg_binary/.dylibs, exactly
# where @loader_path resolves them; libpq's own neighbours (libssl,
# libcrypto, libgssapi...) are referenced @loader_path-flat, so the whole
# directory must ride together.
if IS_MACOS:
    import psycopg  # psycopg_binary refuses to load before psycopg; find it beside it

    _pb_root = os.path.join(
        os.path.dirname(os.path.dirname(psycopg.__file__)), "psycopg_binary"
    )
    _pb_parent = os.path.dirname(_pb_root)  # site-packages: the dest base
    _pb_dylibs = os.path.join(_pb_root, ".dylibs")
    if os.path.isdir(_pb_dylibs):
        for _root, _dirs, _files in os.walk(_pb_dylibs):
            for _file in _files:
                _src = os.path.join(_root, _file)
                # dest is relative to site-packages, so the tree lands at
                # psycopg_binary/.dylibs/ inside the bundle — exactly where
                # the extension's '@loader_path/.dylibs/...' resolves
                _rel = os.path.relpath(_src, _pb_parent)
                pg_binaries.append((_src, os.path.dirname(_rel).replace(os.sep, "/")))
    else:
        print(
            "WARNING: psycopg_binary has no .dylibs/ directory — if the wheel "
            "layout changed, verify the frozen engine still boots (ADR-031)."
        )

datas = [
    # the SQL bootstrap + the Alembic tree, at the package-relative fallback
    (os.path.join(ROOT, "db", "schema.sql"), "ledgerforge/sql"),
    (os.path.join(ROOT, "db", "functions.sql"), "ledgerforge/sql"),
    (os.path.join(ROOT, "db", "migrations"), "ledgerforge/sql/migrations"),
    # the application icon(s): the window finds them under _MEIPASS at runtime
    (os.path.join(SPECPATH, "ledgerforge.ico"), "desktop"),
    # dist metadata: entry_points(group="ledgerforge.plugins") at runtime
    *copy_metadata("ledgerforge"),
] + pg_datas

if IS_MACOS:
    datas.append((os.path.join(SPECPATH, "ledgerforge.icns"), "desktop"))

hiddenimports = [
    # entry-point plugins are imported by name at runtime — invisible to trace
    "ledgerforge.contrib.banking",
    "ledgerforge.contrib.bills",
    "ledgerforge.contrib.gst",
    "ledgerforge.contrib.invoicing",
    "ledgerforge.contrib.inventory",
    "ledgerforge.contrib.orders",
    "ledgerforge.contrib.payroll",
    "ledgerforge.contrib.statutory",
    "ledgerforge.contrib.example_slot",
    # uvicorn selects protocol implementations dynamically
    # (import_from_string — invisible to PyInstaller's trace). The engine
    # child pins loop=asyncio, http auto-resolves httptools->h11, ws auto —
    # "auto" shims resolve these by name, so all of them ride along.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools.impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    # the engine child process (spawn) — the target PyInstaller cannot trace
    "ledgerforge.nativeapp.app",
] + pg_hiddenimports

# Generated from ledgerforge.__version__ by scripts/make_version_info.py —
# the .exe properties track the package, never hand-edited (Windows only).
VERSION_FILE = os.path.join(SPECPATH, "version_info.txt")
ICON_FILE = os.path.join(SPECPATH, "ledgerforge.ico")
ICNS_FILE = os.path.join(SPECPATH, "ledgerforge.icns")

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
    version=VERSION_FILE if IS_WINDOWS and os.path.exists(VERSION_FILE) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LedgerForge",
)

if IS_MACOS:
    # the .app bundle: what macOS users double-click. The Info.plist carries
    # the honest metadata; the icns comes from the same glyph as every
    # other platform's icon.
    bundle = BUNDLE(
        coll,
        name="LedgerForge.app",
        icon=ICNS_FILE if os.path.exists(ICNS_FILE) else None,
        bundle_identifier="org.ledgerforge.LedgerForge",
        info_plist={
            "CFBundleName": "LedgerForge",
            "CFBundleDisplayName": "LedgerForge",
            "CFBundleShortVersionString": os.environ.get("LF_BUNDLE_VERSION", "0.0.0"),
            "CFBundleVersion": os.environ.get("LF_BUNDLE_VERSION", "0.0.0"),
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.finance",
        },
    )
