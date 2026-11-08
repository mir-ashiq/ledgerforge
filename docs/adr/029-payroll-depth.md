# ADR-029: Payroll depth — attendance registers as facts, loans & settlement as documents, payslips through the outbox

- **Status:** Accepted (2026-11-08)
- **Deciders:** mir-ashiq
- **Supersedes:** —
- **Extends:** ADR-012 (the payroll pack — this is its depth promotion, the one its own consequences reserved for a later release), ADR-025 (the schema-exception precedent — facts get tables), ADR-026 (the bank-statement argument and the kernel/pack division of labour), ADR-028 (the latest-snapshot semantics and the outbox pattern), ADR-010 (the meta store — where document facts stay), ADR-003 (the plugin contract — plugin API 0.14)

## Context

ROADMAP.md §3 Phase 8 (v0.26.0, issue #14): attendance & leave documents feeding
attendance-derived earning components, loans & advances with EMI recovery, the
full & final settlement voucher, and a payslip distribution seam for plugins.
ADR-012's own consequences named all four as deliberately out — "attendance-derived
pay (per-day rates, leave encashment) and per-employee statutory caps are out …
payslip PDFs and PF/ESI filing bridges subscribe to the RUNS after-commit seam."
The capability ledger carries payroll as **partial**: TallyPrime ✔ (attendance,
loans, F&F, payslip email), LedgerForge holding runs, payouts, YTD, the payslip
and the statutory statements. Three facts shaped the design.

**First — an attendance register is a fact, and Law 1 forbids dressing it as
one.** Present, absent, half-day and leave-with-pay counts per employee per
period are observations about the world, not double-entry movements: there is
no honest Dr/Cr pair for "Asha was present 22 days". The bank statements
(ADR-026) and the GSTR-2A/2B downloads (ADR-028) made the identical argument
for their facts — the store cannot carry them (`vouchers.meta` rides vouchers;
a voucher must balance) and the books must remember them exactly. So attendance
lands as the **fifth core-schema change since 0006**, migration
`0011_payroll_depth`, in the banking pack's exact shape: a batch (the register
receipt) and lines (the day counts), INSERT-only, trigger-guarded append-only,
idempotent by content fingerprint. One register per period per book; a
corrected register imports as a new batch beside the old (append-only history,
Law 2) and the fold reads the period's **latest** batch only — the 2B
latest-snapshot discipline, restated for the shop floor.

**Second — loans and settlements are accounting, and depth is more of the same
document grammar.** A staff loan is a real posting (Dr the employee's advance
under *Loans & Advances (Asset)* / Cr the bank); its recovery is a real
reduction of net pay that credits the advance, not a statutory liability; the
full & final settlement is a real closing accrual (leave encashment expensed,
dues credited, recoveries applied). All three extend `meta.payroll` with
document modes — `loan` and `settlement` — beside `run` and `payout`, posting
through the same transformer, guarded by the same lock discipline
(`payroll:{employee}`, one sorted pass), netted by the same reversal semantics
(a settlement's reversal reopens the relationship exactly as a run's reversal
restores YTD). The EMI schedule is computed into the document's `computed`
block with exact-Decimal annuity arithmetic — and stays **advisory**: the
schedule never books anything, recovery is a per-run fact, and the run can
adopt the due installment automatically (`kind: "recovery"` with a `loan`
reference and no amount → the schedule's EMI for that period) or accept an
explicit figure, never both. The loan outstanding is a fold — issued minus
recovered, all-time, reversal-netted — never stored state.

**Third — payslips are a delivery problem, and delivery is out-of-process by
contract.** ADR-012's RUNS seam already records every posted run; ADR-028's
e-way-bill outbox already proved the pattern for "the books owe the outside
world a document": an after-commit JSONL queue (the only hand-off — no network
I/O inside the posting path), a bridge CLI that ships it, 2xx recorded / 4xx
parked / 5xx retried. Payslips join it: every posted run appends one envelope
per employee to `LF_PAYROLL_OUTBOX`, and `lf-payslip-bridge` renders the PDF
batch (through the same ADR-013 layout engine the Ctrl+P path uses, via the
engine's HTTP API) and — only if the operator configures SMTP — emails each
payslip. **Core never sends mail**: the bridge is the only mailer, exactly the
D3 division the IRN and EWB bridges follow.

## Decision

### 1. Migration `0011_payroll_depth` — attendance as facts (ADR-025/026/028 discipline)

- `payroll_attendance_batches` — the register receipt: period (`YYYY-MM`),
  source label, line count, imported_at. Book-scoped.
- `payroll_attendance_lines` — the day counts: employee, days_present,
  days_absent, days_half, days_leave_paid (all `NUMERIC(6, 1)`, non-negative —
  a half-day is 0.5, exact), fingerprint UNIQUE per book (idempotent
  re-import), batch-scoping trigger (a line rides its own receipt's book),
  immutability triggers (append-only, like bank lines and portal rows).
- Validation at import: strict period, non-negative finite counts,
  present + absent + half + leave_paid within the period's calendar days
  (honesty guard), unique employees within a register. The kernel stores and
  validates; **payable days are the pack's arithmetic**
  (`present + half × 0.5 + leave_paid`) — the kernel stays meaning-blind, the
  same division `bank_lines` set.
- The live register is the latest batch of the period; reads never see
  superseded batches. Books are strangers (ADR-009): every row book-scoped.

### 2. Plugin API 0.14 — `Resolver.attendance_lines(period)`

The sanctioned pack read, in the `bank_lines` / `gst_portal_lines` discipline:
a `PriorDocument`-style row per employee of the period's live register, raw
counts, latest-batch-wins, reversal-free (facts have no reversals — they have
supersessions). Additive, floor semantics hold: every existing pack keeps
loading on a 0.14 build; the payroll pack (0.4.0) declares `api_compat="0.14"`
and is the live proof.

### 3. The payroll pack's four new surfaces (`contrib/payroll.py` 0.3.0 → 0.4.0)

**Attendance-derived components.** A run component may carry `per_day` (and
then must not carry `amount`): the transformer reads the period's live
attendance register, computes `per_day × payable_days`, and posts that as the
component's amount, recording the derivation (`days`, `per_day`) beside the
amount in `computed` — the payslip's footnote names it. A missing register (or
a register without the employee's row) is a 422, never a guess; an
explicit amount and `per_day` together is a 422 (ambiguous). The day rate is
the pay head's fact; the days are the register's fact — neither invents the
other.

**Loans & advances.** `mode: "loan"` — Dr the employee's advance ledger
(default `"{employee} Advance"` under *Loans & Advances (Asset)* — a
distinct name, because ledgers are unique per book and the payable ledger
already holds the employee's own; overridable `advance_ledger` /
`advance_group`, with the auto path's unrelated-ledger-refusal guard) /
Cr the bank. `principal`, `rate` (annual %, 0 = interest-free), `months`,
`first_period` produce the exact-Decimal annuity schedule in `computed`
(EMI quantized to cents; the final row absorbs the rounding so principals
sum to the principal exactly). Recovery is the run's
new component kind **`recovery`**: reduces net pay, credits the advance
ledger. Guards: an explicit recovery must not exceed the employee's aggregate
loan outstanding; a `loan`-referenced recovery (by voucher number, validated
to exist, belong to the employee and be a loan document) must not exceed that
loan's outstanding, and with no amount adopts the schedule's EMI due for the
run's period (no installment due → 422). `loan_position(resolver, employee)`
is the public fold — issued, recovered, outstanding — and the
`payroll-loans` pack report renders per-employee positions and per-loan
schedules (JSON for the UI, CSV, PDF). Direct EMI repayment outside payroll
(bank pays the lender directly) is a plain voucher the pack's fold does not
count — documented, and the report footnotes the ledger-vs-fold reading.

**Full & final settlement.** `mode: "settlement"` — one voucher: Dr the
leave-encashment expense (`days × per_day`, computed; the days are the
operator's fact — a leave *balance* needs an accrual policy, which is not this
phase's to invent), Dr any extra earnings, Cr withheld liabilities, Cr each
recovery to the advance ledger (same guards as runs), and the net dues to the
employee's payable ledger — or straight to the bank when `bank` is present.
Guards: one settlement per employee (`policy:allow_duplicate_settlement` for
the genuinely-needed second), recoveries within outstanding, the employee must
have a payroll relationship. **After a settlement the employee is relieved**:
runs, loans and further settlements for them 422 (override:
`policy:allow_post_settlement`); payouts stay legal — paying the F&F dues is
the whole point. A settlement's reversal reopens the relationship (the
ADR-010/011 netting pair). The settlement prints as its own statement view
(the payslip engine, same layout language), and `is_settled(resolver,
employee)` is the public fold.

**The payslip outbox.** The after-commit handler appends one
`lf-payroll-payslip/1` envelope per employee per run (voucher id/no, book,
employee, period, queued_at) to `LF_PAYROLL_OUTBOX` — unset or unwritable
means logged and skipped, never a failed posting. `lf-payslip-bridge`
(contrib/payslip_bridge.py, entry point) processes it: fetch each payslip's
PDF from the engine (`GET /api/v1/vouchers/{id}/pdf?employee=`), write the
batch under `--out` (default `payslips/`), and when `LF_PAYROLL_SMTP_*` is
configured, email each payslip to the address the run entry carried
(`email` rides the entry's extras, exactly as UANs ride runs — blank means
PDF-only, never invented). 2xx/OK → recorded in `payslip-responses.jsonl` and
removed from the outbox; 4xx → parked with the error; 5xx/network → pending,
retried next pass. The seam is generic — RUNS subscribers (a plugin, a
watcher) can do anything with the same events; the bridge is the reference
consumer.

### 4. The API and window surfaces

- `POST /api/v1/payroll/attendance` (writer role) — the register import, the
  bank-statement endpoint's shape: `{period, entries: [{employee, present,
  absent, half, leave_paid, note?}]}` → the batch receipt (idempotent:
  unchanged re-import is a no-op reporting `unchanged: true`). The live
  register reads through the payroll pack's `payroll-attendance` report
  (JSON for the UI, CSV, PDF).
- The native window's payroll workbench (`Alt+0`, after GST): the attendance
  editor (period + per-employee day grid), the loan issue dialog with a live
  EMI schedule preview, the settlement dialog, and the payslips pane (runs →
  per-employee PDFs, "distribute" invokes the bridge in PDF mode). The web
  screens keep their existing payslip flow; attendance rides the API.

## Consequences

- **(+)** Payroll reaches the TallyPrime answer for the shop floor: the
  attendance register feeds per-day pay heads, loans recover themselves from
  their schedules, the relieving letter's numbers are one voucher, and the
  payslip batch leaves the building without the kernel ever opening a socket.
- **(+)** The fact-table/document-mode division is now proven across three
  domains (bank lines, portal rows, attendance): observation gets a table,
  accounting gets a document, and neither pretends to be the other. Law 1
  survives its fourth temptation.
- **(+)** The loan and settlement folds reuse the run fold's machinery —
  reversal netting, lock serialization, books-are-strangers — so a reversed
  loan or a reopened F&F is not a special case but the same pair-skipping
  walk.
- **(−)** The attendance register is whole-period (a correction re-enters the
  period's register; superseded batches stay as history). A single-employee
  correction re-sends the month — the UI holds the register, so the operator
  never notices; the ADR-028 snapshot semantics demand it.
- **(−)** The EMI schedule is advisory by design: a run may recover more or
  less than the schedule's installment (within the outstanding), and the
  schedule's future rows say nothing about what runs will do. Operators who
  want strict-schedule enforcement get the guard's aggregate ceiling and the
  schedule report's paid-vs-due columns — policy, not law.
- **(−)** A leave *balance* (opening + accrual − availed) is not modelled:
  F&F encashment takes the days as the operator's fact. The accrual policy
  that would compute a balance belongs to a future pack, and inventing one
  here would be the slab computation ADR-014 already refused.
- **(−)** The payslip bridge emails only when the operator configures SMTP;
  there is no built-in provider, no tracking pixel, no read receipt — the
  pledge is privacy, and the bridge honours it by doing only what it is told.

Shipped with migration `0011_payroll_depth` (alembic head moves past
`0010_gst_2b`). The kernel service is `ledgerforge/attendance.py` with the
API at `api/payroll.py`; the pack is `ledgerforge/contrib/payroll.py` (0.4.0,
plugin API 0.14) with the bridge at `ledgerforge/contrib/payslip_bridge.py`
(`lf-payslip-bridge`); the window surface is `nativeapp/payroll.py`.
Exercised by `tests/test_payroll_depth.py` (attendance import idempotency,
supersession and validation; attendance-derived components; the loan journal,
schedule exactness and recovery guards incl. the auto-EMI; the settlement
journal, post-settlement gates and reversal-reopening; the outbox and its
failure isolation; books-are-strangers; the over-recovery race under
concurrency) and `tests/test_native_payroll.py` (the workbench, offscreen).
