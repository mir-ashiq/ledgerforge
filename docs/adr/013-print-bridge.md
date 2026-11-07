# ADR-013: The print bridge — document views, one layout engine, a dependency-free PDF writer

- **Status:** Accepted (2026-11-07)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

Three packs now persist business documents on the voucher store (invoices ADR-010, stock ADR-011, payroll ADR-012), and the payroll pack's own docstring names its after-commit seam as "the spot a payslip PDF generator or a PF filing bridge subscribes to". TallyPrime's contract includes the physical artifact: the printed voucher, the tax invoice, the payslip. The web app reserved `Ctrl+P` → `report.print` in its shipped keymap from day one (keyboard-ux §3) — with nothing behind it.

The questions this ADR answers:

1. **Who owns a document's print layout?** The payslip's shape is payroll knowledge (components, YTD windows, payable balance); the tax invoice's is invoicing knowledge (items, GST slabs, due dates). But pagination, column geometry and page furniture are nobody's business knowledge — they should be shared.
2. **What draws the bytes?** Pulling in a PDF library (ReportLab, WeasyPrint) adds a heavy dependency to a kernel whose runtime deps are nine, for a document that is text and rules — no vector art, no embedded images.
3. **What prints when no pack claims the voucher?** A plain payment, a reversal, a payout — these are still documents an accountant prints and files.

## Decision

### 1. Plugin API 0.5 — `register_document_view(key, factory)`

Packs register a factory for their meta key:

- **`DocumentContext`** — what a factory may read: the voucher's immutable fields, its entries (as flat `EntryLine`s), the book's name, the caller's query params, and a book-scoped `Resolver` for derived reads (the payslip replays `payable()` through it — a payslip that disagrees with the ledger is impossible by construction).
- **`DocumentView`** — what a factory returns: *what* the document says (title, fields, sections of columns+rows, totals, footnotes, a filename hint), never *where it lands on the page*.
- **`None` declines** — the payroll factory declines payouts (they print as voucher slips); an unregistered key falls through the same way. First non-`None` view wins; meta keys are offered in posted order.

`api_compat` remains a floor (ADR-011): 0.2/0.3/0.4-era packs load on a 0.5 build untouched; the payroll and invoicing packs declare `0.5` and are the live proof.

### 2. One layout engine, one visual language (`reporting/printing.py`)

The engine consumes any `DocumentView`: header block (title, reference, subtitle), two-column fields, sections with right-aligned money columns and repeated column headers across page breaks (a row never splits), a right-aligned totals block (last row bold), footnotes, and page furniture (`Page i of n`). Every pack's documents share it — a payslip and a tax invoice obviously belong to the same system, and a plugin author writes data, not geometry.

### 3. A dependency-free PDF 1.4 writer

Base-14 fonts only (Helvetica/Helvetica-Bold, `WinAnsiEncoding`) — they live in every viewer, so there is nothing to embed and nothing to install. Width metrics for ASCII come from the public Adobe AFM tables (right-alignment, ellipsized truncation). Content streams are uncompressed: a payslip's amounts are greppable in the bytes, which `tests/test_printing.py` leans on. Glyphs outside cp1252 (₹) degrade to `?` — the layouts never need them (amounts are digits; the currency is the book's code in the header). Money is formatted once at the edge (`format_amount`, exact `Decimal`, never `-0.00`); the writer draws strings only (Law 3).

### 4. The endpoint and the fallback

`GET /api/v1/vouchers/{id}/pdf` (reader role, book-scoped, `?employee=` for payroll runs) serves `application/pdf` inline with a sanitized filename. Dispatch offers the voucher's meta keys to the registry; anything unclaimed — plain vouchers, mystery meta keys, reversals, payouts — prints as the **voucher slip**: the Dr/Cr table with narration, equal totals (Law 1), and the Law 2 note. Every voucher is printable; nothing 404s for lack of a layout.

### 5. The payslip screen (web)

`Alt+P` opens it: the posted voucher list *is* the run index (`meta.payroll.mode === 'run'`), pick employee → period, preview the computed figures (strings, never client arithmetic — Law 3), `Ctrl+P` fetches the PDF and opens it in a new tab for print/save; `Esc` is back, the universal key. Reprints are identical: posted entries are immutable (Law 2).

## Consequences

- **(+)** The kernel stays meta-blind: core knows the *protocol* (view factories, layout engine, endpoint), packs know the documents. A community pack can ship a printable document (a delivery note, a rent receipt) by registering one function — no core change, same visual language.
- **(+)** Zero new runtime dependencies; the wheel stays embeddable in small containers, and CI needs no system font/library packages.
- **(+)** The two flagship layouts (payslip, tax invoice) landed as ~100 lines each inside their packs, proving the API sufficient — the same proof discipline as ADR-011/012.
- **(−)** The writer draws text and rules only — no charts, no logos, no barcode on the PDF. Those arrive (if ever) as a real dependency behind the same `DocumentView`, not by growing this writer.
- **(−)** WinAnsi degradation means a non-Latin employee name prints with `?` marks. Unmappable names deserve a loud future (transliteration at the pack, or CID fonts in the writer); today the guardrail is the encoding boundary, and amounts — the legally load-bearing glyphs — are pure ASCII.

Zero schema change: `alembic` head remains `0005_voucher_meta`. Core: `src/ledgerforge/reporting/printing.py` (writer, layout, slip, dispatch) + the route in `api/vouchers.py`. Packs: `payslip_view` (payroll), `invoice_view` (invoicing). Web: `PayslipScreen.tsx` + `payslip/model.ts`. Exercised by `tests/test_printing.py` (16 tests: writer structure and escaping, xref integrity, money formatting, slip fallback incl. mystery keys and reversals, payslip content/YTD/payable, employee-param guards, tax invoice totals and due date, payout-as-slip, book isolation, pagination) and `frontend/web/tests/payslipScreen.spec.tsx` (run index, Alt+P/Esc contract, Ctrl+P print flow, employee/period switching, blocked-tab error).
