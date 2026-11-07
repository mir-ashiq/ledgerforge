# LedgerForge 0.21.0 — order processing

**Phase 3 of the [v1.0 roadmap](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md):
the quote-to-cash and procure-to-pay chains, closed.** TallyPrime users
live in order documents — a quotation becomes a sales order, goods go out
on a delivery note, the invoice bills the order; the purchase side mirrors
it (purchase order → GRN → purchase bill). LedgerForge now runs both
chains as the fifth plugin pack, with zero database schema change.

## What's new in the window

- **The Orders page (Alt+3).** Every order document of the book —
  quotations, sales orders, delivery notes, purchase orders, goods receipt
  notes — with kind, party, the document's own ref, its value and the live
  position status (open / partial / closed) from the engine's fold. Filter
  by kind, search by party or ref, double-click to view (Save as PDF
  prints the commercial document), *Pending Orders…* jumps to the position
  report. *New Order…* (Ctrl+Shift+N) posts any of the five kinds.
- **Their own numbering.** Order documents number in their own gapless
  series — QT-…, SO-…, DN-…, PO-…, GRN-… — created lazily per book by the
  pack (plugin API 0.9). A refused document consumes no number.
- **Order… (Ctrl+O) on the voucher form.** When a voucher moves a party
  ledger, pick that party's open orders and type the quantity each line
  bills — the settlement rides the post as `meta.orders.settle`, and the
  order's position updates. An invoice can settle an order; a purchase
  voucher bills a PO; bill-wise allocations and order settlements share
  one voucher.
- **New reports:** *Pending Orders (sales)* and *Pending Orders
  (purchase)* — quantities ordered, delivered/received, invoiced/billed,
  pending quantity and value, per-party subtotals, statuses; export CSV,
  save the PDF, same as every report.

## What's new underneath

- **Orders are not accounting — and the books prove it.** Every order
  posts a **zero-net memorandum trace** (one Dr + one Cr on "Order Book
  (memo)"): the day book shows the order at its value, the party ledger is
  untouched, and the memo ledger's trial-balance figure is exactly zero by
  construction. The tests assert it after a full order chain.
- **The position fold** (ADR-024): sales order lines track delivered (from
  delivery notes) and invoiced (from settle allocations); purchase order
  lines track received (GRNs) and billed. Status runs open → partial →
  closed; the guards are the caps — nothing fulfils or bills beyond the
  ordered quantity, per item, per order, same party. Reversals net in the
  fold: reversing an order cancels it, reversing a delivery note or a
  settling invoice restores the position.
- **Plugin API 0.9** (additive — every existing pack keeps loading):
  `Resolver.ensure_voucher_type()` — the `ensure_ledger` pattern applied
  to document numbering; the engine resolves a draft's voucher type after
  transformers, so a pack can put its documents on their own series.
- **The settle annotation** resolves the order among the parties the
  voucher actually moves (the ADR-023 discipline) and writes the
  resolution back into the stored document — the fold replays it without
  guessing, and `GET /vouchers/{id}` shows exactly which order was billed.

## The plan, on the record

Eleven phases to v1.0 — **"everything an Indian SMB's daily accounting
needs, on LedgerForge, without wishing for TallyPrime"** — tracked as
[ROADMAP.md](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md)
with one GitHub milestone per phase. Phase 1 (the completeness pass) landed
in v0.19.0, Phase 2 (bill-wise & outstanding) in v0.20.0, Phase 3 (order
processing) in this release; Phase 4 is **cost centres & cost categories**.

## What's underneath (unchanged, and that's the point)

- Embedded PostgreSQL, append-only history, exact decimals — every law
  enforced at the application and database layers. The native window is an
  API client: zero business logic in the GUI, one engine for web, Docker
  and desktop.
- Your data stays where it was: `%LOCALAPPDATA%\LedgerForge` (or your
  `--data-dir`). v0.20 books open in v0.21 unchanged — zero schema change
  this release; the same migrate path the smoke test proves on every build.
- The engine runs in its own process (ADR-022): the window cannot crash
  your books, and the books cannot crash the window.
