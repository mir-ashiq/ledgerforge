# LedgerForge 0.22.0 — cost centres & cost categories

**Phase 4 of the [v1.0 roadmap](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md):
the break-up, closed.** Where did this money go, and along which axis? A
rent line splits across branches; a salary line across departments; the
break-up report rolls those splits up trees. TallyPrime users have lived in
cost centres for thirty years — LedgerForge now runs them as **the first
core-database change since the scale-out revision** (migration 0007),
because allocations must aggregate with the same rigour as the entries they
split.

## What's new in the window

- **Cost centres… (Ctrl+K) on the voucher form, Alt+K on the web.** Every
  complete line with its allocated-so-far and its remainder — *always
  visible* — and a split editor for the picked line: one row per centre,
  exact arithmetic. A line is **fully allocated or left unallocated**; a
  partial split cannot be armed, and the engine re-proves every split at
  posting time. Create a centre inline (name, parent, category) without
  leaving the entry flow.
- **Masters grows two tabs.** *Cost Centres* (the allocation tree — every
  centre answers to exactly one category) and *Cost Categories* (the
  classification axes: Department, Location, Project…), with create
  dialogs and the same cycle/root protection as the chart of accounts.
- **Three new reports:** *Cost Centre Summary* (each centre carries its
  whole subtree; the **Unallocated** mass is always shown — allocated +
  unallocated is the window's whole entry mass), *Cost Centre Break-up*
  (one centre, ledger by ledger, total proven against the rows), and
  *Cost Category Summary* (a primary category carries its secondaries).
  Tree-nested, CSV-exportable, FYC sweeps excluded.

## What's new underneath

- **The allocation store is core schema** (ADR-025, migration 0007):
  `cost_centres` and `cost_categories` as adjacency+closure trees (the
  chart-of-accounts machinery), and `cost_allocations` — one row per
  (voucher entry, centre, amount), Dr/Cr preserved from the entry, exact
  decimals, append-only like the entries they split.
- **The conservation law, database-enforced like Law 1.** An entry's
  allocations sum to its amount *exactly* — a deferrable constraint
  trigger proves it at commit, the same discipline that proves debits
  equal credits. Nothing partial ever survives a refused posting.
- **Both shapes of the entry table.** The scale-out partition conversion
  (ADR-018) and the new allocation→entry foreign key cooperate: the FK
  rides the plain shape, the insert-time trigger keeps every guarantee on
  the partitioned shape, and unpartitioning restores the FK.
- **Reversals mirror the splits** — same centres, same amounts, the
  flipped side riding the flipped line — so a reversed voucher walks every
  centre's net back to exactly zero. Plugin API 0.10: packs that split
  lines now see and must carry `DraftLine.cost_allocations`.

## The plan, on the record

Eleven phases to v1.0 — **"everything an Indian SMB's daily accounting
needs, on LedgerForge, without wishing for TallyPrime"** — tracked as
[ROADMAP.md](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md)
with one GitHub milestone per phase. Phase 1 (the completeness pass) landed
in v0.19.0, Phase 2 (bill-wise & outstanding) in v0.20.0, Phase 3 (order
processing) in v0.21.0, Phase 4 (cost centres & categories) in this
release; Phase 5 is **the banking pack** (bank reconciliation, cheque
management).

## What's underneath (unchanged, and that's the point)

- Embedded PostgreSQL, append-only history, exact decimals — every law
  enforced at the application and database layers. The native window is an
  API client: zero business logic in the GUI, one engine for web, Docker
  and desktop.
- Your data stays where it was: `%LOCALAPPDATA%\LedgerForge` (or your
  `--data-dir`). v0.21 books open in v0.22 after a one-time automatic
  migration (0007 — new tables only; the migrate path the smoke test
  proves on every build).
- The engine runs in its own process (ADR-022): the window cannot crash
  your books, and the books cannot crash the window.
