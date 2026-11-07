# LedgerForge Desktop — the zero-config Windows application

**One folder, one `LedgerForge.exe`, your books on your own machine.**

Download `LedgerForge-<version>-win64.zip`, extract, double-click
`LedgerForge.exe`. The app boots its own embedded PostgreSQL and the
keyboard-first web UI on `127.0.0.1` (loopback only — nothing is exposed to
the network) and opens your browser. Close the console window to stop;
your data lives in `%LOCALAPPDATA%\LedgerForge`.

Read `QUICKSTART.txt` inside the zip for the first voucher in thirty
seconds, command-line flags, backups, and moving machines.

## What's inside

- The full v0.17 engine: append-only, exact-decimal double-entry
  bookkeeping with the three financial laws enforced at the application
  **and** database layers — no feature is desktop-downgraded.
- Embedded PostgreSQL (the pgserver portable distribution): partitions,
  deferrable constraint triggers, `NUMERIC(26,8)` — Law-grade semantics
  with nothing to install.
- The built web UI served same-origin; the complete GST / payroll /
  statutory plugin packs; the signed community plugin index and drop-in
  trust gate (`%LOCALAPPDATA%\LedgerForge\plugins`).
- `--smoke` self-check: the shipped binary proves it boots (embedded
  database → schema → `/health` → web UI) before this release was
  published.

## Verify your download

`checksums.txt` in this release carries the SHA-256 of the zip:

```powershell
Get-FileHash LedgerForge-<version>-win64.zip -Algorithm SHA256
```

The binary is not code-signed; Windows SmartScreen will ask on first run
("More info" → "Run anyway") — see ADR-021 for the signing roadmap.

## Source code

LedgerForge is AGPL-3.0. The complete source for this build is this
repository at this tag; `desktop/README.md` documents how the artifact is
produced and self-tested.
