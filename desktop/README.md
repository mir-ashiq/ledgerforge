# LedgerForge Desktop — packaging (ADR-021)

The desktop edition turns LedgerForge into a zero-config Windows
application: one folder, one `LedgerForge.exe`, an embedded PostgreSQL and
the web UI on `127.0.0.1`. The launcher logic lives in
`src/ledgerforge/desktop.py` (runs from source on Linux/macOS too); this
directory is the PyInstaller packaging around it.

## What the user gets

`LedgerForge-<version>-win64.zip` → extract → `LedgerForge/LedgerForge.exe`:

- **Embedded PostgreSQL** — the pgserver portable distribution ships inside
  the bundle; the first run `initdb`s a cluster into
  `%LOCALAPPDATA%\LedgerForge\pgdata` and every later run adopts it. No
  database to install, no service, loopback only.
- **The engine and the web UI, same-origin** — the built frontend rides
  along and is served at `/`; the browser window talks to the API without
  CORS, proxies or a second port.
- **A console window** — this is a server app at heart: the window shows
  the logs and stays the app's life support (Ctrl+C stops everything).

## Release pipeline

`.github/workflows/release-windows.yml` fires on every `v*` tag:

1. `windows-latest` runner, Python 3.12 + Node 20;
2. `npm ci && npm run build` (the web UI);
3. `scripts/make_version_info.py` (the .exe version resource, generated
   from `ledgerforge.__version__` so it can never drift);
4. `pyinstaller desktop/ledgerforge.spec --noconfirm`;
5. **`LedgerForge.exe --smoke`** — the shipped binary boots its embedded
   database, provisions the schema, and must answer `/health` and serve
   `/`, or the release fails (the artifact is proven, not assumed);
6. zip + SHA-256 checksums → a GitHub Release with the tag's notes.

## Building locally (Windows)

```powershell
git clone https://github.com/mir-ashiq/ledgerforge
cd ledgerforge
py -3.12 -m venv .venv && .venv\Scripts\activate
pip install -e ".[desktop,dev]" pyinstaller
npm --prefix frontend/web ci
npm --prefix frontend/web run build
python scripts\make_version_info.py > desktop\version_info.txt
pyinstaller desktop\ledgerforge.spec --noconfirm
dist\LedgerForge\LedgerForge.exe --smoke --data-dir $env:TEMP\lf-smoke
```

## Known limitations (v0.17)

- **Unsigned binary** — no code-signing certificate; Windows SmartScreen
  will warn on first run. The release publishes SHA-256 checksums; signing
  is future work (see ADR-021).
- **Windows x64 only** for the shipped artifact — the launcher itself is
  cross-platform (pgserver ships Linux/macOS binaries too); packaging those
  is future work.
- **No auto-update** — a new release is a new zip; the data directory
  survives across versions (migrations run on boot).
