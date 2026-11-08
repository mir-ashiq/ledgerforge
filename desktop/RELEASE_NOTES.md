# LedgerForge 0.26.0 — payroll depth

**Phase 8 of the [v1.0 roadmap](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md):
the people, given their paperwork.** The attendance register feeds
per-day pay heads, loans recover themselves from their schedules, the
relieving letter's numbers are one voucher — and the payslip batch leaves
the building without the kernel ever opening a socket (ADR-029, extends
ADR-012).

## What's new in the window

- **The Payroll page** (native, `Alt+7`): the people workbench — key the
  period's attendance register (a grid, previewed before it lands), issue
  staff loans (the confirmation quotes the engine's own EMI schedule),
  settle a relieving employee's dues (encashment, earnings, recoveries,
  one voucher), and walk every payroll document the book holds with
  per-employee payslip PDFs one key away.
- **Attendance registers as facts**: `POST /api/v1/payroll/attendance`
  keys present / absent / half-day / leave-with-pay per employee per
  period (a half-day is 0.5; the calendar is the honesty ceiling).
  Re-entry of the same register is a no-op; a corrected register is a new
  snapshot — latest-batch-wins, append-only history, the GSTR-2B
  discipline restated for the shop floor.
- **Attendance-derived pay**: an earning component may carry `per_day`
  instead of `amount` — the run derives `per_day × payable days`
  (present + half × 0.5 + leave-with-pay) from the live register and
  records the derivation beside the amount. No register, no derivation:
  a 422, never a guess.
- **Loans & advances with EMI recovery**: `mode: "loan"` issues the
  advance against the bank with the exact-Decimal annuity schedule
  computed into the document (rate 0 = interest-free; the final row
  absorbs its own rounding so principals sum exactly). The schedule is
  advisory; recovery is the run's new `recovery` component — it reduces
  net pay, credits the advance ledger, and can never cross the
  outstanding fold. A recovery naming its loan adopts the scheduled EMI
  for the period automatically.
- **The full & final settlement**: `mode: "settlement"` books leave
  encashment (days × rate — the operator's facts), extra dues,
  withholdings and loan recoveries in one voucher, paying direct from
  the bank or accruing to the payable. After it the employee is
  **relieved**: runs, loans and further settlements refuse; payouts stay
  legal — the dues are the whole point. A reversal reopens the
  relationship.
- **The payslip outbox**: every run queues one envelope per employee to
  `LF_PAYROLL_OUTBOX`, and `lf-payslip-bridge` renders the PDF batch
  through the same layout engine as Ctrl+P — and, only when the operator
  configures SMTP (`LF_PAYROLL_SMTP_*`), emails each payslip to the
  address the run entry carried. **Core never sends mail**; the bridge is
  the only mailer, and a blank address is never guessed.

## What's new underneath

- Migration `0011_payroll_depth` — the fifth core-schema change since
  0006, the ADR-026 argument restated for the shop floor:
  `payroll_attendance_batches` + `payroll_attendance_lines`, INSERT-only,
  append-only, idempotent by revision-carrying fingerprint. Old books
  upgrade in place (the smoke test proves the migrate path on every
  build).
- Plugin API **0.14**: `Resolver.attendance_lines(period)` — the payroll
  pack's per-day fold input as a versioned, session-free read (the
  `fy_start_month` precedent). The kernel stays meaning-blind; the pack
  computes payable days.
- Loans and settlements are documents on the same `meta.payroll` grammar
  as runs and payouts — the same lock discipline, the same reversal
  netting, the same books-are-strangers isolation. The loan and
  settlement folds reuse the run fold's machinery: a reversed loan
  restores the outstanding exactly as a reversed run restores YTD.
- Two pack reports join the register: `payroll-attendance` (the live
  register with payable days — JSON for the UI, CSV, PDF) and
  `payroll-loans` (per-employee positions and per-loan schedules), both
  from the same folds the guards run.

## The plan, on the record

The capability ledger's Payroll row flips to **parity** — landed in
v0.26.0 (Phase 8). Next: **Phase 9 — data exchange & the Tally migration
path** (CSV/Excel import with previewable, idempotent transactions, the
TallyPrime XML import with a verification report, SQL views + personal
API tokens — ADR-030).

---

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

