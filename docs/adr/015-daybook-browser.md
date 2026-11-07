# ADR-015: The daybook browser — every posted voucher browsable and reprintable

- **Status:** Accepted (2026-11-07)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

The print bridge (ADR-013) made every posted voucher a PDF, and the
voucher screen's print button (ADR-014) prints *the last* posted one. But
printing is not only a just-posted act: auditors ask for last quarter's
sale, a supplier disputes an old payment, an employee needs a payslip
reprint. What was missing is the accountant's oldest screen — the
**daybook**: the chronological register of everything posted, any line of
which can be reprinted as its document.

The pieces already existed: `GET /vouchers` lists posted vouchers (type
and date filters), `GET /vouchers/{id}/pdf` prints any of them. Two gaps:

1. **The amount column.** A daybook row shows the voucher's amount. The
   list carried per-entry amounts (storage scale, 8 decimals) but no
   total — and summing them in the browser would be client arithmetic on
   money (Law 3).
2. **The screen.** No browse surface: the payslip screen indexes payroll
   runs only; the voucher screen shows the last posted voucher only.

## Decision

### 1. The engine computes the amount (Law 3 stays law)

`VoucherOut` gains `amount`: the **debit-side total in book currency**,
summed in exact `Decimal` by the engine and quantized to cents at the
edge. Every posted voucher is balanced (opening balances post as balanced
journals, ADR-002), so the debit total *is* the voucher amount. The field
is additive — old clients ignore it, the daybook displays it as a string,
and no client ever sums.

### 2. A screen, not a report

`DaybookScreen` (web) browses `GET /vouchers` with the engine-side
filters (type, date range) plus a client-side text find (voucher number,
narration, ledger names — pure string matching, no arithmetic):

- **The keyboard contract** (ADR-004), same as every screen: `Alt+B`
  opens; `↑/↓` or `j/k` walk the list (inert while a filter control has
  focus, so native keys keep working); `Enter` / `Ctrl+P` print the
  selected voucher; `Esc` is back to entry. All commands are palette-
  visible, user-rebindable.
- **Reprints are identical by construction**: the PDF renders from
  immutable posted entries (Law 2), so the 100th reprint is byte-for-byte
  the document that printed the day it was posted.
- **Document chips** mark what each voucher carried (payroll, payout,
  invoice, stock, reversal) — a reminder that the same row prints as a
  payslip, a tax invoice or a voucher slip.
- **The payroll-run carve-out**: a run covering several employees needs
  `?employee=` (the payslip subject, ADR-013). The daybook prints a
  single-employee run directly (passing the employee) and, for a
  multi-employee run, explains and points at the payslip screen (`Alt+P`)
  instead of erroring. Payouts, reversals and plain vouchers print as
  voucher slips.
- **The detail pane** previews the selected voucher's entries with
  ledger names, Dr/Cr and amounts — display-formatted only (trailing
  storage zeros trimmed; real sub-cent precision shows exactly as
  posted), never recomputed.

### 3. No new kernel surface

The browse is the existing list endpoint; the print is the existing
bridge endpoint. The only engine change is the additive `amount` field.
Pack reports (PF/ESI/TDS, ADR-014) stay out: a daybook is navigation,
not a fold.

## Consequences

- **(+)** Any historical voucher is one `Alt+B` and two arrows away from
  a fresh PDF — the audit/reprint loop closes inside the keyboard-first
  contract, no mouse required.
- **(+)** The amount column is engine-computed; the browser never does
  money math (Law 3), and the field benefits any future consumer of the
  list (the payslip screen, API clients).
- **(+)** Zero schema change: `alembic` head remains `0005_voucher_meta`.
- **(−)** The list is capped by `limit` (200 default); books with dense
  days must narrow by date/type. Pagination is a future decision, taken
  when a real book hits the cap.
- **(−)** "Was reversed" is not shown (the list endpoint exposes
  `reversal_of`, not reversed-by); a reversal is visible only as its own
  row. A `reversed_by` annotation is cheap to add if wanted.

Zero schema change. Engine: `schemas.py` + `api/vouchers.py` (the amount
field). Web: `components/DaybookScreen.tsx`, `daybook/model.ts`,
`api/client.ts` (query filters), `App.tsx` (`Alt+B`). Exercised by
`tests/test_posting.py::test_voucher_amount_is_engine_computed` and
`frontend/web/tests/daybookScreen.spec.tsx` (12 tests: model money/kinds,
browse, navigation, reprint paths, the multi-employee carve-out, filters).
