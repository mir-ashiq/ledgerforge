# ADR-030: Data exchange & the Tally migration path — imports as transactions, exports as a format everywhere, analysts as named read-only roles

- **Status:** Accepted (2026-11-08)
- **Deciders:** mir-ashiq
- **Supersedes:** —
- **Extends:** ADR-002 (the ACID posting engine — the import walks it, never bypasses it), ADR-026 (the import-transaction precedent: previewable, validated, idempotent, all-or-nothing), ADR-027/028/029 (the fact-table discipline the CSV stock and bank kinds ride), ADR-009 (books are strangers — every import is book-scoped), ADR-010 (documents ride `vouchers.meta` — the Tally stock masters stay masters, not documents), ADR-001/ADR-003 (the plugin contract — unchanged, no plugin API bump), ADR-018 (`rpt` schema precedent for the `views` schema), ADR-022 (the native window carries the workbench)

## Context

ROADMAP.md §3 Phase 9 (v0.27.0, issue #15): CSV/Excel import (masters, opening
balances, vouchers, stock, bank statements), export to CSV/Excel on every
report and list surface, the TallyPrime XML import with a trial-balance
verification report, read-only SQL views + ODBC for analysts, and personal API
tokens. The capability ledger carries data exchange as the **gap**: TallyPrime
✔ Excel/CSV/TDL/XML, LedgerForge a JSON REST API only. The adoption lever is
the migration path — *you can move off TallyPrime onto LedgerForge* — and four
facts shaped the design.

**First — an import is a transaction, and Law 1 has no bulk lane.** The
temptation every migration tool succumbs to is the bulk INSERT; the engine
forbids it. A voucher that arrives by file walks the exact path a keyboard
voucher walks: plugin transformers, Law 1 exact-Decimal balance, the FX rules,
the cost-allocation law, the period gate, gapless numbering under the counter
lock — then the deferred trigger re-proves balance at commit. So the import
service runs on **one outer transaction**: every `post_voucher()` call inside
it degrades its commit to a savepoint release (SQLAlchemy's
`join_transaction_mode="create_savepoint"`), and one bad row rolls the whole
file back — the bank-statement all-or-nothing discipline (ADR-026), restated
for the accounting side. Masters create-if-absent by name; vouchers carry
client tokens derived from the file's content hash, so a re-import replays as
no-ops — the engine's own idempotency seam, not a new one.

**Second — the Tally migration is a verification problem.** TallyPrime's XML
export (`ENVELOPE/BODY/IMPORTDATA/REQUESTDATA/TALLYMESSAGE`) is a dialect:
`GROUP`/`LEDGER`/`STOCKITEM`/`UNIT` masters, `VOUCHER` documents whose
`ALLLEDGERENTRIES.LIST` amounts are signed (negative = debit,
`ISDEEMEDPOSITIVE`), dates as `YYYYMMDD`, and group names that are — by
v0.1's seeding decision — **already LedgerForge's own group names** (the 15
TallyPrime primary groups and 13 sub-groups were planted in `seeding.py`
precisely so this day would be boring). Voucher types map onto the seeded
codes (Receipt→RCT, Payment→PYT, Journal→JV, Sales→SAL, Purchase→PUR, Credit
Note→CRN, Debit Note→DBN, Contra→CN; a caller may extend the map; an
unmapped type is **skipped and reported, never guessed**). The verification
report is the honest proof: a **source trial balance folded directly from the
XML** (ledger openings + voucher lines, pure arithmetic, no database) against
the **imported trial balance folded from the engine's books**, per ledger,
difference column, both totals asserted Dr = Cr, every skipped voucher and
unmapped name listed. An operator signs the migration when the differences
column is zero.

**Third — analysts get a contract, not a role on the base tables.** The SQL
surface is a dedicated `views` schema — flat, joinable, stable:
`views.books`, `views.groups`, `views.ledgers`, `views.vouchers`,
`views.voucher_lines`, `views.ledger_monthly`, `views.ledger_balances` — with
a NOLOGIN role `ledgerforge_readonly` holding SELECT on the views and nothing
else (the `ledgerforge_app` precedent, inverted: one role reads a projection,
not the store). ODBC is PostgreSQL's own wire (psqlODBC); the desktop's
answer is `POST /api/v1/ops/analyst-access` (admin), which mints or rotates a
LOGIN role with a server-generated password — returned once, stored only as
PostgreSQL's SCRAM verifier — and answers with the connection parameters the
ODBC dialog wants. DELETE drops the login. The views are cross-book by
construction (the analyst's pivot tables don't know books); an operator who
doesn't want that simply never mints the login.

**Fourth — personal API tokens are the desktop's non-OIDC answer.** A
desktop deployment has no IdP, but integrations (a sync script, a bridge, a
companion) still need a credential that is not a person. The table
`api_tokens` (migration 0012) stores a SHA-256 hash — the raw token
`lft_…` is returned exactly once at creation — with a role ceiling (a
principal may never mint above its own level), a books scope that reuses the
`books`-claim semantics verbatim (`["*"]`, a list, or absent = every book),
an optional expiry, `last_used_at`, and `revoked_at`. The verifier chains in
`auth_context` by prefix: bearers starting `lft_` verify against the
database (one indexed lookup, one cheap touch-write); everything else goes to
OIDC. Revocation is the next request's 401. In `disabled` mode the chain is
bypassed as ever — dev only.

## Decision

### 1. Migration `0012_data_exchange` — tokens, views, and the analyst role

- `api_tokens` — id, name, token_prefix (8 chars, identification),
  token_hash (SHA-256 hex, UNIQUE), role, books (JSONB: `["*"]` or ids),
  expires_at NULLABLE, created_by, created_at, last_used_at NULLABLE,
  revoked_at NULLABLE. Book-agnostic by design: a token scopes books
  through its `books` value, the claim discipline, not through row scoping.
- The `views` schema: seven read-only projections over the live tables
  (books, groups, ledgers, vouchers, voucher_lines with cost-centre names,
  ledger_monthly by month, ledger_balances all-time closing). Views carry no
  secrets: `vouchers` exposes narration and numbers, never `meta` internals
  or client tokens.
- `CREATE ROLE ledgerforge_readonly NOLOGIN` + `GRANT USAGE ON SCHEMA
  views` + `GRANT SELECT ON ALL TABLES IN SCHEMA views`, in the guarded
  DO-block shape `ledgerforge_app` already uses (idempotent re-runs,
  non-superuser deployments log and continue — the DB owner can grant later).
- `ledgerforge_app` gains SELECT on `api_tokens` + INSERT/UPDATE (mint,
  touch, revoke — the app role's own discipline: financial history stays
  INSERT-only, tokens are mutable).

### 2. `ledgerforge/dataexchange.py` — the import pipeline (kernel service)

- `decode_tabular(fmt, payload)` — `csv` (text, the first row headers,
  header names normalized case/space-insensitively the bank parser's way)
  or `xlsx` (base64, first sheet, first row headers). Rows become dicts of
  strings; the kernel never guesses types — every field parser is explicit
  and names its row on failure.
- **`masters`** — rows: `kind` (group|ledger), `name`, `parent`, `nature`
  (groups: one of the five natures; absent → inherit the parent's),
  `gstin`, `currency`, `address` (ledgers). Create-if-absent by
  `(book, name)`; an existing name is reported `existing`, never mutated —
  an import never edits masters behind the operator's back.
- **`openings`** — rows: `ledger`, `dc` (D|C), `amount`, and one `date` per
  file (`YYYY-MM-DD`). All rows post as **one Opening Journal** (type OPEN,
  `post_opening`): Law 1 applies to openings exactly (an unbalanced file is
  a 422 naming the difference; `balance_to: "Suspense A/c"` absorbs a
  residual explicitly, never silently).
- **`vouchers`** — rows grouped by `ref` into vouchers: `ref`, `date`,
  `type`, `narration`, `ledger`, `dc`, `amount`, `line_narration`,
  `currency`/`amount_fx`/`fx_rate` (the FX trio, all-or-none, the engine's
  own rule). Each voucher posts through `post_voucher` — transformers,
  gates, numbering, everything; a group that does not balance is a 422
  naming the ref and the difference.
- **`stock`** — stock item masters (core tables, ADR-027): `name`, `unit`,
  `track_batches`/`track_serials` (yes/no), `reorder_level`,
  `reorder_quantity`, `notes`. Create-if-absent by `(book, name)`. Stock
  **quantities** are pack folds, not rows — the ADR-027 law that nothing is
  stored that can be recomputed; positions arrive through documents, never
  through a file.
- **`bank`** — routes to `banking.import_statement` (ADR-026's own
  machinery, CSV/MT940) so every importable kind has one front door.
- Every kind: `dry_run` is the preview (parse + validate + map, write
  nothing, report per-row problems); the commit is one transaction;
  the receipt reports created/existing/skipped/unchanged counts, and
  `POST /api/v1/dataexchange/imports` (bookkeeper) serves it. Books are
  strangers (ADR-009): the whole import lands in one book.

### 3. `ledgerforge/exporting.py` — one format seam, every surface

- `rows_to_csv(columns, rows)` and `rows_to_xlsx(columns, rows)` — openpyxl
  arrives as a core dependency (pure Python, ~1 MB into the desktop bundle);
  legacy `.xls` is not read — xlsx is the only Excel dialect (TallyPrime and
  every modern spreadsheet writes it).
- `?format=csv|xlsx` on every report and list GET: trial balance, P&L,
  balance sheet, FY close, cost-centre summary/breakup, cost categories,
  the day book (`/vouchers`), groups, ledgers, cost categories/centres,
  currencies, FX rates, books, periods, bank ledgers/lines/entries/
  reconciliation, GST downloads/lines, attendance, inventory items/prices/
  BOMs. Pack reports (`/reports/pack/{key}`) gain `xlsx` beside their CSV.
  Column sets are declared beside each route (pydantic list routes derive
  theirs from the model), amounts render as plain decimal strings — the
  spreadsheet is the summer, the engine stays the source (Law 3).
- The response carries `Content-Disposition` with a download-safe stem, the
  banking precedent.

### 4. `ledgerforge/tally_import.py` — the TallyPrime XML import

- Parse with stdlib ElementTree; tolerate the dialect's spaces in tags
  (`OPENING BALANCE`, `PARTY PANTYPE`) and UDF namespace noise.
- Masters: `GROUP` → group (name, parent mapped onto the seeded tree; an
  unknown parent is created as a plain group under the mapped root — Tally's
  tree is deeper than the 15 primaries), `LEDGER` → ledger (parent group,
  `CURRENCY`, opening from `OPENINGBALANCE` signed negative = debit,
  `ISDEEMEDPOSITIVE` the tiebreak; Tally's GSTIN rides its own export
  sections — ledgers do not carry it in this release), `STOCKITEM`/`UNIT` → stock item masters (unit text,
  batch/serial toggles from Tally's `MTCATEGORY`/`HASEXPIRYDATE`-style flags
  where present, conservatively off). Stock **quantities** are not
  auto-booked — the verification report lists them as re-key guidance
  (positions are documents, ADR-027; a file that invented valuation rates
  would violate Law 3's honesty, not just its arithmetic).
- Vouchers: `VOUCHER VCHTYPE` → the seeded code (the map above, extendable
  by payload `type_map`); `DATE` `YYYYMMDD` → date; `ALLLEDGERENTRIES.LIST`
  → lines (`LEDGERNAME`, negative amount = debit, `ISDEEMEDPOSITIVE` the
  tiebreak; `LEDGERNAME` = "Suspense A/c" maps to the seeded suspense
  ledger); the FX trio when Tally carried `AMOUNT` in a foreign currency
  with `EXCHANGERATE`; missing ledgers are **created on the fly under
  Suspense A/c and reported** (Tally exports reference ledgers the master
  section omits — a migration that dies on the tenth voucher helps nobody;
  the verification report names every one). `PARTYLEDGERNAME` on inventory
  vouchers without entries is reported, not guessed.
- The whole import runs through the §2 pipeline's transaction discipline
  (masters create-if-absent; vouchers content-tokened — re-import is a
  no-op; `dry_run` previews). Receipt: counts + problems + **the
  verification report**: source TB (from the XML) vs imported TB (from the
  engine, as-of the import's last date), per-ledger difference, totals
  asserted, skipped vouchers listed with reasons. Served at
  `POST /api/v1/dataexchange/tally` (bookkeeper); the report re-renders
  on demand via `?verification=only`.

### 5. The tokens API and the analyst seam

- `POST /api/v1/tokens` (admin) — `{name, role, books?, expires_at?}` →
  the raw token once; role ceiling enforced (a bookkeeper admin's token is
  refused); `GET /api/v1/tokens` lists (prefix, role, books, last_used,
  revoked); `DELETE /api/v1/tokens/{id}` revokes (soft — the row is
  history, `revoked_at` is the verdict).
- `PersonalTokenVerifier` chains in `auth_context` by the `lft_` prefix;
  verification touches `last_used_at` (a single UPDATE — usage is
  observability, not audit; the audit log stays financial).
- `POST /api/v1/ops/analyst-access` (admin) — mint/rotate `lf_analyst`
  (password 32 urlsafe chars, returned once; `ALTER ROLE … LOGIN PASSWORD`,
  SCRAM-stored by PostgreSQL itself), response carries host/port/dbname for
  the ODBC dialog; `DELETE /api/v1/ops/analyst-access` drops the login.
  The role names are fixed (`lf_analyst`) — one analyst seat, by design:
  per-analyst seats are the DB owner's `GRANT` away, not this phase's UI.

### 6. The window surface

The native window's **Data Exchange page** (after Payroll, `Alt+8`): the
import pane (kind picker, file picker, preview table, Import button, the
receipt), the Tally pane (XML picker, preview with problems, Import, the
verification report table with the difference column), and the export pane
(every report/list surface, CSV or XLSX, saved through the native file
dialog). Pages after it shift one key down (Reports `Alt+9`, Periods
`Alt+0`, Sandbox keeps its place at the keyboard's wrap-around). The smoke
gate gains one proof: an export answers with the right content type.

## Consequences

- **(+)** The adoption lever ships: a TallyPrime shop's masters, opening
  balances, vouchers (FX included) and stock item masters move over in one
  previewable, idempotent transaction, and the verification report proves
  the imported trial balance equals the source — or names every difference.
- **(+)** Law 1 survives its fifth temptation: there is no bulk lane, and
  the import transaction makes "half a file" impossible — the engine's
  answer to ETL is the engine.
- **(+)** Every report and list surface speaks CSV and XLSX through one
  seam, and analysts get stable SQL views + a named, revocable login —
  read-only by construction, cross-book by declaration.
- **(+)** Personal tokens give integrations a scoped, revocable credential
  without an IdP — the desktop's own answer, one table and one prefix.
- **(−)** openpyxl joins the core dependencies (~1 MB into the Windows
  bundle; pure Python). Legacy `.xls` is out — xlsx only, the modern
  dialect.
- **(−)** The Tally import carries the accounting core; Tally orders, cost
  categories and payroll vouchers have LedgerForge homes (ADR-024/025/029)
  but no XML mapping yet — they are skipped and reported, deliberately:
  a half-mapped document is worse than a named gap. Per-item stock opening
  quantities are listed for re-keying, not auto-valued — inventing rates
  would be the slab computation ADR-014 refused.
- **(−)** The analyst views are cross-book and whole-book: no per-column
  security, no book filter. An operator who needs finer seams keeps ODBC
  un-minted and uses the API's book scoping instead.
- **(−)** Token passwords and analyst passwords are shown exactly once;
  there is no recovery path but rotation. That is the security posture, not
  an oversight — the alternative is storing them.

Shipped with migration `0012_data_exchange` (alembic head moves past
`0011_payroll_depth`). The kernel services are `ledgerforge/dataexchange.py`
(imports + tabular codecs), `ledgerforge/exporting.py` (the format seam) and
`ledgerforge/tally_import.py` (the dialect + the verification report); the
API lives at `api/dataexchange.py`, `api/tokens.py` and the ops analyst
seam in `api/ops.py`; the window surface is `nativeapp/dataexchange.py`.
Exercised by `tests/test_dataexchange_import.py` (decode, every kind,
idempotency, all-or-nothing, balance_to, books-are-strangers, role gates),
`tests/test_exporting.py` (every surface, both formats, exact rows),
`tests/test_tally_import.py` (the dialect, the map, missing-ledger
autocreation, the verification report's zero-difference and named-gap
cases, FX, idempotent re-import), `tests/test_api_tokens.py` (mint/list/
revoke, ceiling, book scoping, expiry, the chained verifier) and
`tests/test_sql_views.py` (views, grants, the analyst seat) plus
`tests/test_native_dataexchange.py` (the page, offscreen).
