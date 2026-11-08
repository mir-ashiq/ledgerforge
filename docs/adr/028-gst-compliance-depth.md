# ADR-028: GST compliance depth — the 2A/2B comparison, the 3B and 9 statements, and the e-way-bill outbox

- **Status:** Accepted (2026-11-08)
- **Deciders:** mir-ashiq
- **Supersedes:** —
- **Extends:** ADR-017 (the GST pack — this is its depth promotion, the one its own consequences reserved for a later release), ADR-026 (the external-facts precedent — statements got tables), ADR-023 (the meta-fold precedent), ADR-010 (the meta store — where document facts stay), ADR-003 (the plugin contract — plugin API 0.13), ADR-016 (drop-in trust — where portal adapters live)

## Context

ROADMAP.md §3 Phase 7 (v0.25.0, issue #13): the GSTR-2A/2B comparison with
an ITC reconciliation report, the GSTR-3B summary, GSTR-9 scaffolding, the
e-way-bill outbox on the IRN pattern, credit/debit-note returns handling
and multi-GSTIN registrations in one book. The capability ledger carried
GST as **partial** — TallyPrime ✔ (GSTR-1/2A/2B/3B/9, e-way, e-invoice),
LedgerForge holding GSTR-1, the HSN summary and the IRN outbox from
ADR-017. Three facts shaped the design.

**First — the portal's rows are facts, and facts need identity.** A
downloaded GSTR-2A/2B statement is a set of external facts the books must
remember exactly: the supplier's GSTIN, their document type, number and
date, the taxable value, the IGST/CGST/SGST split, the place of supply,
and the portal's own ITC-eligibility verdict. The reconciliation needs to
re-aggregate them with exact-decimal rigour, re-downloading the same
period must be a no-op, and two suppliers' look-alike documents must both
survive. That is the ADR-026 argument for bank statements, restated word
for word for the tax portal — `vouchers.meta` cannot promise it (the
download is not double-entry and Law 1 forbids inventing a voucher to
carry it). So Phase 7 is the **fourth core-schema change since 0006**,
landing as migration `0010_gst_2b` in the banking pack's exact shape: a
batch (the import receipt) and lines (the facts), INSERT-only,
trigger-guarded append-only, idempotent by content fingerprint.

The portal's own file format is deliberately **not** the storage format.
The kernel reads an **adapter contract** — `lf-gst-2a/1` / `lf-gst-2b/1`,
a small documented JSON — exactly as the IRP outbox speaks
`lf-gst-invoice/1`: translating the portal's versioned, deep schemas is
the operator's adapter's business (D3), never the kernel's. Books are
strangers (ADR-009): every row is book-scoped, and a line must ride its
own receipt's book — the bank-line trigger discipline.

One wrinkle is honest, not edited away: the portal **regenerates** a
period's 2B statement as suppliers file, so a re-download is not a
duplicate but a new snapshot. An unchanged re-download reproduces every
fingerprint (a clean no-op, no batch); a changed one lands as a new batch
beside the old — append-only history (Law 2) — and the fold reads each
registration's **latest** batch only. The portal's latest-state
semantics, replayed from stored facts; nothing already reconciled
against ever changes underneath anyone.

**Second — the books' side is already documents, and depth is more of the
same fold.** A credit note (a sales return) is the sale walked backwards:
the party credited at the grand total, the revenue debited, the tax
debited — the **same ledgers**, opposite sides, exactly TallyPrime's
Credit Note voucher. A debit note is the purchase walked backwards and
reverses the input credit. So the document mode grows two modes rather
than a second grammar, and every statement nets the notes with a sign
instead of a separate maths — the register names each document (INV/CN/DN)
and its totals net; the 3B's outward and ITC figures net; the HSN summary
nets its side's mass. The same reversal-netting discipline holds
throughout (a whole-voucher reversal still drops both sides).

Multi-GSTIN is the `supplier_state` rule applied to the registration
itself: a voucher may carry `supplier_gstin` (validated, persisted in the
computed block; `LF_GST_SUPPLIER_GSTIN` the deployment default), and every
statement accepts `?gstin=` to scope one registration of a multi-GSTIN
book. Place of supply was already per-voucher — the levy decision follows
the registration the voucher belongs to, and one book can now honestly
serve a company registered in several states.

The comparison's **match key** is the supplier's GSTIN plus their own
document number — which means the books must carry the supplier's number
on inward documents. `meta.gst.invoice_no` is that fact: optional,
persisted as posted, and the comparison counts (never guesses) inward
documents that lack it. This is the bills pack's bill-wise discipline
(ADR-023) recognised at the GST layer: the supplier's invoice number was
always the reconciliation key; now the tax pack sees it too.

The statements themselves are folds of the same registers, not new maths.
**GSTR-3B** summarises outward intra/inter (returns netted), the eligible
ITC from the inward register, and the net payable — the ledger's numbers
or no numbers. The heads this pack cannot capture (reverse-charge
imports, exempt inward supplies, ITC reversal at set-off) are reported as
blanks with footnotes that name them, never as zeros pretending to be
verdicts — ADR-017's "a blank is honest, an invented GSTIN is not",
applied to summary heads. **GSTR-9** is scaffolding in the same spirit:
twelve monthly folds plus the annual totals, with the portal-only tables
(declared-vs-actual turnover, the HSN summary, imports, amendments)
explicitly left for the portal's own records — this is the scaffolding,
not the filing.

**Third — movement integration is the IRN pattern again.** An e-way bill
is needed when goods move at or above Rule 138's line (₹50,000 —
`LF_GST_EWB_THRESHOLD` moves the deployment's own line). The pack queues
a movement envelope for every threshold-crossing document-mode voucher —
sales, purchases and the notes (goods move both ways, and returns move
too) — **after commit**, out of the posting path, to the
`LF_GST_EWB_OUTBOX` JSONL file. The `meta.gst.eway` block carries the
operator's Part-B facts (transporter, vehicle, from, distance) for the
adapter to fill or update; `policy:skip_ewb` opts a voucher out.
`lf-ewb-bridge` ships the outbox to the operator's NIC/ASP adapter in a
separate process and records the returned EWB number — 2xx ships, 4xx
parks, 5xx/network stays pending. Latency-tolerant integration stays
out-of-process by contract (D3) while the tax itself ran inside the
posting transaction where Law 1 lives.

## Decision

### 1. Migration `0010_gst_2b` — two fact tables

- **`gst_downloads`** — the import receipt: kind (`2a`/`2b`), the return
  period, the registrant GSTIN the statement belongs to, a source label,
  line count, import time. Append-only.
- **`gst_download_lines`** — the supplier rows: GSTIN, document type
  (INV/CN/DN), number, date, taxable value, the IGST/CGST/SGST split,
  place of supply, the portal's ITC-eligibility verdict, and a content
  fingerprint (statement identity + row facts + occurrence + import
  revision) UNIQUE per book. A batch-scoping trigger keeps a line on its
  own receipt's book; UPDATE/DELETE are forbidden by the append-only
  trigger — a corrected download is a new import, never an edit.
- The kernel never interprets a row's tax meaning (that is the pack's
  fold, served through plugin API 0.13) and never talks to the portal
  (that is the operator's adapter, D3).

### 2. The kernel seam — `gst_compliance.py` and plugin API 0.13

- `parse_download` validates the adapter contract all-or-nothing; one bad
  row names itself and refuses the import.
- `import_download` is idempotent by fingerprint and computes the import
  revision (this statement's snapshot number) that keeps re-downloads
  honest: unchanged rows count as duplicates; changed rows land as a new
  batch.
- `portal_lines(kind, period, gstin=None)` folds the **latest snapshot
  per registration** — the comparison's portal side, exact `Decimal`
  amounts as stored.
- REST: `POST /api/v1/gst/portal/import` (bookkeeper; `dry_run` the
  preview), `GET /api/v1/gst/portal/downloads` (the receipts), `GET
  /api/v1/gst/portal/lines` (the fold's rows). The after-commit event
  `gst.download.imported` fires through the plugin registry, isolated
  like `bank.statement.imported`.
- **Plugin API 0.13**: `Resolver.gst_portal_lines(kind, period,
  gstin=None)` — the banking `bank_lines` precedent (0.11): the pack's
  fold input as a versioned, session-free read returning frozen
  `PortalLine` snapshots. The pack never touches the session; the kernel
  never learns what a comparison is.

### 3. The pack depth — notes, registrations, and the match key

- Document modes: `sale`, `purchase`, `credit_note` (a sales return:
  party Cr, revenue Dr on *Sales Returns*, tax Dr on the same payable
  ledgers), `debit_note` (a purchase return: party Dr, purchase Cr on
  *Purchase Returns*, input tax Cr on the same Input ledgers). One shape,
  two sides — the mode table carries direction, sign, sides, groups and
  default ledgers; the computed block persists all of it (ADR-010).
- `supplier_gstin` (validated; document's word, else
  `LF_GST_SUPPLIER_GSTIN`) segments every statement; `?gstin=` scopes
  GSTR-1, the HSN summary, 3B, 9 and the comparison. `invoice_no` (alias
  `doc_no`) is the supplier's document number on inward documents — the
  comparison's key.
- The GSTR-1 register lists invoices **and** credit notes with a Type
  column; totals net the notes.

### 4. The statements — `gstr3b`, `gstr9`, `gst-recon`

- **`gstr3b`** (`?period=YYYY-MM[&gstin=]`): 3.1's outward heads
  intra/inter netted of returns; 4's eligible ITC from the inward
  register netted of debit notes; net payable = outward tax − ITC
  (negative means credit carried forward — footnoted, not floored). The
  not-captured heads print as named blanks. PDF, CSV and the JSON payload
  (plugin API 0.8) — one fold, three renderings.
- **`gstr9`** (`?period=YYYY`): the twelve monthly folds and the annual
  totals (Table 4's outward, Table 6's ITC, the year's net), the
  portal-only tables left as scaffolding. `period` wants a year here — a
  month-shaped period is refused loudly.
- **`gst-recon`** (`?period=YYYY-MM&kind=2b[&gstin=]`): the books'
  inward B2B register against the latest portal snapshot. Every document
  lands in exactly one bucket — **matched** (key and magnitudes agree),
  **mismatched** (the key agrees, the facts do not; the reason names
  taxable/tax/type exactly), **missing in books**, **missing in portal**.
  A books debit note pairs the supplier's credit note (or debit note) —
  the same physical document named from both ends — and the comparison
  prices **magnitudes** (books returns negate, the portal states face
  values). The ITC block sums both sides with the portal's own
  arithmetic (invoices and supplier debit notes add, credit notes
  subtract), prices the difference exactly, and counts the portal's
  ITC-ineligible rows. Inward documents without the supplier's GSTIN or
  number are counted unmatchable, never keyed by guesswork.

### 5. The e-way-bill outbox and bridge

- `LF_GST_EWB_OUTBOX` (JSONL, after-commit append, isolated failures) and
  `ewb_envelope`: kind `lf-gst-ewaybill/1`, the document facts, the
  counterparty, the `meta.gst.eway` Part-B block, the totals — the
  operator's adapter contract. Threshold `LF_GST_EWB_THRESHOLD` (default
  ₹50,000); `policy:skip_ewb` opts out; quick-tax vouchers never queue
  (no goods facts to carry).
- `lf-ewb-bridge` (`python -m ledgerforge.contrib.ewb_bridge`): the IRN
  bridge's exact skeleton — 2xx records `ewb_no`/`ewb_date`/`valid_upto`
  to `ewb-responses.jsonl`, 4xx parks in `<outbox>.failed.jsonl`, 5xx and
  network errors stay pending; all writes atomic.

### 6. The surfaces

- The native **GST page** (`Alt+6`, between Inventory and Reports): the
  registration picker, the 2A/2B import with a dry-run preview, the
  receipts table, the reconciliation table with every state and reason,
  the ITC summary line, and the statutory PDFs (1, 3B, 9, the
  comparison) one click away. The Reports page gains the 3B, 9 and
  comparison entries with PDF export.
- The web **GST screen** (`Alt+T`): the same workbench for the browser
  surface.
- Zero business logic in either GUI — every number is the engine's
  string (Law 3), every action one API call.

## Consequences

**The comparison's both sides are now first-class facts.** The books'
side was always replayable (ADR-017's discipline); the portal's side now
has the same guarantees — identity, idempotency, exact aggregation,
append-only history. An ITC figure that disagrees with either the ledger
or the downloaded statement is impossible, not merely tested-for.

**One fold serves four statements.** GSTR-1, the HSN summary, 3B and 9
render the same registers; the comparison reads the books' inward
register and the portal fold. A 3B that disagrees with the register is a
bug, not a rounding story.

**We accept, explicitly:**

- Reverse-charge imports, exempt inward supplies and ITC set-off are not
  modelled — they print as named blanks in the 3B, and the operator
  posts them explicitly when they apply. The follow-up (if the roadmap
  ever wants it) is a reverse-charge document mode, not a summary-head
  guess.
- The comparison cannot key inward documents that lack the supplier's
  GSTIN or `invoice_no`; they are counted and footnoted. Operators who
  want full auto-matching capture the supplier's number at purchase
  time — the bill-wise habit TallyPrime taught, arriving at the GST
  layer.
- GSTR-9's portal-only tables (declared-vs-actual turnover, the HSN
  summary, imports, amendments) remain scaffolding: fill them from the
  portal's own records. The ADR-017 footnotes (GSTIN checksums, UQC
  codes) stand unchanged.
- The portal download is an adapter contract, so an operator moving from
  one ASP to another re-translates once; the stored facts never move.
- The e-way-bill outbox queues on the document's grand total against the
  threshold — a deliberately coarse proxy for "consignment value". A
  shipment split across vouchers queues each; a service invoice above
  the line queues and the adapter declines it. `policy:skip_ewb` and the
  threshold env are the operator's adjustment knobs.

**The plugin API grows to 0.13** (`gst_portal_lines`) with the same
promise as every bump: old packs load unchanged, new reads are opt-in,
and the kernel stays both meta-blind and comparison-blind — it stores
and folds; the pack decides what a row means.
