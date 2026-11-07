# ADR-017: The GST compliance pack — place of supply, HSN rate tables, GSTR statements and the IRN outbox

- **Status:** Accepted (2026-11-07)
- **Deciders:** mir-ashiq
- **Supersedes:** — (grows the worked example of ADR-003 into a pack)

## Context

Issue #6 — the repository's one open issue — asked for the example GST
plugin (`contrib/example_gst.py`, a 100-line teaching demonstration of the
transformer/validator/event contracts) to be extended into a real
localization plugin for India: place-of-supply logic (intra vs inter
state), HSN-wise rate tables with effective dates, GSTR-ready reports, and
an out-of-process e-invoicing (IRN) bridge on `voucher.posted`.

The constraints the architecture had already established:

1. **Tax runs inside the posting transaction** (D3): the levy decision and
   the tax lines are part of the voucher's accounting, so they must be
   computed by an in-process transformer under Law 1.
2. **Latency-tolerant integration stays out-of-process** (also D3): an IRP
   round-trip is a network call to a government portal through an adapter —
   it can never sit in the posting path.
3. **Packs compute, never invent** (ADR-014): rates, state codes and
   notification history are parameters and posted facts, never kernel
   knowledge and never fabricated.
4. **Zero schema change** (ADR-011/012/014 precedent): everything rides on
   `vouchers.meta` (ADR-010), `prior_documents` folds (ADR-011) and pack
   reports (ADR-014).

## Decision

### 1. `contrib/gst.py` — the fifth business pack, replacing the example's entry-point

The pack claims `meta.gst` in two shapes:

- **Document mode** — `mode: sale|purchase`, `party`, `party_gstin`,
  `supplier_state`, `place_of_supply`, `items[]` (description, hsn, qty,
  rate, optional explicit `tax_rate`, optional ledger overrides). The
  transformer derives the TallyPrime shape (party line at the grand total,
  one line per item on its revenue/purchase ledger, tax lines under
  *Duties & Taxes*: `CGST/SGST/IGST Payable` outward, `Input CGST/SGST/IGST`
  inward) and annotates the decision into `meta.gst.computed` — supply
  kind, states, GSTIN, per-tax totals — persisted with the voucher
  (ADR-010), so statements replay posted facts instead of re-deciding them.
- **Quick-tax (legacy)** — `{"rate": "18"}`: the example's contract, kept
  byte-compatible, now honoring `place_of_supply` (intra splits CGST+SGST,
  inter levies IGST). The rate is whitelisted to the statutory slabs
  (0/5/12/18/28) — a fat-fingered 15% is a veto, not a silent wrong tax.

`example_gst.py` remains in-repo as the plugin guide's teaching module —
importable, loadable in a standalone manager — but the entry-point swaps to
`gst`: both transformers claim the same meta key, so only one may load in a
running server (loading both would double-tax a quick-tax voucher; the
duplicate-name refusal at boot already enforces this).

### 2. Place of supply is a code-equality decision

The GST state codes (the first two digits of a GSTIN) are shipped as the
standard table; states parse as `29`, `29-Karnataka` or a name. The levy:
`supplier_state == place_of_supply` ⇒ intra (CGST + SGST, the SGST line the
exact remainder), else IGST. Code equality handles union territories
correctly. The supplier's state is a **compliance fact with one home**: the
document's `supplier_state` or `LF_GST_SUPPLIER_STATE` — a POS without a
supplier state is refused loudly, because the levy is undecidable.
GSTINs are format-checked (15 chars, state-code prefix, PAN shape, 'Z');
the mod-36 checksum stays the portal's to verify — printed as posted,
never invented.

### 3. HSN rate tables are notification history, resolved by calendar

`HsnRate(hsn, description, rate, effective_from, effective_to)` rows;
items match by HSN prefix (the HSN hierarchy), rows match when
`effective_from ≤ date ≤ effective_to` (inclusive close), the most recent
`effective_from` wins, and two different slabs equally in force is a
broken table — refused, never guessed. An explicit `tax_rate` is
slab-whitelisted and must agree with the table; a disagreement is a veto.
The shipped seed is five deliberately illustrative rows of public history
(milk 0%, palm oil 5%, books 0%, iron/steel articles 18%, and the famous
8517 revision — mobile phones 12% until 2020-03-31, 18% from 2020-04-01).
Operators load their own notification history from `LF_GST_HSN_JSON`,
read at posting time — a dropped file takes effect on the next voucher,
no restart (rates change by notification, not by code; the ADR-014 rule).

### 4. GSTR-ready statements are pack reports (plugin API 0.6)

- `GET /api/v1/reports/pack/gstr1?period=YYYY-MM` — the outward **B2B
  invoice register**: GSTIN-wise rows with POS, slabs, taxable value and
  the CGST/SGST/IGST split.
- `GET /api/v1/reports/pack/gst-hsn?period=YYYY-MM&side=outward|inward|both`
  — the **HSN summary**: per HSN × rate, quantity and value, outward
  and/or inward.

Both fold `prior_documents("gst")` with whole-voucher reversal netting —
the same discipline as the payroll and statutory folds — and replay the
items with the slab resolved *at posting time* (posted on the item), so a
statement figure that disagrees with the ledger is impossible, not merely
tested-for. Purchases never appear in the outward register; B2C sales
appear in the HSN summary but not the B2B register; quick-tax vouchers
(no HSN/party facts) are excluded from both and **counted in a footnote** —
a blank is honest, an invented GSTIN is not. `?format=csv` serves the
machine format; the default renders the shared-layout PDF with the applied
rules in the footer. UQC codes are not captured and the statement says so:
a reconciliation aid, not a portal-certified upload file.

### 5. The e-invoicing bridge is an outbox + a separate process

The in-pack half subscribes to `voucher.posted`: every document-mode sale
carrying a `party_gstin` (B2B) is appended — after commit — as an
`lf-gst-invoice/1` envelope to the JSONL outbox named by `LF_GST_OUTBOX`.
That is the **only** hand-off: no network I/O ever runs inside the posting
path, and an unwritable outbox is logged and isolated (an event handler
must never break posting). `policy:skip_irn` opts a voucher out.

The out-of-process half is `lf-irn-bridge`
(`python -m ledgerforge.contrib.irn_bridge`): its own program, its own
loop. Per pending line: **2xx** → record the adapter's answer (IRN, ack
number/date, signed invoice/QR) in `irn-responses.jsonl` and remove the
line; **4xx** → park the line in `<outbox>.failed.jsonl` with the error
(retrying a rejected payload unchanged is spamming, not shipping);
**5xx/network** → leave pending, retry next pass. All writes are atomic
(temp + rename): a crash mid-ship loses nothing — a shipped line is either
still pending (adapters deduplicate by document number) or acknowledged.
The envelope is the operator's adapter contract, not the government
schema — the ASP adapter translates to the portal's format; claiming
schema-compliance with a moving portal would be the ECR-drift trap ADR-014
named.

## Consequences

- **(+)** Issue #6 closed: a full localization pack with zero schema
  change and zero kernel change — the plugin API's second birthday present
  (a transformer, validators, events, and two pack reports, all from the
  0.1–0.6 surfaces).
- **(+)** The example→pack graduation proves the documented contracts were
  enough: `example_gst.py`'s three hooks became a compliance pack without
  touching the runtime.
- **(+)** The IRN bridge demonstrates the D3 split end-to-end: tax inside
  the transaction, the portal outside the process.
- **(−)** The seed HSN table is illustrative; a real deployment loads its
  notification history (one JSON file) or posts explicit slabs.
- **(−)** Quick-tax vouchers will never appear in GSTR statements — they
  carry no HSN/party facts. Operators needing them in the register post
  document-mode vouchers.
- **(−)** The entry-point swap means an operator running both the example
  and the pack (e.g. an old drop-in) gets the second one refused at boot —
  loudly, by design.

Zero schema change. Core: none (plugin API stays 0.6). Packs:
`contrib/gst.py` + `contrib/irn_bridge.py` (entry-point `gst`, console
script `lf-irn-bridge`); `example_gst.py` demoted to teaching module.
Web: none in this release. Exercised by `tests/test_gst_plugin.py`
(22 tests: intra/inter splits, quick-tax POS, effective dates at the
voucher date, slab disagreements, operator tables, GSTIN/state guards,
GSTR-1 CSV+PDF, HSN sides, reversal netting, nil months, book isolation,
outbox gates/isolation, bridge ship/park/retry) and the updated
`test_plugins.py` / `test_plugin_trust.py` / `test_hierarchy.py` (the
entry-point swap and multi-book discipline).
