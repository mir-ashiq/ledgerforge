# ADR-007: Multi-currency — one base currency, snapshot-on-entry, exact products

- **Status:** Accepted (2026-11-06)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

Issue #4 asks for multi-currency books: `NUMERIC(30,10)` FX rates, base-currency posting rules, and realized/unrealized FX gain ledgers. TallyPrime treats multi-currency as a property of *ledgers and vouchers*, not of the book: you keep books in one currency and record foreign amounts alongside. The hard questions are where the rate lives, who rounds, and how history survives rate changes — and all three answers must obey the three financial laws (docs/analysis.md §7): balance is exact, history is immutable, arithmetic is decimal.

The schema was already decimal-safe (ADR-002): every `amount` is `NUMERIC(26,8)` and the API rejects JSON floats. What was missing was any notion of a foreign currency on an entry, reference rates, and gain/loss ledgers.

## Decision

1. **One base currency per book** — `settings.base_currency` (seeded `INR`; the engine defaults to INR when unset). Reports, Law 1 balancing and trial balances are all in base currency, always. Every entry posts a base-currency `amount`; that number is the ledger's truth.

2. **The rate lives on the entry, not just in a reference table.** A foreign-currency line carries the snapshot trio `currency CHAR(3)` + `amount_fx NUMERIC(26,8)` + `fx_rate NUMERIC(30,10)` (base units per one foreign unit). Because entries are immutable (Law 2), a posted voucher can never be restated by a later rate change. `currencies` (ISO 4217 codes, code-keyed, audited) and `fx_rates` (dated pairs, unique per pair+day) are *advisory input data* for humans and UIs — postings snapshot what they actually used.

3. **The database proves the product.** A table CHECK on `voucher_entries` enforces, for any writer:
   - all three trio fields or none;
   - `amount = round(amount_fx * fx_rate, 8)`.
   PostgreSQL's `round(numeric, n)` rounds half away from zero; the engine mirrors it with `Decimal.quantize(..., ROUND_HALF_UP)` (amounts are positive by earlier checks). Engine and database can never disagree — the same pair the Law-1 deferred trigger and the app-level sum already form.

4. **The client sends the base amount explicitly** (and `amount_fx`/`fx_rate` when foreign). A mismatch returns 422 carrying the *expected* figure, so the fix is mechanical. Nothing is silently computed-and-stored behind the client's back.

5. **Rates are correctable but not erasable.** The app role holds `SELECT, INSERT, UPDATE` on `fx_rates` (no `DELETE`): a typo rate is updated — with the change journaled by an UPDATE/DELETE-only audit trigger — never silently replaced or deleted. `resolve` looks up the exact day's rate, else the latest on-or-before; inverse pairs are **never** auto-derived (`1/rate` re-rounds and would lie about what was applied).

6. **Realized vs unrealized differences are ledgers, not engine magic.** The seed ships four system ledgers — *Realized/Unrealized Exchange Gain* (Indirect Incomes) and *Realized/Unrealized Exchange Loss* (Indirect Expenses). When a foreign position settles at a different rate, the base-currency difference goes to the realized pair as ordinary voucher lines the accountant posts (or a plugin computes); period-end revaluation of open positions would post to the unrealized pair. The engine enforces balance in base currency (Law 1); which gain ledger absorbs a difference is an accounting choice, not an engine decision.

7. **Plugin API 0.2.** `DraftLine` gains the optional trio. Transformers that rebuild or split lines must keep the trio consistent or drop it; the engine (and the CHECK) reject an inconsistent draft even if a plugin produced it. The example GST plugin refuses `meta.gst` on foreign-currency lines rather than silently dropping snapshots — real multi-currency tax logic belongs to a dedicated plugin.

## Consequences

- **(+)** Posted history is immune to rate-table edits — the classic multi-currency bug class is structurally impossible.
- **(+)** Reports needed **zero** changes: `amount` stays the base figure everywhere (`rpt.*` untouched by 0002).
- **(+)** The snapshot invariant holds for any writer — psql, a rogue client, or a buggy plugin — same defense-in-depth philosophy as Laws 1 and 2.
- **(−)** Clients must compute the rounded base amount (the 422 tells them the exact expected value). Accepted: explicitness is the point of Law 3.
- **(−)** No automatic unrealized revaluation yet — the ledgers and the rules are in place; a `revaluation` helper (or plugin) can post to them later.
- **(−)** Changing `base_currency` on a book with postings is unsupported (snapshots are quoted against the old base). Documented non-goal until there is a real migration story.

Shipped as revision `0002_multi_currency` (ADR-006 post-freeze policy): the migration adds the tables/columns/CHECKs, `db/schema.sql` + seed were updated in the same change, and the structural drift guard proves the two paths identical.
