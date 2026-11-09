# LedgerForge — The Road to v1.0

| | |
|---|---|
| **Living document** | This is the plan of record. Each phase lands as a minor release with its own ADR, tests and a Windows build; each phase has a GitHub milestone and a tracking issue. |
| **Baseline** | v0.18.0 (2026-11-07) — the native Windows application (ADR-022) · current: **v1.0.0** |
| **Destination** | **v1.0.0 — everything an Indian SMB's daily accounting needs, on LedgerForge, without wishing for TallyPrime.** |
| **Ordering** | Phases are strictly ordered by dependency, deliberately not dated. Scope below is a commitment to the *problem*, not a deadline for the code. |

The engineering rules of every phase are already settled and never renegotiated: the three
financial laws (double-entry, append-only, exact decimals) hold everywhere; plugin packs
ride `vouchers.meta` with **zero core schema change wherever possible** (the pattern proven
by inventory, payroll and GST); every phase ships with its ADR, its tests against a real
PostgreSQL, and the smoke-gated Windows build.

---

## 1. Where we stand (v0.18.0)

The kernel and its packs — the [original 17-item roadmap](docs/analysis.md#9-risks--roadmap)
is fully landed:

- **Engine**: exact-decimal append-only double-entry posting, gapless numbering, reversal,
  opening balances, period lock/FY close, multi-currency with FX snapshots, multi-book,
  OIDC auth, the balance cache and yearly entry partitions for scale-out reporting, cost
  centres & categories with their allocation store (ADR-025, v0.22), bank statement
  facts with the match annotation and the reconciliation proof (ADR-026, v0.23).
- **Packs**: invoicing, payroll (runs, YTD, payslips), statutory (PF/ESI/TDS), GST
  (place of supply, HSN rate tables, GSTR-1/HSN statements, the IRN outbox + bridge),
  bills (bill-wise, outstanding, ageing, interest — ADR-023, v0.20), orders
  (quotation/SO/DN/PO/GRN, position fold, pending reports — ADR-024, v0.21), banking
  (statements, reconciliation proof, cheques — ADR-026, v0.23), inventory (ADR-011's
  weighted-average fold, grown to depth in v0.24: batches/serials/expiry with FEFO,
  godowns, re-order + shortfall, price lists, the manufacturing journal — ADR-027),
  GST grown to compliance depth in v0.25: credit/debit notes netting every
  statement, multi-GSTIN scoping, GSTR-3B/9, the 2A/2B ITC reconciliation
  against imported portal downloads, the e-way-bill outbox + bridge — ADR-028).
- **Surfaces**: keyboard-first web UI (command palette, rebindable Tally keymap, sandboxed
  plugin slots), native Windows application (ADR-022: a Qt window over an embedded
  PostgreSQL — no browser; the Orders page and document pickers joined in v0.21, the
  cost split dialog and masters tabs in v0.22, the Banking page and web banking screen
  in v0.23, the Inventory page and web stock screen in v0.24, the GST page and
  web compliance screen in v0.25), CLI,
  REST API, dependency-free PDF printing.
- **Trust**: signed community plugin distribution with the curator index, Ed25519
  signatures, operator acknowledgement.
- **Ops**: CI on real PostgreSQL (469 backend tests + 131 web tests + Playwright E2E),
  backup/restore drills, production compose, deployment runbook.

## 2. The benchmark: TallyPrime today

TallyPrime is the reference product and the honest measuring stick. As of its 7.x releases
(2025–2026) it offers: full accounting with bill-wise details and outstanding management;
order processing (quotations, sales/purchase orders, delivery notes); cost centres and cost
categories; banking with connected banking, bank reconciliation and cheque management;
inventory with batches, serial numbers, expiry dates, godowns, re-order levels and
manufacturing; GST compliance including GSTR-2A/2B reconciliation aids, GSTR-3B/9
preparation, e-invoice and e-way bill flows; payroll with attendance; multi-company;
TallyIra (an AI assistant); Excel/CSV import-export and TDL extensibility. Pricing:
Silver ₹22,500 one-time (single user) / ₹750 month, Gold ₹67,500 / ₹2,250 month, plus TSS
subscriptions for updates and remote services.

### Capability ledger, v0.18.0 vs TallyPrime

| Capability | TallyPrime | LedgerForge v0.18 | Verdict |
|---|---|---|---|
| Double-entry core, vouchers, reversal | ✔ | ✔ (with DB-proven laws) | **LedgerForge leads** — append-only history and exact-decimal wire format are *enforced*, not conventional |
| Chart of accounts (28 groups, deep nesting) | ✔ | ✔ (same seed, closure-table rollups) | parity |
| Multi-company | ✔ | ✔ (ADR-009) | parity in the engine; **UI gap** — the web app cannot pick a book (Phase 1) |
| Period lock / FY close | ✔ | ✔ (ADR-008) | parity; web UI lacks the period screen (Phase 1) |
| Multi-currency | ✔ | ✔ (ADR-007, entry-level snapshots) | parity |
| Keyboard-first UX | ✔ (its identity) | ✔ (ADR-004, rebindable keymap, palette) | parity in the web app; the native window is narrower (Phase 1) |
| Invoicing | ✔ | ✔ (ADR-010) | parity for the invoice itself; bill-wise tracking landed in **v0.20** (ADR-023) |
| Inventory | ✔ batches/serial/expiry/godowns/re-order/mfg | ✔ (ADR-027: batches/serials/expiry with FEFO, godowns, re-order + shortfall, price lists, manufacturing) | **parity** — landed in v0.24.0 (Phase 6); valuation stays weighted-average by design |
| Orders (quote/SO/PO/GRN) | ✔ | ✔ (ADR-024) | **parity** — landed in v0.21.0 (Phase 3) |
| Cost centres | ✔ | ✔ (ADR-025) | **parity** — landed in v0.22.0 (Phase 4) |
| Banking, BRS, cheques | ✔ (incl. connected banking) | ✔ (ADR-026) | **parity** — landed in v0.23.0 (Phase 5); per-bank connected-banking APIs stay plugin-side by design (D3) |
| GST | ✔ (GSTR-1/2A/2B/3B/9, e-way, e-invoice) | GSTR-1, HSN summary, IRN outbox, 2A/2B recon, 3B, 9, notes, multi-GSTIN, e-way outbox (ADR-028) | **parity** — landed in v0.25.0 (Phase 7); reverse-charge and set-off stay honestly blank, portal adapters stay plugin-side (D3) |
| Payroll | ✔ (attendance, loans, F&F) | runs, YTD, payslips, PF/ESI/TDS, attendance-derived pay, loans with EMI recovery, F&F, payslip distribution (ADR-029) | **parity** — landed in v0.26.0 (Phase 8); a leave *balance* (accrual policy) stays the operator's fact, SMTP stays bridge-side by design (D3) |
| Bill-wise & outstanding, ageing, interest | ✔ | ✔ (ADR-023) | **parity** — landed in v0.20.0 (Phase 2) |
| Data exchange | ✔ Excel/CSV/TDL/XML | CSV/XLSX import (masters, openings, vouchers, stock, bank) & export on every surface, TallyPrime XML migration with a verification report, SQL views + ODBC, personal API tokens (ADR-030) | **parity** — landed in v0.27.0 (Phase 9); TDL (Tally's proprietary language) stays out by design — Python packs are the answer (D4) |
| Extensibility | TDL (proprietary) | Python packs, signed index, sandbox slots | **LedgerForge leads** |
| Deployment | Windows LAN (Silver/Gold) | Native Windows app, web container, REST API, CLI | **parity** — landed in v0.28.0 (Phase 10): signed self-verifying updates with deltas, code-signing gates armed in the workflow, macOS + Linux packages, opt-in crash reporting (ADR-031); a paid signing certificate still buys the last SmartScreen mile |
| Price | ₹22,500+ per seat | AGPL-3.0, free forever | **LedgerForge leads** |

The honest summary: **the accounting core is at parity** — bill-wise, orders, cost
centres, banking, inventory, GST compliance depth, payroll depth and data
exchange all landed (v0.20–v0.27), and distribution hardening landed in
v0.28. What remains toward v1.0 is the parity release itself
(Phase 11). That is exactly what the remaining phase below delivers —
**and it has: v1.0.0 is the parity release, walked and proven.**

---

## 3. The phases

### Phase 1 — v0.19.0 · The completeness pass *(landed in v0.19.0)*

Close every self-inflicted gap found in the v0.18 audit — no API surface without a UI,
no documented event unfired, no stale docs. **Nothing new; everything finished.**

- **W1 — The event taxonomy kept.** `master.created` fires for groups and ledgers
  (documented in the plugin guide since the beginning, never emitted). A pack can now
  react to master creation exactly as the guide promises.
- **W2 — Multi-book in the web UI.** A book switcher and a Books screen
  (create/rename/archive/switch — TallyPrime's "select company"); `X-Book-Id` plumbed
  through the web client; the 400-for-multiple-books failure mode becomes a picker.
- **W3 — Reversal from the daybook.** The correction workflow (Law 2's one legal edit
  path) stops being API-only: a confirmed `Ctrl+R` on the selected voucher posts the
  mirrored reversal and refreshes the list.
- **W4 — The periods screen (web).** Lock, unlock, close-preview and close — the native
  app's period page, in the web UI, behind the admin role.
- **W5 — Native window parity.** FX line entry in the native voucher dialog and the
  F4–F9 voucher-type keys of the Tally keymap.
- **W6 — Docs and hygiene.** README status honest to the shipped version;
  `docs/analysis.md` §9 gains its v0.18 row; this roadmap is the plan of record;
  `cryptography` becomes a direct dependency (it is imported by the trust gate).

### Phase 2 — v0.20.0 · Bill-wise & outstanding management *(ADR-023, landed in v0.20.0)*

The accounting-core gap. TallyPrime's party ledgers carry **bill-wise details**: every
invoice opens a named bill, every receipt settles specific bills, and the receivables/
payables position is bill-level, not ledger-level.

- Bill references on commercial documents (`new` / `against` / `on-account`) as a pack on
  the `meta` document store — settlement is a deterministic fold over posted documents,
  the pattern proven by inventory and payroll.
- **Bills receivable / bills payable** reports with ageing buckets (0–30/30–60/60–90/90+).
- **Ledger statement** — the party's running statement, bill-linked, opening balance to
  date, as a pack report (PDF/CSV through the ADR-013/014 bridges).
- **Interest calculation** on overdue bills (configurable rate per ledger; interest
  vouchers postable through the normal engine path — never a side effect).
- Receipt allocation UX: the on-account/against-ref decision at entry time in the web UI
  and the native dialog.

### Phase 3 — v0.21.0 · Order processing *(ADR-024, landed in v0.21.0)*

The quote-to-cash and procure-to-pay chains TallyPrime users live in.

- Quotation, Sales Order, Delivery Note, Purchase Order, GRN — five document types as one
  pack on the `meta` store, each a print-claiming document view (ADR-013).
- Order position tracking: pending quantity, fulfilled quantity, order status, from a
  deterministic fold; **pending orders** report (sales and purchase sides).
- Order → invoice linkage (an invoice can settle against an order; the order position
  updates) and PO → GRN → purchase invoice linkage, same mechanism.
- The native window gains the document pickers alongside vouchers.

### Phase 4 — v0.22.0 · Cost centres & cost categories *(ADR-025, landed in v0.22.0)*

The first core-schema change since 0006 (a `cost_allocations` table: voucher entry ×
cost centre × amount, Dr/Cr preserved, Σallocation = entry amount enforced exactly) —
because allocations must aggregate with the same rigour as entries, they belong in the
database, not in `meta`.

- Cost centre master (tree, like groups) and cost categories (primary/secondary).
- Allocation at entry time in both UIs (the unallocated remainder is always visible; a
  voucher posts only when every line is fully allocated or explicitly left unallocated).
- Cost-centre break-up reports: centre summaries, ledger-wise within centre, category
  rollups — through the same closure-table machinery as group rollups.

### Phase 5 — v0.23.0 · The banking pack *(ADR-026, landed in v0.23.0)*

- **Bank reconciliation**: bank statement import (CSV and MT940), a matching engine
  (amount-exact, date-window, reference-fuzzy; suggestions never auto-commit), manual
  match/unmatch, and the reconciliation statement (book vs bank, as-of, with the
  reconciled-balance proof).
- **Cheque management**: the cheque register (allocated on payment/receipt vouchers,
  status lifecycle issued→presented→cleared, PDC tracking with the maturity date).
- The connected-banking APIs (per-bank) stay out of core by design (D3); a webhook event
  seam lets gateway integrations deliver statements through a plugin.

### Phase 6 — v0.24.0 · Inventory depth *(ADR-027, extends ADR-011 — landed in v0.24.0)*

- Batch, serial and expiry tracking on stock documents (batch-wise in/out/transfer/
  recount; expiry-ageing report; FEFO suggestion on out-movements — valuation stays
  weighted-average).
- **Godowns as a first-class dimension** (a location key on movements and positions,
  not "another ledger"), godown-wise stock reports.
- Re-order levels and the **shortfall report**; price lists (per-party, date-effective).
- Manufacturing: BOM definitions and manufacturing journal documents (the pack's most
  ambitious fold — cost rollup from components at their moving averages).

### Phase 7 — v0.25.0 · GST compliance depth *(ADR-028, extends ADR-017 — landed in v0.25.0)*

- **GSTR-2A/2B comparison**: supplier-purchase register vs the portal download, an ITC
  reconciliation report (matched / mismatched / missing-in-books / missing-in-portal).
- **GSTR-3B summary** (outward, inward, ITC available/utilised, net payable) as a pack
  report from the same folds GSTR-1 already uses.
- **GSTR-9** annual-return scaffolding (annexure tables as reports over the year's data).
- **E-way bill outbox**: the IRN pattern applied to e-way bills (after-commit JSONL queue
  + bridge CLI to the operator's adapter).
- Credit/debit note returns handling and multi-GSTIN (several registrations in one book,
  place-of-supply already decided per voucher).

### Phase 8 — v0.26.0 · Payroll depth *(ADR-029, extends ADR-012 — landed in v0.26.0)*

- Attendance & leave registers as the fifth fact table (migration 0011, the
  GSTR-2B discipline restated for the shop floor): idempotent by fingerprint,
  latest-snapshot-wins, calendar-guarded counts.
- Attendance-derived earning components: `per_day` × payable days of the live
  register (plugin API 0.14, `Resolver.attendance_lines`) — the derivation
  rides the stored document; no register, no derivation.
- Loans & advances: issue voucher with the exact-Decimal annuity EMI schedule
  (advisory — recovery is a per-run fact), the `recovery` component kind with
  the outstanding fold's ceiling (named loans adopt their scheduled EMI), and
  `loan_position()` + the `payroll-loans` report as the fold's window.
- Full & final settlement (leave encashment, dues, recoveries — one voucher;
  the relieved gate with reversal-reopen; payouts stay legal).
- Payslip distribution seam: the `LF_PAYROLL_OUTBOX` JSONL +
  `lf-payslip-bridge` (PDF batch through the same layout engine; SMTP only
  when the operator configures it — core never sends mail).

### Phase 9 — v0.27.0 · Data exchange & the Tally migration path *(ADR-030)*

The adoption lever: **you can move off TallyPrime onto LedgerForge**.

- CSV/Excel import: masters, opening balances, vouchers, stock positions, bank
  statements — every import a previewable, idempotent, validated transaction (the engine
  never bulk-bypasses Law 1).
- Export to CSV/Excel on every report and list surface.
- **TallyPrime import**: its XML export → LedgerForge books (masters, vouchers, FX,
  inventory) with a verification report (trial balance of the import vs the source).
- Read-only SQL views + ODBC for analysts; personal API tokens for integrations
  (long-lived, scoped, revocable — the desktop's non-OIDC answer).

### Phase 10 — v0.28.0 · Distribution hardening *(ADR-031, landed in v0.28.0 — pays the ADR-021 debt)*

- **Windows code signing** (the recorded follow-up; Azure Trusted Signing or a hardware
  certificate in the release workflow) — the SmartScreen warning dies.
- **macOS and Linux native packages** of the same Qt shell.
- **Auto-update**: signed update manifest, delta-aware downloader, update-on-relaunch.
- Opt-in crash reporting (no telemetry by default — it stays a pledge, not a setting).

All four shipped: the workflow signs the moment the secrets exist; macOS
(arm64 `.app`) and Linux (x64 tar.gz) packages build from the same spec,
smoke-gated like Windows; the Ed25519-signed `update_manifest.json` +
file-level deltas drive update-on-relaunch (fail closed, verified staging,
idempotent apply); crashes are captured locally always and sent nowhere
unless chosen (ADR-031).

### Phase 11 — v1.0.0 · The parity release *(landed in v1.0.0)*

- **The accountant's day, in CI**: a scripted acceptance run on the native app — create
  company, masters, fifty vouchers across packs, an order chain, bill-wise settlement,
  bank reconciliation, GST statements, payroll, period close — green on every release.
  *(Landed: `--accountants-day`, ADR-032; it gates every platform leg of the release
  workflow after `--smoke`. Composing the packs for the first time surfaced and fixed a
  real direction bug in the banking pack — the composition class of bug isolated suites
  cannot catch, which is why the day exists.)*
- Performance certification at 10× the §2.3 workload model (posting latency, report
  latency under one second, partitioned and cached paths).
  *(Landed: `scripts/certify_performance.py` — ≥2,000 vouchers/min concurrent through
  the public posting path, gapless, exactly balanced; warm TB + P&L under a second,
  fast proven equal to slow to the cent; CI `certify` job on main and tags, and inside
  the release publish job — the tag released is the tag certified.)*
- Accessibility audit of both UIs; docs completion pass; the v1.0 announcement with the
  full capability ledger re-run (the table in §2, all-green or honestly annotated).
  *(Landed: `tests/test_accessibility_native.py` — every interactive widget named,
  focus traversal, table context; the web axe-core pass asserting zero serious/critical
  violations with lesser findings logged for honest triage; the announcement is the
  1.0.0 release notes, the §2 table re-derived there, trailing rows named in plain
  words: SmartScreen needs a funded certificate, connected banking and portal adapters
  stay plugin-side by design.)*

### Beyond v1.0 (recorded, deliberately unscheduled)

An AI assistant against the books (TallyIra's answer — natural-language query, anomaly
flags, reconciliation suggestions; privacy-preserving: local-model option, no data
egress by default) · multi-user LAN mode for the native window · a read-only mobile
companion · the community index grown beyond its one demo plugin.

---

## 4. Tracking

- **One GitHub milestone per phase** (Phase 1 … Phase 11), one tracking issue per phase
  with the workstream checklist; Phase 1 additionally carries an issue per workstream.
- Phase = milestone closed when its acceptance shows in the release: feature + ADR +
  tests + docs + the smoke-gated Windows build, nothing less.
- Anything that bends a law, adds a schema object, or narrows the plugin contract gets
  its ADR *before* its first line of code — the repo's rule since 001.

*Maintained in the repository as `ROADMAP.md`; changes land through pull requests like
code. Last revisited: 2026-11-09, at the close of Phase 11 — the road to v1.0 is
walked; what comes next lives in "Beyond v1.0" above, deliberately unscheduled.*
