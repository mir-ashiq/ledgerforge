# ADR-009: Multi-book — one database, many companies

- **Status:** Accepted (2026-11-06)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

docs/analysis.md §9 has said it since Phase 1: *"add a `book_id` discriminator to masters and entries before 1.0 (cheap now, breaking later)"*. Every unique constraint, every counter scope, every report function and the whole API surface implicitly assume ONE book; each week of adoption makes retrofitting the discriminator a bigger rewrite of financial history. This is also the gate the authentication layer has been waiting for — `auth.py` promises "per-book permissions land together with the multi-book schema". TallyPrime calls the concept *company*: pick one at the gate, everything you do happens inside it. LedgerForge calls it **book**.

## Decision

1. **A `books` table owns what used to be global settings.** `books(id, name, slug, base_currency, fy_start_month, is_archived, …)`: a book is a legal entity's complete accounting context. `name` is the human label, `slug` the stable machine key (the bootstrap default book is `slug='default'`). `base_currency` (FK → `currencies`) and `fy_start_month` move from `settings` into `books` — they were always per-company facts. The `settings` table remains reserved for genuine system-wide keys.

2. **The `book_id` discriminator lands on every book-owned table, now:** `groups`, `ledgers`, `voucher_types`, `vouchers`, `voucher_entries`, `periods`, `fx_rates`. `currencies` stay global ISO 4217 reference data (shared across books; the base currency is a per-book pointer). `voucher_entries.book_id` is denormalized from the voucher and trigger-verified exactly like `voucher_date` — it is also the future partition key when books grow past millions of entries (roadmap §9 "report scale-out").

3. **Book-local uniqueness.** `groups(book_id, name)`, `ledgers(book_id, name)`, `voucher_types(book_id, code)`, `periods(book_id, name)`, `fx_rates(book_id, from_ccy, to_ccy, valid_on)`, `vouchers(book_id, voucher_type_id, voucher_no)` — two books may each have their own "Cash" ledger, their own FY 2026-27, their own SAL numbering. The single-root index becomes per-book (`UNIQUE (book_id) WHERE is_root`). `client_token` uniqueness becomes `(book_id, client_token)`: the same offline client may retry into two books without interference, and idempotent replay only ever resolves **within** the request's book.

4. **Hierarchy cannot cross books.** A composite foreign key `(book_id, parent_id) → groups(book_id, id)` (backed by `UNIQUE (book_id, id)`) makes same-book parenting a property of the schema, not of engine discipline; `group_closure` stays a pure id-keyed read model.

5. **Periods overlap per book, without btree_gist.** The baseline `EXCLUDE USING gist (daterange …)` cannot take a `book_id WITH =` term unless the `btree_gist` contrib module exists — it does not in the embedded `pgserver` builds developers (and `make test`) run on. Revision 0004 replaces the global EXCLUDE with `trg_periods_no_overlap`, a BEFORE INSERT OR UPDATE trigger that takes `pg_advisory_xact_lock` keyed on the book (period creation is a rare admin action; the lock serializes the rare race) and rejects any overlap with a same-book period. Two books may run identical fiscal years.

6. **Gapless numbering gains a book scope.** `doc_counters.scope_key` becomes `'{book_id}:{voucher_type_id}:{fy}'` — each book's SAL-2026-27-00001 starts at 1. The counter row stays the single locked row in the write path (ADR-002 unchanged).

7. **The API carries the book as context, not as path noise.** Clients send `X-Book-Id: <id>` on every request (TallyPrime's "select company", made a header). Resolution rules: header wins; with exactly one active book the header is optional (every existing single-book client and test keeps working unchanged); zero books → 400 "create a book"; two or more active books and no header → 400 listing the books to pick from. `POST /api/v1/books` creates a book **and seeds its chart of accounts** (TallyPrime's 28 groups, system ledgers, 10 voucher types) in one action; `PATCH` renames/archives; `base_currency` is immutable once the book has vouchers (amounts are stored in it — changing it would restate history, which Law 2 forbids).

8. **Per-book permissions are claims, not tables.** The OIDC token may carry `books`: either `["*"]` or a list of book ids the principal may touch at its global role. `admin` always passes; a token with no `books` claim sees every book (the single-book-era contract every deployed token already satisfies). Scoping the *role* per book (a reader in one book, bookkeeper in another) is deliberately left to the IdP issuing claims — the enforcement seam in `require_book_access` is the only place that knows the rule, so a future membership table can replace the claim reader without touching a single route.

9. **`rpt.*` functions take `p_book_id BIGINT` as their first parameter** — a breaking signature change for raw-SQL callers, taken now while the API is pre-1.0 and raw-SQL report callers are the project's own tests. Every report filters `book_id` at the fact level (`voucher_entries`, `vouchers`, `ledgers`, `groups`).

10. **Seeding becomes code.** The chart-of-accounts seed moves from `db/seed/chart_of_accounts.sql` to `ledgerforge/seeding.py` (`seed_book_defaults`) so `POST /books` and `python -m ledgerforge.bootstrap` create identical books from one definition; bootstrap applies schema+functions SQL, then seeds the default book through the same function.

## Consequences

- **(+)** The discriminator is in place while the user base is small: every future feature (inventory, GST packs, e-invoicing) inherits per-book scoping for free instead of needing a history rewrite.
- **(+)** Single-book installs are unchanged on the wire: no header needed, one book bootstrapped, all pre-0004 clients and tests pass as-is.
- **(+)** Cross-book integrity is schema-enforced: same-book parenting, book-scoped uniqueness, entry↔voucher book agreement, and per-book period overlaps are database facts, not engine conventions.
- **(−)** `rpt.*` signatures changed — any external raw-SQL caller must add the book id (the README's raw-SQL examples show the new form).
- **(−)** Period overlap now depends on an advisory lock instead of a constraint; exotic concurrent same-book period creation is serialized rather than constraint-rejected (identical outcome, marginally later failure).
- **(−)** Two seed sources existed briefly (SQL file for bootstrap, Python for API book creation); ADR-009 collapses them into one Python definition — the SQL file is retired with 0004.

Shipped as revision `0004_multi_book` (ADR-006 post-freeze policy); `db/schema.sql` + `db/functions.sql` updated in the same change, and the drift guard (`tests/test_migrations.py`) proves the alembic and bootstrap paths structurally identical.
