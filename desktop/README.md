# LedgerForge Native — packaging (ADR-021 engine stack, ADR-022 shell)

The native application turns LedgerForge into a real Windows program: one
folder, one `LedgerForge.exe`, a Qt window over an embedded engine. The
window and its client live in `src/ledgerforge/nativeapp/` (runs from
source on Linux/macOS too: `ledgerforge-native`); this directory is the
PyInstaller packaging around it. `src/ledgerforge/desktop.py` (ADR-021)
still powers the engine stack both editions share — `ledgerforge-desktop`
remains available for the browser-based local edition.

## What the user gets

`LedgerForge-<version>-win64.zip` → extract → `LedgerForge/LedgerForge.exe`:

- **A native window** — GUI subsystem (no console), own menu bar, tables,
  file dialogs, taskbar icon. No browser is opened; no web UI ships.
- **The engine in a child process** — embedded PostgreSQL (pgserver) and
  the FastAPI core in a `multiprocessing` spawn child (ADR-022): process
  isolation between Qt and libpq, graceful stop through a watched event,
  WAL recovery if the parent dies hard.
- **The same data directory** — `%LOCALAPPDATA%\LedgerForge` (or
  `--data-dir`), forward- and backward-compatible with v0.17 books.

## Release pipeline

`.github/workflows/release-windows.yml` fires on every `v*` tag:

1. `windows-latest` runner, Python 3.12, `pip install -e ".[gui,dev]"`
   (engine + PySide6 + test kit) + PyInstaller — no Node build, no web UI;
2. `scripts/make_version_info.py` (the .exe version resource, generated
   from `ledgerforge.__version__` so it can never drift);
3. `pyinstaller desktop/ledgerforge.spec --noconfirm`;
4. **`LedgerForge.exe --smoke`** under `QT_QPA_PLATFORM=offscreen`: the
   shipped binary boots its embedded database (the default
   `%LOCALAPPDATA%` path — RUNNER_TEMP's ACLs break initdb), provisions or
   migrates the schema, constructs the native window, and loads the day
   book, a trial balance and the GSTR-1 pack. Exit 0 or the release fails
   (the artifact is proven, not assumed). A GUI-subsystem exe needs
   `Start-Process -Wait` — PowerShell does not wait on those;
5. zip + SHA-256 checksums → a GitHub Release with the tag's notes.

## Building locally (Windows)

```powershell
git clone https://github.com/mir-ashiq/ledgerforge
cd ledgerforge
pip install -e ".[gui,dev]" pyinstaller
python scripts\make_version_info.py | Out-File -Encoding ascii desktop\version_info.txt
pyinstaller desktop\ledgerforge.spec --noconfirm
$env:QT_QPA_PLATFORM = "offscreen"; .\dist\LedgerForge\LedgerForge.exe --smoke
```

## The icon

`ledgerforge.ico` is generated (`scripts/make_icon.py`, Pillow — the
brass-ledger glyph) at 16–256 px; the spec bundles it and the Qt window
resolves it under `sys._MEIPASS` in the frozen bundle.
