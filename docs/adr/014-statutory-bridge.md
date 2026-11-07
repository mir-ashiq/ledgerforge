# ADR-014: The statutory bridge — PF/ESI statements from payroll folds, and pack reports

- **Status:** Accepted (2026-11-07)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

ADR-012's payroll pack named its after-commit seam "the spot a payslip PDF
generator or a PF filing bridge subscribes to". The payslip half of that
promise landed with the print bridge (ADR-013). This ADR is the statutory
half — plus the print button that makes the bridge's endpoint universal in
the UI.

An Indian employer files two recurring statements built from exactly the
wage data the payslips print:

- **EPF (Provident Fund)** — monthly, per member: UAN, gross wages, the
  EPF/EPS/EDLI wage bases (configured components, capped at the statutory
  ceiling), the employee share, the pension share, the employer's EPF share
  and EDLI — the ECR's core columns.
- **ESI (Employee State Insurance)** — per insured person: IP number, gross
  wages, the insurability test (monthly gross at/below the threshold), the
  employee and employer shares.

The questions this ADR answers:

1. **Where does a report that is not one voucher's document live?** The
   print bridge dispatches by a *single voucher's* meta keys. A monthly
   statutory statement folds *many* vouchers — every run of the month — so
   there is no voucher to print and no meta key to claim.
2. **Who owns statutory arithmetic?** Wage bases, ceilings and rates are
   statutory-pack domain knowledge, never the kernel's. But the *fold* —
   which runs count, how reversals net, how off-cycle duplicates sum — is
   precisely the payroll pack's (ADR-012).
3. **Where do identifiers (UAN, ESI IP number) come from?** There is no
   employee master (deliberately — adding one is out of scope); the run
   documents carry what the caller posted, and extra fields persist exactly
   as stored (ADR-012).

## Decision

### 1. Plugin API 0.6 — `register_report(key, factory)` and `PackReport`

Packs publish book-scoped reports under a key; the kernel serves them at
`GET /api/v1/reports/pack/{key}` (reader role) and stays **report-blind**,
exactly as it stays meta-blind:

- **Factory** `(resolver, params) -> PackReport` — a book-scoped `Resolver`
  for derived reads and the caller's query parameters as a plain string
  dict; the pack validates its own parameters (`ValidationError` → 422).
- **`PackReport`** — either a `DocumentView` (rendered to PDF by the
  ADR-013 layout engine — the same visual language as payslips and
  invoices) or pre-rendered CSV text for machine consumption, with a
  filename hint.

One factory per key; a later registration replaces an earlier one,
mirroring the other registries. Unknown keys are 404s naming the known
ones.

### 2. The payroll pack publishes the period fold

`payroll.period_wages(resolver, period)` — every employee's wage detail
for one `YYYY-MM`: per-`(name, kind)` component amounts and gross, folded
over the same document sequence with the same reversal netting as the YTD
fold, off-cycle duplicates summed, plus the entry's extra fields
(`uan`, `esi_ip`, …) exactly as posted. The statement therefore agrees
with the payslips and the ledger *by construction* — a PF figure that
disagrees with a payslip is impossible, not merely tested-for.

### 3. The statutory pack computes, never invents

`contrib/statutory.py` (the fourth business pack, zero schema change)
folds `period_wages` into the two statements:

- **Rules are parameters with long-standing defaults**: PF wage components
  (basic + DA by default), the ₹15,000 wage ceiling, the 12% employee /
  8.33% pension / 0.5% EDLI rates; the ₹21,000 ESI threshold with 0.75% /
  3.25% shares. All overridable (`wage=`, `ceiling=`, `threshold=`,
  `round=cent|rupee`) — they change by notification, not by code.
- **The ER share is derived**, not rated: `quantized(EE rate × base) −
  EPS share`, so employee + employer shares always reconstruct the full
  rate at the applied rounding.
- **Identifiers print exactly as posted** — blank (with a footnote) when
  the run didn't carry them. A fabricated UAN is worse than a blank one.
- **Honest footers**: every statement carries the applied rules and the
  "verify against the portal's current template before uploading" note.
  This is a *reporting bridge*, not a portal-certified file generator.

### 4. The print button (web)

The voucher screen gains `Ctrl+P` and a Print chip for the **last posted
voucher** — through the same `GET /vouchers/{id}/pdf` the payslip screen
uses, so *any* posting voucher (sale, journal, payment — the voucher-slip
fallback) prints the moment it is posted. Before anything is posted,
`Ctrl+P` explains instead of printing the page.

## Consequences

- **(+)** A community pack publishes a report with one function — a
  TDS statement, a GST summary, an attendance register — no core change,
  same route, PDF or CSV.
- **(+)** Zero schema change: `alembic` head remains `0005_voucher_meta`.
- **(+)** The statutory statements, the payslips and the ledger all replay
  one fold; the bridge also exercises plugin API 0.6 as its first live
  consumer, the same proof discipline as ADR-011/012/013.
- **(−)** Portal upload formats drift (the EPFO ECR alone has changed
  column sets several times); the bridge answers with parameters and
  honest footers, not with a frozen claim of portal compatibility.
- **(−)** Identifiers must be posted on run entries (`uan`, `esi_ip`);
  runs posted without them print blanks. An employee master (if ever) is
  a separate decision.

Zero schema change. Core: `src/ledgerforge/plugins/__init__.py` (API 0.6)
+ the route in `api/reports.py`. Packs: `payroll.period_wages` +
`contrib/statutory.py` (registered as an entry point). Web:
`VoucherScreen.tsx` (Print chip, `Ctrl+P`) + `api/client.ts` (optional
`employee`). Exercised by `tests/test_statutory_plugin.py` (12 tests:
PF math/caps/rounding, ESI threshold, CSV and PDF formats, reversal
netting, off-cycle summing, nil months, identifier pass-through, guards,
book isolation) and `frontend/web/tests/voucherScreen.spec.tsx` (print
chip, Ctrl+P flow, nothing-posted hint, blocked-tab error).
