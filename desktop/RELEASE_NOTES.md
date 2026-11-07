# LedgerForge 0.18.0 — the native Windows application

**A real program now: double-click, a window opens. No browser.**

v0.17 booted the engine and pointed a browser tab at it. You asked for a
full Windows application — this is it. `LedgerForge.exe` opens a native
window (Qt) with its own menus, tables and file dialogs; the engine —
embedded PostgreSQL and the whole LedgerForge core — runs invisibly in a
child process. No URL, no port to remember, no console window, no website.

## What's in the window

- **Day Book** — your vouchers with date/type filters and search;
  double-click any voucher to view it and save its PDF.
- **Voucher entry (Ctrl+N)** — keyboard-first: autocomplete ledger names,
  Debit/Credit lines with a live balance gate (the button unlocks only
  when debits equal credits to the cent), narration, idempotent posting.
- **Masters** — the chart of accounts: ledgers under their groups, create
  new ledgers.
- **Reports** — Trial Balance, Profit & Loss, Balance Sheet, FY-close
  preview, and the **GSTR-1 pack**: save the statutory PDF or export CSV,
  through real Windows file dialogs.
- **Periods** — financial years: lock, unlock, close preview, close.
- **Sandbox** — the plugin dashboards your installed plugins register.

Keyboard: Ctrl+N new voucher · F5 refresh · Enter open · Alt+1..5 pages ·
Ctrl+Q quit.

## What's underneath (unchanged, and that's the point)

- The proven ADR-021 engine: embedded PostgreSQL, append-only history,
  exact decimals, every law enforced at the application and database
  layers. The native window is an API client — zero business logic in the
  GUI, one engine for web, Docker and desktop.
- Your data stays where it was: `%LOCALAPPDATA%\LedgerForge` (or your
  `--data-dir`). v0.17 books open in v0.18 unchanged — the same migrate
  path the smoke test proves on every release.
- The engine runs in its own process (ADR-022): the window cannot crash
  your books, and the books cannot crash the window.

## Proven, not assumed

Every release runs `LedgerForge.exe --smoke` on the build machine before
publishing: the shipped binary boots its embedded database, provisions or
migrates the schema, constructs the native window (offscreen), and loads
the day book, a trial balance and the GSTR-1 pack through the same client
the window uses. Exit 0 or no release.

## Verify your download

`checksums.txt` in this release carries the zip's SHA-256:

```powershell
Get-FileHash LedgerForge-0.18.0-win64.zip -Algorithm SHA256
```

SmartScreen may warn — the binary is unsigned; "More info" → "Run anyway",
after checking the hash.
