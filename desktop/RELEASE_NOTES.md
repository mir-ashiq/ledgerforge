# LedgerForge 0.19.0 — the completeness pass

**Phase 1 of the [v1.0 roadmap](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md):
nothing new — everything finished.** Every gap the v0.18 audit found in our
own house is closed: no API surface without a UI, no documented event
unfired, no stale docs.

## What's new in the window

- **Voucher entry speaks FX (and Tally's F-keys).** Every line can carry the
  FX trio — currency, FX amount, rate — and the base amount derives exactly
  as the engine's database check computes it (`round(amount_fx × fx_rate, 8)`),
  sub-cent precision intact. An incomplete trio or a mistyped base amount is
  named on the spot and blocks posting. **F4–F9** switch the voucher type —
  Contra, Payment, Receipt, Journal, Sales, Purchase — the same map as the
  web keymap.
- **Everything else you already loved — day book, masters, reports (including
  the GSTR-1 pack), periods, sandbox — unchanged and smoke-proven.**

## What's new in the web UI

- **The Books screen (`Alt+O`)** — TallyPrime's "select company": create a
  book (seeded with the full chart of accounts in one action), rename,
  archive, and switch. The choice is remembered; the open company is named
  in the entry bar.
- **Reversal from the day book (`Ctrl+R`)** — Law 2's one correction path,
  with a two-key confirm that states exactly what will happen: a mirrored
  voucher posts, the pair nets to exactly zero, the original is never
  edited. A reversal row explains why it cannot itself be reversed.
- **The periods screen (`Alt+E`)** — lock, unlock, and the close preview
  showing every zeroing line the close would post, before the confirmed
  close sweeps the P&L into Profit & Loss A/c.

## What's new underneath

- **`master.created` fires** — the plugin event the guide has promised since
  the first release: groups and ledgers announce their creation to packs
  after commit (webhook pack authors, this one's for you).
- **`cryptography` is a direct dependency** — the trust gate always imported
  it; now the manifest says so.

## The plan, on the record

Eleven phases to v1.0 — **"everything an Indian SMB's daily accounting
needs, on LedgerForge, without wishing for TallyPrime"** — now live as
[ROADMAP.md](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md),
one GitHub milestone per phase, one tracking issue each. Phase 2 is
**bill-wise & outstanding management** (bill references, ageing, receivables
and payables, interest on overdue bills).

## What's underneath (unchanged, and that's the point)

- Embedded PostgreSQL, append-only history, exact decimals — every law
  enforced at the application and database layers. The native window is an
  API client: zero business logic in the GUI, one engine for web, Docker
  and desktop.
- Your data stays where it was: `%LOCALAPPDATA%\LedgerForge` (or your
  `--data-dir`). v0.18 books open in v0.19 unchanged — the same migrate
  path the smoke test proves on every release.
- The engine runs in its own process (ADR-022): the window cannot crash
  your books, and the books cannot crash the window.
