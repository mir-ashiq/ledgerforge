# ADR-026: The banking pack — statements get tables, matches get a table, cheques stay documents

- **Status:** Accepted (2026-11-08)
- **Deciders:** mir-ashiq
- **Supersedes:** —
- **Extends:** ADR-002 (append-only write path — and its first deliberate boundary), ADR-003 (the plugin contract — plugin API 0.11), ADR-009 (book scoping), ADR-010 (the meta store — used where it fits), ADR-011 (the API floor), ADR-016 (drop-in trust — where per-bank adapters live), ADR-023 (the meta-fold precedent), ADR-025 (the schema-exception precedent)

## Context

ROADMAP.md §3 Phase 5 (v0.23.0, issue #11): bank reconciliation (statement
import CSV/MT940, a matching engine, manual match, the reconciliation
statement), the cheque register with PDC tracking, and a webhook seam for
gateway integrations. The comparison table carries banking as a **gap**:
TallyPrime ✔ (including connected banking), LedgerForge ✖. Three facts
shaped the design.

**First — statement lines are facts, and facts need identity.** An imported
bank statement is a set of external facts the books must remember exactly:
value date, direction, amount, reference, narrative. Re-importing the same
statement must be a no-op (idempotent), two identical-looking genuine
transactions must both survive, and the reconciliation statement's
bank-side sums must aggregate with exact-decimal rigour. That combination —
persistent facts, database-enforced uniqueness, exact aggregation — is the
ADR-025 argument for allocations, restated for statements: `vouchers.meta`
cannot promise it (there is not even a voucher to ride; a statement is not
double-entry and Law 1 forbids inventing one). So Phase 5 is the **second
core-schema change since 0006**, landing as migration `0008_banking`.

**Second — the match set is annotation, not truth, and it must be mutable.**
The heart of reconciliation is the user's judgement: *this statement line
is that book entry*. Users reconcile and de-reconcile — TallyPrime always
allowed it, and an accountant who mismatches must be able to undo without
posting a correction voucher. Making match/unmatch an append-only event
log (the bills pattern) would preserve Law 2's purity but misreads the law:
Law 2 protects **financial history** — vouchers, entries, allocations,
imported statement facts. Which line matches which entry changes no
financial fact; it is the one place in the system where honest bookkeeping
*is* revision. The design therefore draws the boundary explicitly:
`bank_statement_lines` is append-only (trigger-guarded, exactly the
`vouchers` discipline); `bank_line_matches` is the kernel's **first
deliberately mutable table** — INSERT, UPDATE (re-match/un-match), never
DELETE, one row per statement line with `matched_at` / `unmatched_at`
provenance.

**Third — connected banking is a plugin concern (D3).** Per-bank APIs are
credentials, rate limits, proprietary payload shapes — the opposite of
kernel material. The kernel's job is the seam: an import service any
in-process caller may use, a REST route any authenticated client may call,
an inbound **webhook route** a plugin may claim (plugin API 0.11:
`register_webhook`), and after-commit events so bridges can react. A bank
adapter is a drop-in behind the ADR-016 trust gate; core never learns a
bank's name.

## Decision

### 1. Migration `0008_banking` — three tables

- **`bank_statement_batches`** — the import receipt: book, ledger, format,
  source label, opening/closing balance when the statement carried them
  (MT940 `:60F:`/`:62F:`; CSV callers may supply), line counts. Append-only.
- **`bank_statement_lines`** — the facts: `value_date`, `dc` (D = money out
  per the bank, C = money in — the same side the books would post: a
  deposit credits the bank ledger), exact `amount > 0`, `reference`,
  `narrative`, a parsed `cheque_no` when present, the batch, and a
  **content fingerprint**: `sha256(ledger, date, dc, amount, reference,
  narrative, occurrence-within-batch)` — re-importing the same statement
  reproduces every fingerprint and is a clean no-op; the second of two
  genuinely identical transactions carries the next occurrence and
  survives. `UNIQUE (book_id, fingerprint)` makes idempotency a database
  verdict, not a check-then-insert race. Append-only:
  `trg_forbid_mutation` (the Law 2 trigger family), INSERT-only privilege.
- **`bank_line_matches`** — the annotation: one row per statement line
  (`statement_line_id` primary key), pointing at the matched
  `voucher_entries` row **by validated reference, not by FK**: the entry
  id, the carrying `voucher_id` (a real FK — `vouchers` is never
  partitioned), and the entry's `voucher_date` denormalized, all proven by
  a BEFORE INSERT/UPDATE trigger that checks the entry exists with exactly
  that voucher and date, in the same book, on the statement's own ledger —
  shape-independent integrity (ADR-025 could not take this route because
  allocations needed the FK's deferrable rigour; annotations need
  validity, and the trigger gives it on both `voucher_entries` shapes
  without touching ADR-018's swap machinery). `unmatched_at NULL` = the
  live match; UPDATE-only provenance columns; no DELETE ever.
- Ledger scoping: a composite FK `(book_id, ledger_id)` → `ledgers` — a
  statement can only exist in the ledger's own book (the groups pattern).
  The service layer adds the accounting guard: the ledger must live under
  **Bank Accounts** or **Bank OD A/c** (the seeded groups), and the
  statement's currency, when the statement declares one (MT940), must be
  the book's base currency — an FX-account reconciliation needs
  `amount_fx` plumbing through the fold and is honestly recorded below as
  not in v1.

### 2. The import service — validation, then idempotent insert

`banking.import_statement` parses, validates, and (unless `dry_run`)
inserts in one transaction, returning a receipt: parsed line count,
duplicates skipped (by fingerprint), rejected rows with line numbers and
reasons. **All-or-nothing**: one bad row refuses the batch (the engine's
transactional discipline; `dry_run` is the preview). Two parsers, both
dependency-free:

- **CSV** — header-sniffed columns (date / value date; narrative /
  particulars / description; reference / chq no / utr; either one signed
  amount, or a withdrawal+deposit pair, or dr+cr), dates in ISO, `DD/MM/YYYY`,
  `DD-MM-YYYY` or `DD MMM YYYY`, amounts with thousand separators and
  ₹/Cr/Dr decorations stripped. Unknown or unparseable rows are rejects,
  never guesses.
- **MT940** — the SWIFT customer statement subset: `:20:`/`:25:`/`:28c:`
  header, `:60F:/M:` opening (D/C mark, date, currency, amount), `:61:`
  lines (value date, booking date, C/D/RC/RD mark, amount, funds code,
  reference) with `:86:` narrative attached, `:62F:/M:` closing. Reversal
  marks (`RC`/`RD`) import as the flipped direction. Opening/closing land
  on the batch.

### 3. The matching engine — a pure function, suggestions never commit

`suggestions(...)` computes, ranks, and returns; it writes nothing — the
roadmap's law, restated as architecture. Candidates: every **unmatched**
statement line × every **unmatched** book entry on the same ledger (an
entry is unmatched when no live match points at it). Score: exact amount
(+50; both directions carry D/C, so a match is side-equal by nature),
date-window (the entry's voucher date within ±N days of the line's value
date, default 7, caller-tunable; nearer ranks higher), and
reference-fuzzy: normalized token overlap between the line's
reference/narrative/cheque number and the entry's narration/voucher
number (cheque-number hits rank hardest). Ties break by date proximity,
then amount. The API returns them; the UIs show them; **only an explicit
match commits**, and a manual match need not be amount-exact — the user is
the boss, and the reconciliation statement prices any disagreement
honestly (below).

### 4. The reconciliation statement — a proof, not a display

`reconciliation(ledger, as_of, statement_balance=?)` folds exact Decimals:

- **book balance** = ΣCr − ΣDr of the bank ledger's entries ≤ as_of
  (positive = money in bank, negative = overdraft);
- **statement balance** = the caller's figure (the accountant reads it off
  the statement — the classic anchor) or, when the caller omits it, the
  fold from the earliest imported batch opening + Σ signed lines ≤ as_of;
- **unmatched book entries** (deposits not yet in the statement; cheques
  issued not yet presented), **unmatched statement lines** (credits in
  bank not in books; payments per bank not in books), and **matched-pair
  differences** (book figure − bank figure per pair, when a manual match
  disagrees);

and asserts the identity, exactly:

```
book_balance − statement_balance
    = Σ unmatched-book effects − Σ unmatched-statement effects + Σ pair differences
```

Every number a UI shows comes from this fold; the tests assert the
identity against independently computed sums (the trial-balance
discipline). The **reconciled balance** — Σ matched entry amounts, the
money both sides agree on — is surfaced alongside. Reconciliation is
read-only: matching may cross a locked period's border (it annotates; it
does not post — ADR-008's gates guard writes to financial history, and a
match is not one).

### 5. The cheque register — a fold, not a status column

Cheque declarations ride `vouchers.meta` (the kernel stays meta-blind;
the pack validates): `meta.banking.cheque = {no, bank_ledger, issued_on?,
matures_on?}` on payment/receipt vouchers. The validator enforces the
ledger exists in a bank group, `matures_on ≥` the voucher date when
present, and — under the resolver's advisory lock — that no other live
voucher of the same ledger declares the same number. The lifecycle is
derived, exactly like an order's position (ADR-024):

- **issued** — the voucher posted (a reversal pair kills the cheque with
  it; the bills fold's skip-undone rule);
- **presented** — an imported statement line of that ledger references
  the number (reference, parsed cheque field, or narrative): the bank has
  seen it;
- **cleared** — that line carries a live match: both sides agree;
- **PDC** — `matures_on` in the future as-of the report date, with the
  maturity shown; the register filters by status, ledger and date.

### 6. The seams (plugin API 0.11)

- **`register_webhook(key, handler)`** — an inbound HTTP hook a pack may
  claim: `POST /api/v1/hooks/{key}` (book-scoped by the standard header,
  **bookkeeper role** — webhooks write) runs `handler(resolver, payload)`
  in the request transaction and returns its dict. The banking pack claims
  `bank-statement-gateway` (generic gateway JSON: ledger, format, text,
  balances, source → the import service → the receipt). A per-bank adapter
  is a drop-in claiming its own key and translating its bank's payload;
  core never learns a bank's name (D3).
- **After-commit events** — `bank.statement.imported` (the receipt),
  `bank.line.matched`, `bank.line.unmatched`, `bank.cheque.…` flows derive
  from these; bridges subscribe through the ordinary handler registry
  (`voucher.posted`'s discipline: isolated, never breaking the request).
- **`Resolver.bank_lines(ledger_id)`** — the sanctioned read for pack
  folds (the `fy_start_month` precedent of API 0.4): every imported line
  of a bank ledger with its live match state and the matched voucher's
  number — exactly what the cheque register and the pack's reconciliation
  report need, nothing more.
- The **banking pack** (`contrib/banking.py`, entry-point like every
  pack) owns the meta layer: the cheque validator, the
  `banking-reconciliation` and `banking-cheques` pack reports (JSON for
  the UIs, PDF through the print bridge, CSV for machines — ADR-014), the
  gateway webhook handler, and a document view claiming `meta.banking`
  vouchers so a cheque-carrying payment prints with its cheque number,
  date and maturity in the header fields.

## Consequences

- **Migration discipline holds**: 0008's blocks are generated from
  `db/schema.sql` verbatim; the drift guard proves `alembic upgrade head` ≡
  bootstrap; the revision is independently reversible. No ADR-018
  interaction: none of the three tables references `voucher_entries` by
  FK (the validated-reference trigger is the shape-independent integrity
  provider), so the partition swap functions are untouched.
- **The append-only story gains an honest boundary**, stated as policy:
  financial history (vouchers, entries, allocations, statement facts,
  match-receipt provenance) is append-only and trigger-guarded; the match
  annotation is mutable by design, UPDATE-only, audited by its timestamps.
  A future ADR may promote matches to an event log if audit depth is ever
  demanded; the fold's inputs would not change.
- **Both surfaces at once**: the web app gains a Banking screen (Alt+B:
  import with preview, the line book with suggestions, match/unmatch
  keys, the reconciliation statement, the cheque register) and the native
  window a Banking page with the same moves — both consuming the same
  JSON folds, zero business logic in either UI (ADR-022's rule).
- **What this ADR does not do**: no per-bank connected-banking adapters in
  tree (D3 — drop-ins only, trust-gated); no FX-account reconciliation
  (statements must be in the book's base currency at import; the
  `amount_fx` fold is an additive follow-up); no N:1 / 1:N match groups
  (one line ↔ one entry; a composite deposit reconciles as
  multiple entries against multiple lines, matched pairwise); no
  auto-match-on-import (suggestions are computed on request, never
  persisted, never applied — the roadmap's law, kept structural); no
  intraday connected feeds. All additive, none blocking.
