# LedgerForge 0.20.0 — bill-wise & outstanding management

**Phase 2 of the [v1.0 roadmap](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md):
the accounting-core gap, closed.** TallyPrime's party ledgers carry bill-wise
details — every invoice opens a named bill, every receipt settles specific
bills, and the receivables/payables position is bill-level. LedgerForge now
does the same, as the fourth plugin pack, with zero database schema change.

## What's new in the window

- **Allocate bills while you enter (`Ctrl+L`).** Add a line on a party ledger
  (Sundry Debtors or Sundry Creditors) with its amount, then *Allocate
  bills…*: the party's open bills appear with their outstanding and ageing,
  you type an amount against each, and the remainder is offered **on
  account** or as a **new ref** — the TallyPrime flow. The allocations ride
  the voucher; the engine re-checks every one of them (a refusal keeps your
  draft intact).
- **New reports in the Reports page:** *Bills Receivable* and *Bills Payable*
  with ageing buckets (not due / 0–30 / 31–60 / 61–90 / 90+ by due date) and
  the on-account pools; the **Party Ledger Statement** — every entry with its
  voucher's bill allocations alongside, opening balance to closing; and
  **Interest on Overdue Bills** — the exact computation, per bill, before you
  post a thing.
- **Interest posts as a voucher, never a side effect.** The `interest_charge`
  document mode derives Dr party / Cr Interest Income from the overdue fold,
  opens a bill for its total (the charge must appear in what the party owes),
  and lands in the day book like any other voucher — reviewable, reversible.

## What's new in the web UI

- **The allocation dialog (`Alt+L`)** on the voucher screen — the same
  settle/remainder decision at entry time, keyboard-first.
- **The Outstanding screen (`Alt+I`)** — receivable or payable side, ageing
  columns, party filter, the bucket totals the engine computed.

## What's new underneath

- **The conservation law** (ADR-023): for every voucher carrying bill
  allocations, Σ ref dr − Σ ref cr must equal the party's net movement —
  exactly. From that, inductively: **Σ open bills + on-account equals every
  party's ledger balance**, at every point in the book's history. The tests
  assert it against the real trial balance.
- **Plugin API 0.8** (additive — every existing pack keeps loading):
  `Resolver.ledger_entries()` gives packs the engine-side rows of a ledger
  statement, and pack reports can answer **JSON** (`?format=json`) alongside
  PDF and CSV — one fold feeding every surface, including the dialogs above.
- Rate resolution for interest: the bill's own declaration → the party's
  `default_interest` → the request's explicit rate → none (a blank is
  honest, an invented rate is not).

## The plan, on the record

Eleven phases to v1.0 — **"everything an Indian SMB's daily accounting
needs, on LedgerForge, without wishing for TallyPrime"** — tracked as
[ROADMAP.md](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md)
with one GitHub milestone per phase. Phase 1 (the completeness pass) landed
in v0.19.0, Phase 2 (bill-wise & outstanding) in this release; Phase 3 is
**order processing** — the quote-to-cash and procure-to-pay chains.

## What's underneath (unchanged, and that's the point)

- Embedded PostgreSQL, append-only history, exact decimals — every law
  enforced at the application and database layers. The native window is an
  API client: zero business logic in the GUI, one engine for web, Docker
  and desktop.
- Your data stays where it was: `%LOCALAPPDATA%\LedgerForge` (or your
  `--data-dir`). v0.19 books open in v0.20 unchanged — zero schema change
  this release; the same migrate path the smoke test proves on every build.
- The engine runs in its own process (ADR-022): the window cannot crash
  your books, and the books cannot crash the window.
