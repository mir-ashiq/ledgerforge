# LedgerForge 0.25.0 — GST compliance depth

**Phase 7 of the [v1.0 roadmap](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md):
the tax month, closed end to end.** The books and the portal finally sit
at the same table: the GSTR-2A/2B download imports as facts, the
reconciliation prices every matched, mismatched and missing document —
and the 3B, the 9 and the e-way-bill outbox join the GSTR-1 register on
the same folds (ADR-028, extends ADR-017).

## What's new in the window

- **The GST page** (native, `Alt+6`) and the **GST screen** on the web
  (`Alt+T`): the compliance workbench — import the portal's 2A/2B
  download (paste the adapter's JSON, preview first), walk the
  reconciliation table with every state and reason named, read the ITC
  both sides claim, and take the statutory PDFs (GSTR-1, 3B, 9, the
  comparison) home. The Reports page gains the GSTR-3B summary, the
  GSTR-9 scaffolding and the reconciliation with PDF export.
- **The GSTR-2A/2B reconciliation**: the books' inward register (keyed on
  the supplier's GSTIN and their own invoice number) against the imported
  portal statement — matched / mismatched / missing-in-books /
  missing-in-portal, magnitudes priced, every disagreement's reason
  named, the ITC difference exact to the cent. Re-downloads are no-ops
  when unchanged and new snapshots when the portal moved.
- **GSTR-3B, the monthly summary's own statement**: outward intra/inter
  and the eligible ITC netted of returns, the net payable — the ledger's
  numbers or no numbers, with the not-captured heads (reverse charge,
  exempt) named as blanks, never guessed.
- **GSTR-9 scaffolding**: the year in twelve monthly folds plus the
  annual totals (Table 4's outward, Table 6's ITC) — the portal-only
  tables honestly left for the portal's own records.
- **Credit & debit notes** are first-class document modes: a credit note
  walks the sale backwards (same ledgers, opposite side) and nets every
  outward figure; a debit note reverses the input credit. The GSTR-1
  register names every document INV/CN/DN.
- **Multi-GSTIN books**: a voucher's `supplier_gstin` segments every
  statement; `?gstin=` scopes one registration of a company registered
  in several states.
- **The e-way-bill outbox** (the IRN pattern applied to movement): every
  threshold-crossing document (₹50,000 by default, `LF_GST_EWB_THRESHOLD`
  to move the line) queues a movement envelope after commit — with the
  Part-B facts you captured at dispatch — and `lf-ewb-bridge` ships it to
  your adapter: 2xx records the EWB number, 4xx parks, 5xx retries.

## What's new underneath

- Migration `0010_gst_2b` — the fourth core-schema change since 0006, by
  the ADR-026 argument restated for the tax portal: `gst_downloads` +
  `gst_download_lines`, INSERT-only, append-only, idempotent by
  fingerprint. Old books upgrade in place (the smoke test proves the
  migrate path on every build).
- Plugin API **0.13**: `Resolver.gst_portal_lines(kind, period, gstin=)` —
  the pack's fold input as a versioned, session-free read (the
  `bank_lines` precedent). The kernel stays comparison-blind; the pack
  decides what a portal row means.
- The GST pack's statements are folds of the same registers GSTR-1
  already used — a 3B that disagrees with the register is a bug, not a
  rounding story. Credit/debit notes ride the same tax ledgers opposite
  side; reversals still net.
- The kernel seam (`gst_compliance.py`, `POST /api/v1/gst/portal/import`,
  the receipts and lines feeds, the `gst.download.imported` event) never
  talks to the GST portal itself — the operator's adapter translates, D3.

## The plan, on the record

The capability ledger's GST row flips to **parity** — landed in v0.25.0
(Phase 7). Next: **Phase 8 — payroll depth** (attendance & leave
documents, loans & advances with EMI recovery, full & final settlement,
the payslip distribution seam — ADR-029, extends ADR-012).

---

# LedgerForge 0.24.0 — inventory depth

**Phase 6 of the [v1.0 roadmap](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md):
the stockroom, given a memory.** Batches remember their expiry, serial
numbers remember their shelf, godowns remember what they hold — and the
manufacturing journal remembers exactly what it cost to make. The
comparison table's biggest gap closes at parity's depth, with valuation
staying the moving weighted average ADR-011 promised.

## What's new in the window

- **The Inventory page** (native, `Alt+7`) and the **stock screen** on
  the web (`Alt+V`): stock-item masters with their tracking modes and
  re-order levels, the positions browser with the godown and batch
  split, the BOM recipes — and the expiry ageing, re-order shortfall,
  godown-wise and batch-wise reports one key away (`1`–`5`).
- **Batches & expiry with FEFO**: every stock line may carry a batch and
  its expiry (the first receipt fixes it); issues name their batch or
  take FEFO — earliest expiry first, ties by batch name, the allocation
  recorded in the document's `computed`. The expiry-ageing report
  buckets every dated batch: expired, ≤30, ≤60, ≤90, >90 days.
- **Serial numbers are identity**: one per unit; receiving a duplicate
  is a data error; issuing a serial that sits at another godown or in
  another batch is refused — a serial is one physical thing.
- **Godowns are a first-class dimension** — a location key on movements
  and positions, not another ledger: `(item, godown) → qty` in the fold,
  and an issue cannot take what the location does not hold (the ADR-011
  limitation, closed).
- **Re-order levels and the shortfall report**; **price lists**
  (per-party, date-effective) whose effective rate is annotated on
  sales — suggestions never commit; the selling rate stays yours.
- **The manufacturing journal**: consumes components at their moving
  averages, adds your charge legs (wages, power, freight — anything
  outside Stock-in-Hand), and produces the output at the rolled-up cost
  — `output value = Σ components + Σ charges`, one exact amount. A named
  BOM validates the recipe; without one, the explicit journal stands
  alone. Every stock document prints as the physical document it is.

## What's new underneath

- **Migration `0009_stock_depth` (ADR-027)** — the third core-schema
  change since the scale-out, by the ADR-025 argument restated for
  master data: stock items, price lists and BOMs are masters that need
  database identity (UNIQUE names per book, FK-validated components,
  one rate per item/party/date slot). The **movements' depth stays in
  the document fold** — ADR-011's extension, not its replacement: no
  document rewrite, old books upgrade in place.
- **Plugin API 0.12**: four stock-master reads on the `Resolver` —
  `stock_item(name)`, `stock_items()`, `stock_bom(name)` (frozen
  `StockItemMaster`/`StockBomMaster` snapshots) and
  `stock_price(item, party, on)`. The ADR-011 floor keeps every older
  pack loading.
- **The tolerant replay**: positions fold ALL items together now (a
  manufacture needs its components' states to cost its output), and a
  replay of reversal-orphaned history applies the overdraw instead of
  refusing the whole book — the position reports the orphan, the
  posting guards stop new ones.
- **Five pack reports over one fold** — `stock-position`,
  `stock-godown`, `stock-batch`, `stock-expiry`, `stock-shortfall` —
  each as PDF (the shared print-bridge layout), CSV and JSON: one
  truth, every surface.
- 11 kernel + 17 pack + 8 native + 4 web tests pin the depth: the
  grammar's guards, FEFO's determinism, the manufacturing cost rollup's
  exactness, reversal netting across every sub-ledger, and book
  isolation. Migration 0009 is drift-guarded and independently
  reversible; docs refreshed (README, ROADMAP, plugin guide, keyboard
  guide, this file).

---

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
