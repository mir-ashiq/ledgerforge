# LedgerForge 0.23.0 — the banking pack

**Phase 5 of the [v1.0 roadmap](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md):
the bank statement, reconciled.** The books say one thing, the bank says
another — the reconciliation statement is where the two agree, and where
every disagreement is priced, exactly. TallyPrime's banking pack has been
the comparison table's honest gap since v0.18; this release closes it with
the same rigour the kernel brings to everything else.

## What's new in the window

- **The Banking page** (native) and the **banking screen** on the web
  (`Alt+G`): import a statement with a preview, walk the line book, match
  a line to the matchmaker's suggestion with one move (`M`), withdraw a
  judgement (`U`), read the proof, open the cheque register.
- **Statement import (CSV + MT940)**: validated, all-or-nothing, with a
  dry-run preview. Re-importing the same statement is a clean no-op (the
  content fingerprint is a database `UNIQUE`, not a check-then-insert
  race); a later statement's genuine repeat of an identical transaction
  lands beside the first. MT940's own opening/closing balances become the
  proof's foldable anchor.
- **Suggestions never commit.** The matchmaker is a pure function —
  amount-exact, date-windowed, reference-fuzzy (cheque number, voucher
  number, narration overlap), ranked. Only the explicit match writes; a
  manual match may even disagree on amount, and the statement prices the
  difference as its own row instead of hiding it.
- **The reconciliation statement is a proof**: book − statement =
  unmatched-book − unmatched-statement + pair differences (with the
  statement's own opening as a baseline row), exact decimals, asserted on
  every render — the accountant's anchor is the figure on the statement,
  or the fold from the imported opening when you leave it blank.

## What's new underneath

- **Migration 0008 (ADR-026)** — the second core-schema change since the
  scale-out, deliberately: statement facts need database identity, and the
  proof aggregates them exactly. `bank_statement_batches`/`_lines` are
  append-only (the Law-2 trigger family); `bank_line_matches` is the
  kernel's **first deliberately mutable table** — match/un-match is
  revisable judgement, not financial history. UPDATE-only, DELETE never,
  provenance columns always; the boundary is stated as policy.
- **The cheque register is a fold, not a status column** (the order
  philosophy): declarations ride vouchers (`meta.banking.cheque` — number,
  bank ledger, PDC maturity), validated to move their own bank account
  with per-ledger number uniqueness. *Issued* when posted, *presented*
  when an imported line references the number (precise digit-runs —
  448812 never matches 4488123), *cleared* when that line is matched.
- **The gateway seam (plugin API 0.11)**: `register_webhook(key, handler)`
  claims `POST /api/v1/hooks/{key}` — the banking pack's
  `bank-statement-gateway` accepts the generic gateway JSON, and a
  per-bank connected-banking adapter is a drop-in behind the ADR-016 trust
  gate that translates its bank's payload. The kernel never learns a
  bank's name (ADR-026 D3); bank credentials belong to the adapter or the
  proxy in front of the route, never to core. After-commit events
  (`bank.statement.imported`, `bank.line.matched`, `bank.line.unmatched`)
  fire for bridges, and `Resolver.bank_lines` is the sanctioned fold read.
- **What this release does not do, on the record**: no per-bank
  connected-banking adapters in tree (drop-ins only, trust-gated); no
  FX-account reconciliation (statements must be in the book's base
  currency — the `amount_fx` fold is the recorded follow-up); no N:1
  match groups; no auto-match-on-import (suggestions are computed on
  request, never applied — the roadmap's law, kept structural).

## The plan, on the record

Eleven phases to v1.0 — **"everything an Indian SMB's daily accounting
needs, on LedgerForge, without wishing for TallyPrime"** — tracked as
[ROADMAP.md](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md)
with one GitHub milestone per phase. Phase 1 (the completeness pass) landed
in v0.19.0, Phase 2 (bill-wise & outstanding) in v0.20.0, Phase 3 (order
processing) in v0.21.0, Phase 4 (cost centres & categories) in v0.22.0,
Phase 5 (the banking pack) in this release; Phase 6 is **inventory depth**
(batches, serials, expiry, godowns, re-order levels, manufacturing).

## What's underneath (unchanged, and that's the point)

- Embedded PostgreSQL, append-only history, exact decimals — every law
  enforced at the application and database layers. The native window is an
  API client: zero business logic in the GUI, one engine for web, Docker
  and desktop.
- Your data stays where it was: `%LOCALAPPDATA%\LedgerForge` (or your
  `--data-dir`). v0.22 books open in v0.23 after a one-time automatic
  migration (0008 — three new tables only; the migrate path the smoke test
  proves on every build).
- The engine runs in its own process (ADR-022): the window cannot crash
  your books, and the books cannot crash the window.

---

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
