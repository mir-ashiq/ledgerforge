# LedgerForge 1.0.0 — the parity release

**Phase 11 of the [v1.0 roadmap](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md):
parity demonstrated, not asserted (ADR-032).** v0.19–v0.28 closed every
capability gap the roadmap named; 1.0.0 is the release where LedgerForge
stops claiming parity and starts proving it — in the pipeline, on every
release, on every platform. This announcement is itself an honest
ledger: the capability table below was **re-derived from what the
release actually exercises**, not pasted from the roadmap, and the two
rows that still trail are marked as trailing in plain words.

## The accountant's day — composition, proven per release

Every pack had its suite; nothing proved the packs *compose* — that one
book can carry an order chain, bill-wise settlement, a bank
reconciliation, GST statements, a payroll run and a period close in one
continuous session without disagreeing about money. Now something does:
**`LedgerForge --accountants-day`**, the scripted acceptance run that
executes a full working day through the native window's own API client —
one company, fifty vouchers across every pack, an order chain each side,
a manufacturing journal, a bank statement imported and reconciled to the
proof, GSTR-1 and GSTR-3B with a GSTR-2B portal download reconciled, an
attendance-derived payroll run with its payslip, and the financial year
closed with the sweep's totals agreeing with the preview. Every step
asserts the *numbers* — the outstanding is zero after settlement, the
trial balance's difference is exactly 0, the 3B's net payable equals the
invoices' tax, the sweep's debit equals the preview's — and the day ends
by rendering the real Qt window over the books. It runs in the release
workflow **after `--smoke` on every platform leg**: a release that
cannot run an accountant's day does not ship.

The day earned its keep before it ever reached CI: composing the packs
for the first time surfaced a real bug no isolated test could catch —
the banking pack's reconciliation had grown a direction convention that
contradicted the invoicing and payroll packs and turned bank balances
negative on the balance sheet (see the breaking change below). That is
exactly the class of bug 1.0 would otherwise have shipped.

## Performance certified at 10×, in the pipeline

`scripts/certify_performance.py` seeds the 10× book — 3,000 groups,
34,000 ledgers, 1.2M voucher entries over three financial years, the
scale-out partitions engaged — and certifies two budgets with one exit
code:

- **Posting**: at least **2,000 vouchers per minute**, concurrent,
  through the engine's public posting path, with gapless numbering and
  an exactly-balanced trial balance at the end;
- **Reports**: the warm cached **trial balance and profit & loss each
  answer in under a second** on the 10× book, through the partitioned +
  cached paths, which the certificate first proves equal the slow path
  to the cent.

It runs as CI's `certify` job on every push to `main` and every `v*`
tag, **and inside the release pipeline's publish job** — the tag being
released is the tag that certified. A regression past a budget fails
the pipeline before a release exists, not after.

## Accessibility: a contract, both UIs

- **The native window** ships `tests/test_accessibility_native.py`:
  every interactive widget must expose an accessible name (its label,
  placeholder or an explicit name — never silence on something
  focusable), focus traversal must visit each page's surfaces without
  escaping the window, and every table's columns must carry header text
  — the screen reader's context for every cell. The audit found 19
  nameless controls across the pages; all named now.
- **The web screen** grows an axe-core pass (`@axe-core/playwright`,
  `e2e/a11y.spec.ts`) over its principal states — the entry grid, the
  day book, the command palette — asserting **zero serious/critical
  WCAG 2.x A/AA violations**. Lesser findings print into the run log as
  an honest triage backlog, not a silent pass.

## The capability ledger, re-derived

Every row re-derived from what `--accountants-day`, the certification
and the audits actually exercise on the shipped 1.0.0:

| Capability | TallyPrime | LedgerForge 1.0 | Verdict |
|---|---|---|---|
| Double-entry core, vouchers, reversal | ✔ | ✔ — plus append-only history and exact-decimal enforcement in the database | **LedgerForge leads** |
| Chart of accounts, multi-company, period lock/FY close | ✔ | ✔ (28-group seed, closure rollups; the day closes a year) | parity |
| Keyboard-first UX | ✔ (its identity) | ✔ rebindable keymap, palette, both UIs | parity |
| Invoicing, bill-wise, outstanding, ageing, interest | ✔ | ✔ (the day settles every bill to zero, one on-account advance) | parity |
| Orders (quote/SO/PO/DN/GRN), pending folds | ✔ | ✔ (the day drains both chains) | parity |
| Cost centres & categories | ✔ | ✔ (the day's rent fully allocated) | parity |
| Banking, BRS, cheques | ✔ (incl. connected banking) | ✔ statements, matching, the reconciliation proof, the cheque register (PDC in the day); **connected banking stays plugin-side by design** | parity; one row trails by design |
| Inventory (batches/serials/expiry/godowns/re-order/mfg) | ✔ | ✔ (FEFO batches, godowns, price lists, the day's BOM roll-up) | parity — weighted-average valuation by design |
| GST (GSTR-1/2A/2B/3B/9, notes, e-way, e-invoice) | ✔ | ✔ (the day's 2B reconciles matched; IRN/e-way outboxes + bridges); **portal adapters stay plugin-side by design** | parity; one row trails by design |
| Payroll (attendance, loans, F&F) | ✔ | ✔ (the day's attendance-derived run, PF withheld, payslip PDF) | parity |
| Data exchange (Excel/CSV, migration) | ✔ (TDL/XML) | ✔ CSV/XLSX import-export everywhere, TallyPrime XML migration with verification, SQL views + ODBC, API tokens; TDL stays out — Python packs are the answer | **LedgerForge leads** |
| Extensibility | TDL (proprietary) | Python packs, signed index, sandbox slots | **LedgerForge leads** |
| Deployment & updates | Windows LAN, paid | Windows/macOS/Linux natives, **signed self-verifying delta updates**, opt-in crash reporting; **an unsigned exe still meets SmartScreen until a signing certificate is funded** | one row trails, in plain words |
| Price | ₹22,500+ per seat | AGPL-3.0, free forever | **LedgerForge leads** |

The two trailing rows are not surprises; they are the same honest
blanks the roadmap has carried since v0.28: SmartScreen needs a paid
certificate, and per-bank/per-portal integrations stay plugin-side so
the kernel never depends on a third party's uptime. Neither blocks a
business day.

## Breaking change (the one), and why

**The banking pack's direction convention is corrected.** v0.23–v0.28
modeled the bank ledger as a passbook mirror: money into the bank posted
**Cr Bank**, which contradicted the invoicing and payroll packs (money
in is Dr Bank) and reported bank balances as negative assets on the
balance sheet. The packs' suites each passed in isolation — the
contradiction only surfaced when the accountant's day put all of them in
one book. In 1.0.0, everything reads the way the statement does: money
into the bank is **Dr Bank**, the reconciliation and `bank_ledgers`
balances are asset-positive, and a deposit line reconciles to a Dr-bank
entry. **Books created on v0.23–v0.28 that posted bank receipts as Cr
Bank must have those vouchers reversed and re-posted** (or migrated by
re-importing statements and re-matching); books whose banking was never
used are unaffected. The three financial laws are untouched — this is a
presentation-direction correction, not a re-posting of history.

## For the operator

- The release pipeline now gates on the day per platform, certifies 10×
  budgets in publish, and the CI `certify` job prints the certificate
  into every main-push run log — read it anytime.
- `--accountants-day` is available to you too: it boots a scratch
  company in an isolated data directory and proves your build end to
  end in ~3 minutes (`LedgerForge --accountants-day --data-dir <dir>`).
- Everything else you know from v0.28 stands: signed delta updates
  verify before use, crash reports stay yours, macOS and Linux build
  from the same spec.
