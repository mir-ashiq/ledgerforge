# ruff: noqa: E501 (generated: the SQL slices keep their source formatting)
"""Banking: statement batches/lines and the match annotation (ADR-026).

The second core-schema change since 0006, deliberately — the ADR-025
argument restated for imported facts: statement lines need database
identity (idempotent re-import is a UNIQUE verdict, not a check-then-insert
race) and the reconciliation statement aggregates them exactly. The match
annotation is the kernel's first deliberately mutable table: financial
history is append-only (Law 2 covers batches and lines, trigger-guarded),
while which line matches which entry is revisable annotation — UPDATE only,
DELETE never, audited by its own provenance columns and the after-commit
events.

- ``bank_statement_batches`` — the import receipt (format, source, the
  opening/closing balance when the statement carried them);
- ``bank_statement_lines`` — the facts: value date, direction, exact
  amount, reference, narrative, parsed cheque number, content fingerprint
  (date, direction, amount, reference, narrative, occurrence-within-batch)
  — UNIQUE (book_id, fingerprint) makes re-import a clean no-op;
- ``bank_line_matches`` — one row per statement line ever: the matched
  entry referenced by validated id (a BEFORE INSERT/UPDATE trigger proves
  the entry's voucher, date, book and ledger on BOTH voucher_entries
  shapes — no FK to the partitionable table, no ADR-018 interaction),
  matched_at/by provenance, unmatched_at for the honest un-match.

The upgrade blocks are generated from db/schema.sql verbatim; the
downgrade drops everything this revision created.

Revision ID: 0008_banking
Revises: 0007_cost_centres
Create Date: 2026-11-08
"""

from alembic import op

revision = "0008_banking"
down_revision = "0007_cost_centres"
branch_labels = None
depends_on = None

UPGRADE_SQL = """\
-- ----------------------------------------------------------------------------
-- Banking (ADR-026, migration 0008) — the second core-schema change since
-- 0006, deliberately (the ADR-025 argument restated): imported statement
-- facts need database identity (idempotent re-import is a UNIQUE verdict,
-- not a check-then-insert race) and the reconciliation statement aggregates
-- them with exact-decimal rigour. Three tables, three disciplines:
--
-- - bank_statement_batches — the import receipt (format, source, the
--   opening/closing balance when the statement carried them). Append-only.
-- - bank_statement_lines — the facts themselves: value date, direction
--   (D = money out per the bank = the side the books would post Dr; C =
--   money in = Cr), exact amount, reference, narrative, a parsed cheque
--   number, and a content fingerprint (date, direction, amount, reference,
--   narrative, occurrence-within-batch) so re-importing the same statement
--   is a clean no-op while two genuinely identical transactions both
--   survive. Append-only: financial history imported is history still.
-- - bank_line_matches — the ONE deliberately mutable table: which line the
--   user reconciled to which entry. Not financial truth — Law 2 protects
--   vouchers, entries, allocations and statement facts; the match is
--   annotation, revisable by design (accountants de-reconcile). One row
--   per line ever: unmatch sets unmatched_at, re-match rewrites the row.
--   INSERT/UPDATE only — DELETE never (trigger-forbidden). The entry is
--   referenced by VALIDATED ID, not FK: voucher_entries may be the
--   partitioned shape whose PK is (id, voucher_date), so an FK on id alone
--   cannot exist there; the insert/update trigger proves the entry (same
--   voucher, same date, same book, same ledger as the statement line) on
--   BOTH shapes — shape-independent integrity without touching ADR-018's
--   swap machinery.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bank_statement_batches (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id          BIGINT NOT NULL REFERENCES books(id),
    ledger_id        BIGINT NOT NULL REFERENCES ledgers(id) ON DELETE RESTRICT,
    format           TEXT NOT NULL CHECK (format IN ('csv', 'mt940')),
    source           TEXT,
    currency         CHAR(3),
    opening_balance  NUMERIC(26, 8),
    closing_balance  NUMERIC(26, 8),
    line_count       INTEGER NOT NULL DEFAULT 0,
    imported_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bank_statement_lines (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id      BIGINT NOT NULL REFERENCES books(id),
    ledger_id    BIGINT NOT NULL REFERENCES ledgers(id) ON DELETE RESTRICT,
    batch_id     BIGINT NOT NULL,
    value_date   DATE NOT NULL,
    dc           TEXT NOT NULL CHECK (dc IN ('D', 'C')),
    amount       NUMERIC(26, 8) NOT NULL CHECK (amount > 0),
    reference    TEXT,
    narrative    TEXT,
    cheque_no    TEXT,
    fingerprint  TEXT NOT NULL,
    imported_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_bank_lines_fingerprint UNIQUE (book_id, fingerprint),
    CONSTRAINT fk_bank_lines_batch FOREIGN KEY (batch_id)
        REFERENCES bank_statement_batches (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_bank_lines_ledger_date
    ON bank_statement_lines (ledger_id, value_date);
CREATE INDEX IF NOT EXISTS idx_bank_lines_batch ON bank_statement_lines (batch_id);

CREATE TABLE IF NOT EXISTS bank_line_matches (
    statement_line_id BIGINT PRIMARY KEY,
    book_id           BIGINT NOT NULL REFERENCES books(id),
    voucher_id        BIGINT NOT NULL REFERENCES vouchers(id) ON DELETE RESTRICT,
    entry_id          BIGINT NOT NULL,
    voucher_date      DATE NOT NULL,
    matched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    matched_by        TEXT,
    unmatched_at      TIMESTAMPTZ,
    CONSTRAINT fk_bank_matches_line FOREIGN KEY (statement_line_id)
        REFERENCES bank_statement_lines (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_bank_matches_entry ON bank_line_matches (entry_id);

-- Book and ledger scoping at the only write path the fact tables have
-- (INSERT — they are append-only): a batch's ledger belongs to its book,
-- a line rides its own batch's ledger. The composite-FK trick the groups
-- and cost tables use is unavailable here (ledgers and vouchers carry no
-- UNIQUE (book_id, id)), so the guard is a trigger — same verdict, stated
-- where the rows are born.
CREATE OR REPLACE FUNCTION trg_bank_batches_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_book BIGINT;
BEGIN
    SELECT book_id INTO v_book FROM ledgers WHERE id = NEW.ledger_id;
    IF v_book IS NULL THEN
        RAISE EXCEPTION 'Statement batch references unknown ledger %', NEW.ledger_id;
    END IF;
    IF v_book <> NEW.book_id THEN
        RAISE EXCEPTION 'Statement batch book mismatch: ledger % belongs to book %, the batch claims %',
            NEW.ledger_id, v_book, NEW.book_id;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_bank_batches_check ON bank_statement_batches;
CREATE TRIGGER trg_bank_batches_check
    BEFORE INSERT ON bank_statement_batches
    FOR EACH ROW EXECUTE FUNCTION trg_bank_batches_check();

CREATE OR REPLACE FUNCTION trg_bank_lines_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_batch RECORD;
BEGIN
    SELECT book_id, ledger_id INTO v_batch
    FROM bank_statement_batches WHERE id = NEW.batch_id;
    IF v_batch IS NULL THEN
        RAISE EXCEPTION 'Statement line references unknown batch %', NEW.batch_id;
    END IF;
    IF v_batch.book_id <> NEW.book_id OR v_batch.ledger_id <> NEW.ledger_id THEN
        RAISE EXCEPTION 'Statement line must ride its own batch''s book and ledger (batch % is book %/ledger %; the line claims %/%)',
            NEW.batch_id, v_batch.book_id, v_batch.ledger_id, NEW.book_id, NEW.ledger_id;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_bank_lines_check ON bank_statement_lines;
CREATE TRIGGER trg_bank_lines_check
    BEFORE INSERT ON bank_statement_lines
    FOR EACH ROW EXECUTE FUNCTION trg_bank_lines_check();

-- Match integrity, shape-independent: the entry exists with exactly this
-- voucher and date, in the same book, ON THE STATEMENT LINE'S OWN LEDGER —
-- every guarantee an FK to voucher_entries(id) would give, checked at the
-- only write paths the annotation table has. A reversal does not kill a
-- match (annotations are not financial state); the folds surface the pair.
CREATE OR REPLACE FUNCTION trg_bank_matches_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_line  RECORD;
    v_entry RECORD;
BEGIN
    SELECT book_id, ledger_id INTO v_line
    FROM bank_statement_lines WHERE id = NEW.statement_line_id;
    IF v_line IS NULL THEN
        RAISE EXCEPTION 'Bank match references unknown statement line %', NEW.statement_line_id;
    END IF;
    IF NEW.book_id <> v_line.book_id THEN
        RAISE EXCEPTION 'Bank match book mismatch: line % belongs to book %, the match claims %',
            NEW.statement_line_id, v_line.book_id, NEW.book_id;
    END IF;
    SELECT e.ledger_id, e.dc, v.book_id AS vbook
    INTO v_entry
    FROM voucher_entries e
    JOIN vouchers v ON v.id = e.voucher_id
    WHERE e.id = NEW.entry_id AND e.voucher_id = NEW.voucher_id
      AND e.voucher_date = NEW.voucher_date;
    IF v_entry IS NULL THEN
        RAISE EXCEPTION 'Bank match references entry % of voucher % dated % — no such entry (the match must name the entry''s own voucher and date)',
            NEW.entry_id, NEW.voucher_id, NEW.voucher_date;
    END IF;
    IF v_entry.vbook <> NEW.book_id THEN
        RAISE EXCEPTION 'Bank match voucher % belongs to another book', NEW.voucher_id;
    END IF;
    IF v_entry.ledger_id <> v_line.ledger_id THEN
        RAISE EXCEPTION 'Bank match pairs a line of ledger % with an entry of ledger % — reconcile within one bank ledger',
            v_line.ledger_id, v_entry.ledger_id;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.unmatched_at IS NOT NULL THEN
        RAISE EXCEPTION 'A new bank match is live — unmatched_at is set by un-matching, never at insert';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_bank_matches_check ON bank_line_matches;
CREATE TRIGGER trg_bank_matches_check
    BEFORE INSERT OR UPDATE ON bank_line_matches
    FOR EACH ROW EXECUTE FUNCTION trg_bank_matches_check();

-- ----------------------------------------------------------------------------
-- Law 2 for the imported facts: append-only, trigger-guarded. The match
-- annotation is the exception with a stated boundary — UPDATE is its edit
-- path, DELETE never (ADR-026).
-- ----------------------------------------------------------------------------
-- Imported statement facts are history the moment they land (ADR-026): a
-- correction is a new import, never an edit of a fact already reconciled
-- against. The batches (import receipts) are history with them.
DROP TRIGGER IF EXISTS trg_bank_batches_immutable ON bank_statement_batches;
CREATE TRIGGER trg_bank_batches_immutable
    BEFORE UPDATE OR DELETE ON bank_statement_batches
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();

DROP TRIGGER IF EXISTS trg_bank_lines_immutable ON bank_statement_lines;
CREATE TRIGGER trg_bank_lines_immutable
    BEFORE UPDATE OR DELETE ON bank_statement_lines
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();

-- The match annotation is the ONE deliberately mutable table (ADR-026):
-- UPDATE is the honest edit path (un-match sets unmatched_at, re-match
-- rewrites the row) — but rows are never erased. The audit trail is the
-- row's own provenance columns plus the after-commit events.
DROP TRIGGER IF EXISTS trg_bank_matches_no_delete ON bank_line_matches;
CREATE TRIGGER trg_bank_matches_no_delete
    BEFORE DELETE ON bank_line_matches
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();

-- ----------------------------------------------------------------------------
-- Privileges: INSERT-only on the facts (financial history), UPDATE-allowed
-- on the annotation (DELETE never — the trigger mirrors the privilege).
-- ----------------------------------------------------------------------------
-- Banking (0008/ADR-026): statement facts and import receipts are financial
-- history — INSERT only, append-only. The match annotation is mutable by
-- design — UPDATE allowed, DELETE never (mirrored by the no-delete trigger).
GRANT INSERT ON bank_statement_batches, bank_statement_lines TO ledgerforge_app;
GRANT SELECT ON bank_statement_batches, bank_statement_lines TO ledgerforge_app;
GRANT SELECT, INSERT, UPDATE ON bank_line_matches TO ledgerforge_app;
GRANT USAGE ON SEQUENCE bank_statement_batches_id_seq, bank_statement_lines_id_seq
    TO ledgerforge_app;
"""

DOWNGRADE_SQL = """\
-- Privileges first: REVOKEs need the objects to still exist.
REVOKE INSERT ON bank_statement_batches, bank_statement_lines FROM ledgerforge_app;
REVOKE SELECT ON bank_statement_batches, bank_statement_lines FROM ledgerforge_app;
REVOKE SELECT, INSERT, UPDATE ON bank_line_matches FROM ledgerforge_app;
REVOKE USAGE ON SEQUENCE bank_statement_batches_id_seq, bank_statement_lines_id_seq
    FROM ledgerforge_app;

DROP TRIGGER IF EXISTS trg_bank_matches_no_delete ON bank_line_matches;
DROP TRIGGER IF EXISTS trg_bank_lines_immutable ON bank_statement_lines;
DROP TRIGGER IF EXISTS trg_bank_batches_immutable ON bank_statement_batches;
DROP TRIGGER IF EXISTS trg_bank_matches_check ON bank_line_matches;
DROP TRIGGER IF EXISTS trg_bank_lines_check ON bank_statement_lines;
DROP TRIGGER IF EXISTS trg_bank_batches_check ON bank_statement_batches;
DROP TABLE IF EXISTS bank_line_matches;
DROP FUNCTION IF EXISTS trg_bank_matches_check();
DROP TABLE IF EXISTS bank_statement_lines;
DROP FUNCTION IF EXISTS trg_bank_lines_check();
DROP TABLE IF EXISTS bank_statement_batches;
DROP FUNCTION IF EXISTS trg_bank_batches_check();
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
