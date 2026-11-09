# ADR-032: The parity release — v1.0 is a demonstrated day, a certified budget, and an honest ledger

- **Status:** Accepted (2026-11-09)
- **Deciders:** mir-ashiq
- **Relates to:** ROADMAP.md §3 Phase 11 (issue #17), ADR-004 (the keyboard
  contract), ADR-021/022 (the desktop and its native window), ADR-031 (the
  release pipeline this phase hard-wires its gates into), docs/analysis.md
  §2.3 (the workload model the certification multiplies)

## Context

v0.19–v0.28 closed every capability gap the roadmap named; the capability
ledger (ROADMAP §2) reads parity or leads on every row. But "parity" claimed
by a table and parity *demonstrated* are different products. TallyPrime's
real moat was never a feature list — it is that a practicing accountant can
sit down at 9am and run an entire business day without the software being
the story. Phase 11 is the release where LedgerForge stops asserting that
and starts proving it, in the pipeline, on every release. Four workstreams,
one shape each.

**First — capability claims need a scripted day, not a demo.** The suite
proves every pack in isolation (678+ tests); the smoke proves the shipped
binary boots. Nothing proves the packs *compose* — that the same book can
carry an order chain, bill-wise settlement, a bank reconciliation, GST
statements, a payroll run and a period close in one continuous session
without the pieces disagreeing about money. Composition bugs are exactly
what isolated tests cannot catch, and exactly what an accountant hits on
day one.

**Second — performance was measured at 1×; v1.0 certifies 10×.** The
workload model (analysis.md §2.3) is a mid-size book: ~300 groups, ~3,400
ledgers, 120k voucher entries, posting peaks of a few hundred vouchers a
minute, reports under a second. The benchmarks measured that. A v1.0 stamp
on a product inspired by TallyPrime — software that routinely carries a
decade of a company's history — must hold its budgets at an order of
magnitude more book, or say which budget bends and where.

**Third — an accounting product that a screen-reader user cannot audit is
not done.** The keyboard contract (ADR-004) made operability the product's
identity, but operable-by-keyboard and accessible-to-assistive-tech are
different claims; neither UI has ever been audited.

**Fourth — the announcement must be an honest ledger, not a victory lap.**
Every previous phase re-ran the capability table against the shipped
version. v1.0's announcement does it once more, in public, with the rows
that still lose marked as losing — that honesty is the product's
accumulated brand and it survives contact with a 1.0 sticker only if the
table is re-derived, not pasted.

## Decision

### 1. The accountant's day — `--accountants-day`, a scripted acceptance run

A new mode of the native app (`src/ledgerforge/nativeapp/acceptance.py`,
`LedgerForge --accountants-day`, sibling of `--smoke`): a full working day
executed against a fresh embedded engine through the native window's own
`ApiClient` — the exact door the Qt shell uses. One book ("the day's
company"), then, in order:

1. **Masters** — parties, bank and cash ledgers, a cost category with two
   centres, batch- and serial-tracked stock items, a price list;
2. **Fifty vouchers across packs** — openings; purchases that move stock in
   (batches, expiry, godown); sales that move stock out, open GST-registered
   invoices and their bills; receipts that settle those bills (and one
   on-account); payments (one by cheque, for the register); a contra; a
   rent journal fully allocated across cost centres;
3. **An order chain** — quotation → sales order → delivery note → invoice
   settlement, and purchase order → GRN → purchase invoice, both sides
   proving the pending fold drains;
4. **A manufacturing journal** — a BOM consumed into finished goods, cost
   rolled up at moving averages;
5. **Bank reconciliation** — a CSV statement imported, a payment matched,
   the reconciliation proof (`proof.ok`) asserted true;
6. **GST statements** — GSTR-1 and GSTR-3B for the month, a GSTR-2B portal
   download imported and reconciled with at least one matched invoice;
7. **Payroll** — an attendance register, a salary run (a per-day wage
   component derived from attendance, a withheld PF, an employer
   contribution), the payslip PDF pulled;
8. **Period close** — the financial year locked and closed, the close
   preview's totals agreeing with the sweep voucher that lands in the next
   year.

Every step asserts the *numbers*, not just the 2xx: the bill's outstanding
is zero after settlement; the trial balance's difference is exactly 0; the
GSTR-3B net payable equals the tax the invoices computed; the close sweep's
debit equals the preview's. The day ends by constructing the real
`MainWindow` offscreen and proving it renders the day (day book count,
trial balance totals) — the window, not just the API, has seen the books.
Exit 0 with `DAY OK`, or exit 1 naming the step that lied. It runs as a
pytest module (subprocess, like the native smoke) and **in the release
workflow after `--smoke` on every platform leg** — a release that cannot
run an accountant's day does not ship.

### 2. Performance certification at 10× — `scripts/certify_performance.py`

One script, one exit code. It seeds the 10× book against a real
PostgreSQL (3,000 groups, 34,000 ledgers, 1.2M voucher entries spread over
three financial years, the scale-out partitions engaged), then certifies
two budgets and prints the certificate:

- **Posting**: sustained throughput through the engine's public posting
  path (the same code as the HTTP endpoint) of at least **2,000 vouchers
  per minute** (10× the model's "few hundred"), concurrent, with gapless
  numbering and an exactly-balanced trial balance at the end — the
  correctness proofs of `bench_posting` at 10× scale.
- **Reports**: the warm cached trial balance **and** the profit & loss on
  the 10× book answer in **under one second each** (the model's budget,
  held at 10×, through the ADR-018 partitioned + cached paths, which the
  script first proves equal the slow path to the cent).

It runs in CI as a dedicated `certify` job — on pushes to `main` and on
`v*` tags (pull requests keep the existing smoke benchmarks) — **and in the
release workflow's publish job**, so the tag being released is the tag that
certified. A regression past a budget fails the pipeline before a release
exists, not after.

### 3. The accessibility audit — both UIs, in the pipeline

- **Native (Qt)**: `tests/test_accessibility_native.py` walks the real
  `MainWindow` (offscreen) and asserts the assistive contract: every
  interactive widget exposes an accessible name (its label, placeholder or
  explicit `accessibleName` — never the empty string on something focusable
  and actionable), focus traversal visits the primary surfaces, and the
  tables' models carry accessible text for their cells' meaning where the
  plain text is not the whole story.
- **Web (React)**: the existing Playwright E2E job grows an axe-core pass
  (`@axe-core/playwright`) over the principal screens — day book, voucher
  entry, reports — asserting **zero serious (impact: serious/critical)
  violations**; findings below that bar are triaged honestly in this ADR's
  consequences, not silently ignored.

### 4. The v1.0 announcement — the capability ledger, re-derived

The release notes for v1.0.0 are the announcement, and their spine is the
capability table re-run against the shipped product: every ROADMAP §2 row
re-derived from what `--accountants-day`, the certification and the audit
actually exercise, with the two rows that still trail (SmartScreen until a
certificate is funded; connected banking and portal adapters staying
plugin-side by design) marked in plain words. README's status section
moves to 1.0.0 with the same honesty; analysis.md §9 gains its v1.0 row.

## Consequences

- **(+)** Composition is now a gate: the packs must agree about one book's
  money in one session, on every release, on all three platforms — the
  class of bug v1.0 would otherwise ship and an accountant would otherwise
  find.
- **(+)** The performance claim scales with the sticker: 10× the model,
  budgets asserted by the pipeline, certificate printed into the run log
  where any user can read it.
- **(+)** Accessibility stops being an aspiration and becomes a contract
  both UIs must keep to merge.
- **(−)** The accountant's day adds ~2–4 minutes per platform to every
  release, and the certify job ~10 minutes to every main push — the price
  of claims that survive contact with a release tag; accepted.
- **(−)** The day asserts exact numbers against seeded fixtures; a
  deliberate change to a pack's posting shape will edit the day too. That
  is the point (the day is the spec of a working business day), but it is
  a maintenance cost a demo would not have.
- **(−)** axe-core's zero-serious bar is a floor, not a ceiling; the
  moderate/minor findings will accumulate a triage backlog that must stay
  honest in this file's successors.

Shipped as: `src/ledgerforge/nativeapp/acceptance.py` (the day),
`tests/test_acceptance_day.py`, `scripts/certify_performance.py`,
`tests/test_accessibility_native.py`, the web E2E axe pass
(`frontend/web/e2e/a11y.spec.ts`), the `certify` CI job and the
release-workflow gates (`--accountants-day` per platform, certification in
publish), the 1.0.0 version bump, docs refresh and the v1.0 release notes.
