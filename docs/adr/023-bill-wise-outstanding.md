# ADR-023: Bill-wise & outstanding management — allocations as a signed fold on the meta store

- **Status:** Accepted (2026-11-07)
- **Deciders:** mir-ashiq
- **Supersedes:** —
- **Extends:** ADR-010 (voucher meta), ADR-011 (prior_documents fold), ADR-013/014 (print & report bridges)

## Context

TallyPrime's party ledgers carry **bill-wise details**: every invoice opens a named
bill, every receipt settles specific bills, and the receivables/payables position is
bill-level, not ledger-level. This is the accounting-core gap the roadmap's Phase 2
(v0.20.0) exists to close — "the accounting-core gap", ahead of orders, banking and
the rest, because outstanding management is what an accountant does *first* with a
party ledger, before any workflow around it.

The requirements (ROADMAP.md §3 Phase 2):

- bill references `new` / `against` / `on-account` on commercial documents, as a pack
  on the `meta` document store — settlement a deterministic fold over posted documents,
  "the pattern proven by inventory and payroll";
- bills receivable / bills payable with ageing buckets (0–30/30–60/60–90/90+);
- the party **ledger statement** — bill-linked, opening balance to date, as a pack
  report through the ADR-013/014 bridges;
- **interest calculation** on overdue bills — configurable rate per ledger, interest
  vouchers posted through the normal engine path, never a side effect;
- the on-account/against-ref decision at entry time in the web UI and the native dialog.

Two structural facts shaped the design. First, allocation is *not* a line-builder: a
receipt's lines are already built (explicit entries, or the invoicing pack's
`meta.receipt` document); what bills add is the **split of the party's movement across
named bills**. A pack that derived lines would fight the existing packs rather than
annotate them. Second, a ledger statement needs the party's *entries* — engine data a
pack has never been able to read; `prior_documents` returns documents, not ledger
lines, and the report cannot be honest without both.

## Decision

### 1. Allocations are signed dr/cr refs; the law is a per-voucher conservation invariant

`meta.bills` rides any voucher that moves a party ledger:

```json
{
  "party": "Acme Traders",
  "refs": [
    {"kind": "new",       "ref": "INV-42",  "dr": "1430.00", "due_days": 30},
    {"kind": "against",   "ref": "INV-42",  "cr": "500.00"},
    {"kind": "on_account",                 "cr": "100.00"}
  ],
  "default_interest": {"annual": "18", "grace_days": "7"}
}
```

TallyPrime's bill-wise screen has Dr/Cr columns per ref line; the model here is the
same, and it is chosen because it makes the correctness statement **Law-1-shaped**:

> For every voucher carrying `meta.bills` for party P:
> **Σ ref dr − Σ ref cr == P's net movement on that voucher** (Σ party debit lines −
> Σ party credit lines), exactly, in Decimal.

The validator enforces it *after all transformers* (validators run after every
transformer, so document-mode lines — `meta.invoice`, `meta.receipt`, `meta.stock`
with a party — are final when the check reads them; the pack never builds lines and
never depends on transformer order). Because the invariant holds per voucher, and both
sides start at zero, an induction gives the pack's master theorem:

> **Σ open bill outstandings + on-account pool == the party's ledger balance**, at
> every point in the book's history.

The fold (`prior_documents("bills")`, reversals netted, replayed oldest-first by
`(voucher_date, voucher_id)`) applies refs mechanically: `new dr X` opens a receivable
bill of X; `new cr X` opens an advance (negative outstanding); `against` moves the
named bill toward zero — settlement is strictly toward zero: it reduces the
outstanding or closes it exactly, never past zero and never away from zero
(over-settlement is refused because it is always expressible correctly as
`against` + `on_account`, the TallyPrime flow — which keeps the fold a
trivial, policy-free replay); `on_account` adjusts the party's pool. `against` must
name a bill a *prior* document opened; `new` must not collide with the party's open
bill of the same ref. Bill direction is derived, never stored: outstanding > 0 is a
receivable, < 0 a payable-side balance — one fold serves both reports, and a party
that is both debtor and creditor nets honestly.

**On-account is a pool, never auto-applied.** FIFO auto-application would fabricate
allocations the accountant never made; the reports surface the pool as its own line
per party (the same "a blank is honest, an invented number is not" discipline as the
GSTR-1 quick-tax exclusion). Applying on-account to a bill is a netting journal —
Dr party / Cr party, `refs` balancing to a net of zero — which the model admits
naturally.

### 2. Guardrails, all 422

Unknown party ledger (bills never invent masters — the party must already exist, by
the engine's own rule or an earlier transformer's `ensure_ledger`); unknown ref
kinds; non-2-dp or non-positive amounts; conservation violations; settling an
unopened bill; over-settlement past zero; settling away from zero; re-settling a
closed bill; duplicate open refs; bills on a payroll
document (a payables model, deliberately out of scope); and an `interest_charge`
document alongside any other document key or explicit refs — the one mode that
*does* derive lines must be unambiguous.

### 3. Interest: computed from documents, posted as a document

Rate resolution is a fold over declarations, newest-wins by date: the bill's own
`interest.annual` (on its `new` ref) → the party's `default_interest` (the latest
declared on or before the bill's date) → the request's explicit `rate` parameter →
none (the bill prints without interest and a footnote says why). Interest for an
overdue open bill is `pending × annual/100 × days_overdue/365`, exact Decimal,
quantized to cents once at the end (grace days shift the due date; per-diem division
at 50-digit precision, the posting engine's own headroom rule).

The computation is published two ways, both deterministic replays of the same fold:

- the **`bills-interest` pack report** (`as_of`, optional `party`, `rate`,
  `grace_days`) — the preview, PDF or CSV;
- the **`interest_charge` document mode** — `meta.bills = {"party": P,
  "interest_charge": {"ref": "INT-1"}, "as_of": …}` on a dated voucher (the as-of
  must equal the voucher date, or the preview and the posting would disagree). The
  transformer derives one Dr party line per interest-bearing overdue bill (the
  narration carries the computation) and one Cr *Interest Income* line (under
  Indirect Incomes), then **auto-opens one bill for the total** — the charge must
  appear in the outstanding position or the master theorem breaks. The charge is a
  voucher like any other: reviewed in the daybook, reversible, and never a side
  effect of anything.

### 4. Plugin API 0.8 — two additive primitives

- **`Resolver.ledger_entries(ledger_id)`** — every posted entry of the book on one
  ledger, oldest first, as `LedgerEntryLine(voucher_id, voucher_no, voucher_date,
  reversal_of, dc, amount, narration, meta)`: the engine data a ledger statement
  needs, book-scoped like every Resolver read. The kernel returns rows; their
  interpretation (running balance, windowing, bill links) is the pack's.
- **`PackReport.data`** — a JSON payload alongside `view`/`csv`, served by
  `GET /api/v1/reports/pack/{key}?format=json`. ADR-014's bridges stay the print/machine
  path; JSON is what a UI consumes (the web Outstandings screen and both allocation
  dialogs read the same `bills-open` report the PDF is rendered from — one fold,
  every surface).

Both are additive; the floor semantics of ADR-011 keep every 0.2–0.7 pack loading.

### 5. The pack (`ledgerforge/contrib/bills.py`, zero schema change)

Reports registered: `bills-receivable` and `bills-payable` (ageing by due date —
`not due / 0–30 / 31–60 / 61–90 / 90+`, on-account shown separately per party),
`ledger-statement` (the party's running statement from the first entry — opening
balance before the window, per-voucher bill links, closing balance), `bills-interest`
(§3) and `bills-open` (one party's open bills + on-account — the dialog feed).
Surfaces: the web gains an Outstandings screen (`Alt+I`, receivable/payable, ageing
columns, party filter) and a bill-allocation dialog on the voucher screen
(`Alt+L` when the cursor's ledger is a party — Sundry Debtors/Creditors — with the
unallocated remainder offered as on-account); the native voucher dialog gains the
same allocation panel, and the native reports page carries all four report keys.

## Consequences

- **(+)** Phase 2's whole scope lands with **zero schema change and zero kernel law
  change** — the third pack to prove the ADR-011 pattern on a read-heavy feature,
  and the first whose correctness is an inductive invariant over the ledger itself:
  the reports can (and the tests do) assert that Σ bills + on-account equals the
  trial-balance figure for every party, on every fold, forever.
- **(+)** The allocation model is pack-agnostic by construction: it annotates
  `meta.invoice`, `meta.receipt`, `meta.stock` documents and plain vouchers alike,
  because it validates against final lines instead of building them. When Phase 3's
  order documents arrive, their invoices link to bills with no change here.
- **(+)** Interest never mutates silently: the only way interest reaches the books is
  a posted, reviewable voucher — the ADR-002 write path, unchanged.
- **(−)** The fold is O(all bills documents of the book) per validation and report —
  the same ADR-011 trade-off, fine at SMB scale; the same escape hatch applies
  (promote to schema via a migration when a book outgrows it).
- **(−)** `against`-ref validation inside the posting transaction serializes on the
  document fold rather than a lock: two concurrent receipts oversettling the same
  bill can both read the fold, but the conservation invariant holds per voucher, so
  the worst case is an over-settled bill that a later reconcile surfaces — the
  strictly-toward-zero refusal at posting time is the guard, not a proof of global
  serializability. (The engine's own counter lock is untouched; parties settle at
  human speed.)
- **(−)** v0.20 scope, documented: bills are per-party per-book (no cross-book
  netting — ADR-009 isolation, as everywhere); on-account is surfaced, never
  auto-applied; interest is simple (no compound, no TallyPrime's transaction-by-
  transaction slab rates); credit-period ageing (by bill date) is not offered — due
  date only; payroll payables are out. A richer interest engine or auto-application
  ships as a plugin on the same 0.8 primitives, not by bending this pack.

Shipped with no migration: `alembic` head unchanged. The consumer is
`ledgerforge/contrib/bills.py`, registered as an entry point and loaded at boot;
exercised by `tests/test_bills_plugin.py` (allocation shapes, the conservation
invariant, reversal netting, ageing buckets, statement balances vs the trial
balance, interest arithmetic and the charge document, book isolation) and the web
and offscreen-native suites for the new surfaces.
