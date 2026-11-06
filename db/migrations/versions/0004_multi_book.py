"""Multi-book: one database, many companies (ADR-009).

The roadmap's own warning: adding the ``book_id`` discriminator to masters
and entries is cheap before 1.0 and a history rewrite afterwards. This is
that migration, taken while the user base is small.

The design in one breath:

- A ``books`` table owns what used to be global settings: company name,
  base currency, fiscal-year start. The bootstrap default book is
  ``slug='default'``; existing single-book installs are migrated into it
  (their ``settings`` values become the book's row).
- ``book_id`` lands on every book-owned table: groups, ledgers,
  voucher_types, vouchers, voucher_entries, periods, fx_rates.
  ``currencies`` stay shared ISO 4217 reference data. Uniqueness moves
  book-local (ledger names, group names, voucher type codes, period names,
  FX pairs, voucher numbering restarts, idempotency tokens).
- Hierarchy cannot cross books: composite FK ``(book_id, parent_id)`` →
  ``groups(book_id, id)``.
- Per-book period non-overlap replaces the global gist EXCLUDE (btree_gist
  is not available in slim/embedded builds) with an advisory-lock trigger.
- ``rpt.*`` functions gain ``p_book_id BIGINT`` as their first parameter —
  a deliberate, pre-1.0 signature break for raw-SQL callers.
- ``doc_counters`` scope keys gain the book prefix ``'{book}:{vtype}:{fy}'``.
- The chart-of-accounts seed moves from SQL to ``ledgerforge/seeding.py``
  so bootstrap and ``POST /api/v1/books`` create identical books.

Downgrade folds every book back into the implicit single book: all rows are
assigned to whatever ``settings`` said before 0004 (restored from the
default book), book columns and the books table are dropped, and the global
unique constraints return. A downgrade with several populated books refuses
loudly (two roots / two same-named ledgers cannot fold into one book).

Revision ID: 0004_multi_book
Revises: 0003_period_locking
Create Date: 2026-11-06
"""

from alembic import op

revision = "0004_multi_book"
down_revision = "0003_period_locking"
branch_labels = None
depends_on = None

# -- db/schema.sql + db/functions.sql (head state) — kept in sync by
# tests/test_migrations.py. Trigger/report bodies are byte-identical to the
# SQL files: the drift guard compares pg_get_functiondef output.

BOOKS_SQL = """\
CREATE TABLE books (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    slug           TEXT NOT NULL UNIQUE,
    base_currency  CHAR(3) NOT NULL DEFAULT 'INR' REFERENCES currencies(code),
    fy_start_month SMALLINT NOT NULL DEFAULT 4
                   CONSTRAINT chk_books_fy_start CHECK (fy_start_month BETWEEN 1 AND 12),
    is_archived    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_audit_books ON books;
CREATE TRIGGER trg_audit_books
    AFTER INSERT OR UPDATE OR DELETE ON books
    FOR EACH ROW EXECUTE FUNCTION trg_audit_master();

GRANT SELECT, INSERT, UPDATE ON books TO ledgerforge_app;
GRANT USAGE ON SEQUENCE books_id_seq TO ledgerforge_app;
"""

DEFAULT_BOOK_SQL = """\
DO $$
DECLARE
    v_name TEXT;
    v_base TEXT;
    v_fy   SMALLINT;
BEGIN
    -- The base currency ships as a system row (mirrors the seed).
    INSERT INTO currencies (code, name, symbol, minor_unit, is_system)
    VALUES ('INR', 'Indian Rupee', '₹', 2, TRUE)
    ON CONFLICT (code) DO NOTHING;

    SELECT value INTO v_name FROM settings WHERE key = 'company_name';
    v_name := NULLIF(TRIM(COALESCE(v_name, '')), '');
    IF v_name IS NULL THEN
        v_name := 'My Company';
    END IF;

    SELECT value INTO v_base FROM settings WHERE key = 'base_currency';
    v_base := UPPER(TRIM(COALESCE(v_base, '')));
    IF v_base = '' OR NOT EXISTS (SELECT 1 FROM currencies WHERE code = v_base) THEN
        v_base := 'INR';
    END IF;

    BEGIN
        v_fy := COALESCE((SELECT value FROM settings
                          WHERE key = 'fy_start_month')::SMALLINT, 4);
    EXCEPTION WHEN OTHERS THEN
        v_fy := 4;
    END;
    IF v_fy < 1 OR v_fy > 12 THEN
        v_fy := 4;
    END IF;

    INSERT INTO books (name, slug, base_currency, fy_start_month)
    VALUES (v_name, 'default', v_base, v_fy);
END $$;
"""

IMMUTABLE_OFF_SQL = """\
-- Law 2 triggers block the book_id backfill UPDATEs below; the migration
-- itself is the sanctioned maintenance path, so they come off for the
-- duration (same transaction) and are restored verbatim at the end.
DROP TRIGGER trg_vouchers_immutable ON vouchers;
DROP TRIGGER trg_voucher_entries_immutable ON voucher_entries;
"""

IMMUTABLE_ON_SQL = """\
CREATE TRIGGER trg_vouchers_immutable
    BEFORE UPDATE OR DELETE ON vouchers
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();

CREATE TRIGGER trg_voucher_entries_immutable
    BEFORE UPDATE OR DELETE ON voucher_entries
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();
"""

DISCRIMINATOR_SQL = """\
-- book_id on every book-owned table: add (nullable) -> backfill from the
-- default book -> tighten to NOT NULL -> FK. Auto-named FKs
-- (groups_book_id_fkey, …) match the head schema.sql inline REFERENCES.
ALTER TABLE groups ADD COLUMN book_id BIGINT REFERENCES books(id);
ALTER TABLE ledgers ADD COLUMN book_id BIGINT REFERENCES books(id);
ALTER TABLE voucher_types ADD COLUMN book_id BIGINT REFERENCES books(id);
ALTER TABLE vouchers ADD COLUMN book_id BIGINT REFERENCES books(id);
ALTER TABLE periods ADD COLUMN book_id BIGINT REFERENCES books(id);
ALTER TABLE fx_rates ADD COLUMN book_id BIGINT REFERENCES books(id);
ALTER TABLE voucher_entries ADD COLUMN book_id BIGINT REFERENCES books(id);

UPDATE groups SET book_id = (SELECT id FROM books WHERE slug = 'default');
UPDATE ledgers SET book_id = (SELECT id FROM books WHERE slug = 'default');
UPDATE voucher_types SET book_id = (SELECT id FROM books WHERE slug = 'default');
UPDATE vouchers SET book_id = (SELECT id FROM books WHERE slug = 'default');
UPDATE periods SET book_id = (SELECT id FROM books WHERE slug = 'default');
UPDATE fx_rates SET book_id = (SELECT id FROM books WHERE slug = 'default');
UPDATE voucher_entries e SET book_id = v.book_id
FROM vouchers v WHERE v.id = e.voucher_id;

ALTER TABLE groups ALTER COLUMN book_id SET NOT NULL;
ALTER TABLE ledgers ALTER COLUMN book_id SET NOT NULL;
ALTER TABLE voucher_types ALTER COLUMN book_id SET NOT NULL;
ALTER TABLE vouchers ALTER COLUMN book_id SET NOT NULL;
ALTER TABLE periods ALTER COLUMN book_id SET NOT NULL;
ALTER TABLE fx_rates ALTER COLUMN book_id SET NOT NULL;
ALTER TABLE voucher_entries ALTER COLUMN book_id SET NOT NULL;

-- Numbering and idempotency become book-scoped counters: prefix the
-- existing scopes with the default book id ('{book}:{vtype}:{fy}').
UPDATE doc_counters
SET scope_key = (SELECT id FROM books WHERE slug = 'default')::TEXT || ':' || scope_key;

-- Per-book facts leave settings (they live on the books row now).
DELETE FROM settings WHERE key IN ('company_name', 'base_currency', 'fy_start_month');
"""

GROUPS_SQL = """\
-- Hierarchy cannot cross books: the composite FK replaces the simple
-- parent FK; names become book-local.
ALTER TABLE groups ADD CONSTRAINT uq_groups_book_id UNIQUE (book_id, id);
ALTER TABLE groups DROP CONSTRAINT groups_parent_id_fkey;
ALTER TABLE groups ADD CONSTRAINT fk_groups_parent_same_book
    FOREIGN KEY (book_id, parent_id) REFERENCES groups (book_id, id) ON DELETE RESTRICT;
ALTER TABLE groups DROP CONSTRAINT groups_name_key;
ALTER TABLE groups ADD CONSTRAINT uq_groups_book_name UNIQUE (book_id, name);
DROP INDEX uq_groups_single_root;
CREATE UNIQUE INDEX uq_groups_single_root ON groups (book_id) WHERE is_root;

ALTER TABLE ledgers DROP CONSTRAINT ledgers_name_key;
ALTER TABLE ledgers ADD CONSTRAINT uq_ledgers_book_name UNIQUE (book_id, name);

ALTER TABLE voucher_types DROP CONSTRAINT voucher_types_code_key;
ALTER TABLE voucher_types ADD CONSTRAINT uq_voucher_types_book_code UNIQUE (book_id, code);

-- The offline client's idempotency key is unique per book, not globally.
ALTER TABLE vouchers DROP CONSTRAINT vouchers_client_token_key;
ALTER TABLE vouchers ADD CONSTRAINT uq_voucher_client_token_book UNIQUE (book_id, client_token);

DROP INDEX idx_vouchers_date;
CREATE INDEX idx_vouchers_date ON vouchers (book_id, voucher_date);
DROP INDEX idx_vouchers_type_date;
CREATE INDEX idx_vouchers_type_date ON vouchers (book_id, voucher_type_id, voucher_date);

DROP INDEX idx_entries_ledger_date;
CREATE INDEX idx_entries_ledger_date
    ON voucher_entries (book_id, ledger_id, voucher_date) INCLUDE (dc, amount);

ALTER TABLE periods DROP CONSTRAINT periods_name_key;
ALTER TABLE periods ADD CONSTRAINT uq_periods_book_name UNIQUE (book_id, name);
-- 0003's global non-overlap EXCLUDE is replaced by trg_periods_no_overlap
-- (per-book; btree_gist is not portable — see PERIODS_SQL below).
ALTER TABLE periods DROP CONSTRAINT uq_periods_no_overlap;

-- Voucher numbers restart per book: each book's SAL-2026-27-00001 is the
-- first of its own series (ADR-009).
ALTER TABLE vouchers DROP CONSTRAINT uq_voucher_no_per_type;
ALTER TABLE vouchers ADD CONSTRAINT uq_voucher_no_per_type
    UNIQUE (book_id, voucher_type_id, voucher_no);

ALTER TABLE fx_rates DROP CONSTRAINT uq_fx_rates_pair_day;
ALTER TABLE fx_rates ADD CONSTRAINT uq_fx_rates_pair_day
    UNIQUE (book_id, from_ccy, to_ccy, valid_on);
"""

ENTRIES_TRIGGER_SQL = """\
-- Entry date and book must match the voucher's (denormalization proofs).
CREATE OR REPLACE FUNCTION trg_entries_check_date() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_date DATE;
    v_book BIGINT;
BEGIN
    SELECT voucher_date, book_id INTO v_date, v_book FROM vouchers WHERE id = NEW.voucher_id;
    IF v_date IS NULL THEN
        RAISE EXCEPTION 'Voucher % does not exist', NEW.voucher_id;
    END IF;
    IF v_date <> NEW.voucher_date THEN
        RAISE EXCEPTION 'Entry date % does not match voucher date %', NEW.voucher_date, v_date;
    END IF;
    IF v_book <> NEW.book_id THEN
        RAISE EXCEPTION 'Entry book % does not match voucher book %', NEW.book_id, v_book;
    END IF;
    RETURN NEW;
END $$;
"""

PERIODS_SQL = """\
-- Per-book non-overlap. A gist EXCLUDE over (book_id =, daterange &&) would
-- need the btree_gist contrib module, which embedded/slim builds (pgserver,
-- alpine) may not ship; the advisory-lock trigger is portable and serializes
-- the rare concurrent same-book period creation exactly as tightly (ADR-009).
CREATE OR REPLACE FUNCTION trg_periods_no_overlap() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('ledgerforge:periods:' || NEW.book_id::TEXT));
    IF EXISTS (
        SELECT 1 FROM periods p
        WHERE p.book_id = NEW.book_id
          AND p.id <> NEW.id
          AND daterange(p.starts_on, p.ends_on, '[]')
              && daterange(NEW.starts_on, NEW.ends_on, '[]')
    ) THEN
        RAISE EXCEPTION
            'Period % overlaps an existing period of this book — periods must not share dates',
            NEW.name;
    END IF;
    RETURN NEW;
END $$;

-- Deletion is only safe on an open, empty period (same book only).
CREATE OR REPLACE FUNCTION trg_periods_delete_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_n BIGINT;
BEGIN
    IF OLD.status <> 'open' THEN
        RAISE EXCEPTION 'Only open periods can be deleted; % is %', OLD.name, OLD.status;
    END IF;
    SELECT count(*) INTO v_n FROM vouchers
    WHERE book_id = OLD.book_id
      AND voucher_date BETWEEN OLD.starts_on AND OLD.ends_on;
    IF v_n > 0 THEN
        RAISE EXCEPTION 'Period % contains % voucher(s) and cannot be deleted',
            OLD.name, v_n;
    END IF;
    RETURN OLD;
END $$;

-- FOR SHARE serializes a posting against a concurrent lock/close of the same
-- period row: whichever commits first defines the outcome for the other.
CREATE OR REPLACE FUNCTION trg_vouchers_check_period() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_status TEXT;
    v_name   TEXT;
BEGIN
    -- Opt-in per book: books that define no periods accept every date
    -- (pre-feature behavior; existing books are unaffected until they
    -- adopt periods).
    IF NOT EXISTS (SELECT 1 FROM periods WHERE book_id = NEW.book_id) THEN
        RETURN NEW;
    END IF;
    SELECT p.status, p.name INTO v_status, v_name
    FROM periods p
    WHERE p.book_id = NEW.book_id
      AND NEW.voucher_date >= p.starts_on AND NEW.voucher_date <= p.ends_on
    FOR SHARE OF p;
    IF v_status IS NULL THEN
        RAISE EXCEPTION 'No period covers voucher date %: create the period first',
            NEW.voucher_date;
    END IF;
    IF v_status = 'closed' THEN
        RAISE EXCEPTION 'Period % is closed: its dates are final', v_name;
    END IF;
    IF v_status = 'locked' THEN
        RAISE EXCEPTION 'Period % is locked: postings are frozen until it is closed or reopened',
            v_name;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_periods_overlap ON periods;
CREATE TRIGGER trg_periods_overlap
    BEFORE INSERT OR UPDATE ON periods
    FOR EACH ROW EXECUTE FUNCTION trg_periods_no_overlap();
"""

# -- the book-scoped reporting functions (db/functions.sql head state) ------
TRIAL_BALANCE_SQL = """\
CREATE OR REPLACE FUNCTION rpt.trial_balance(p_book_id BIGINT, p_as_of DATE)
RETURNS TABLE (
    out_ledger_id   BIGINT,
    out_ledger_name TEXT,
    out_group_id    BIGINT,
    out_group_name  TEXT,
    out_nature      TEXT,
    out_dr          NUMERIC,
    out_cr          NUMERIC,
    out_balance     NUMERIC,
    out_dc          CHAR(1)
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH per_ledger AS (
        SELECT e.ledger_id,
               COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0) AS dr,
               COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0) AS cr
        FROM voucher_entries e
        JOIN vouchers v ON v.id = e.voucher_id
        WHERE e.book_id = p_book_id
          AND v.voucher_date <= p_as_of
        GROUP BY e.ledger_id
    )
    SELECT l.id, l.name, g.id, g.name, g.nature,
           pl.dr, pl.cr,
           CASE WHEN pl.dr >= pl.cr THEN pl.dr - pl.cr ELSE pl.cr - pl.dr END,
           CASE WHEN pl.dr >= pl.cr THEN 'D' ELSE 'C' END
    FROM per_ledger pl
    JOIN ledgers l ON l.id = pl.ledger_id
    JOIN groups g ON g.id = l.group_id
    WHERE l.book_id = p_book_id
      AND g.book_id = p_book_id
$$;
"""

GROUP_ROLLUPS_SQL = """\
CREATE OR REPLACE FUNCTION rpt.group_rollups(p_book_id BIGINT, p_as_of DATE)
RETURNS TABLE (
    out_group_id   BIGINT,
    out_group_name TEXT,
    out_parent_id  BIGINT,
    out_nature     TEXT,
    out_dr         NUMERIC,
    out_cr         NUMERIC,
    out_net        NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH per_ledger AS (
        SELECT l.group_id AS gid,
               SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END) AS dr,
               SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END) AS cr
        FROM voucher_entries e
        JOIN ledgers l ON l.id = e.ledger_id
        JOIN vouchers v ON v.id = e.voucher_id
        WHERE e.book_id = p_book_id
          AND v.voucher_date <= p_as_of
        GROUP BY l.group_id
    ),
    per_group AS (
        SELECT c.ancestor_id AS gid,
               SUM(pl.dr) AS dr,
               SUM(pl.cr) AS cr
        FROM per_ledger pl
        JOIN group_closure c ON c.descendant_id = pl.gid
        GROUP BY c.ancestor_id
    )
    SELECT g.id, g.name, g.parent_id, g.nature, pg.dr, pg.cr,
           CASE WHEN g.nature IN ('asset', 'expense') THEN pg.dr - pg.cr
                ELSE pg.cr - pg.dr END
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid
    WHERE g.book_id = p_book_id
$$;
"""

PROFIT_LOSS_SQL = """\
CREATE OR REPLACE FUNCTION rpt.profit_and_loss(p_book_id BIGINT, p_from DATE, p_to DATE)
RETURNS TABLE (
    out_section   TEXT,
    out_bucket    TEXT,
    out_group_id  BIGINT,
    out_group_name TEXT,
    out_amount    NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH per_ledger AS (
        SELECT l.group_id AS gid,
               COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0) AS dr,
               COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0) AS cr
        FROM voucher_entries e
        JOIN ledgers l ON l.id = e.ledger_id
        JOIN vouchers v ON v.id = e.voucher_id
        JOIN voucher_types vt ON vt.id = v.voucher_type_id
        WHERE e.book_id = p_book_id
          AND v.voucher_date BETWEEN p_from AND p_to
          AND vt.code <> 'FYC'
        GROUP BY l.group_id
    ),
    ledger_class AS (
        SELECT g.nature, g.affects_gross, pl.dr, pl.cr
        FROM per_ledger pl
        JOIN groups g ON g.id = pl.gid
    ),
    per_group AS (
        SELECT c.ancestor_id AS gid,
               SUM(pl.dr) AS dr,
               SUM(pl.cr) AS cr
        FROM per_ledger pl
        JOIN group_closure c ON c.descendant_id = pl.gid
        GROUP BY c.ancestor_id
    ),
    totals AS (
        SELECT
            COALESCE(SUM(CASE WHEN nature = 'income'  AND affects_gross THEN cr - dr END), 0)
                AS direct_income,
            COALESCE(SUM(CASE WHEN nature = 'expense' AND affects_gross THEN dr - cr END), 0)
                AS direct_expense,
            COALESCE(SUM(CASE WHEN nature = 'income'  AND NOT affects_gross THEN cr - dr END), 0)
                AS indirect_income,
            COALESCE(SUM(CASE WHEN nature = 'expense' AND NOT affects_gross THEN dr - cr END), 0)
                AS indirect_expense
        FROM ledger_class
    )
    SELECT 'trading', 'direct_income', g.id, g.name, pg.cr - pg.dr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'income' AND g.affects_gross

    UNION ALL
    SELECT 'trading', 'direct_expense', g.id, g.name, pg.dr - pg.cr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'expense' AND g.affects_gross

    UNION ALL
    SELECT 'profit', 'gross_profit', NULL, NULL, t.direct_income - t.direct_expense
    FROM totals t

    UNION ALL
    SELECT 'profit', 'indirect_income', g.id, g.name, pg.cr - pg.dr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'income' AND NOT g.affects_gross

    UNION ALL
    SELECT 'profit', 'indirect_expense', g.id, g.name, pg.dr - pg.cr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'expense' AND NOT g.affects_gross

    UNION ALL
    SELECT 'profit', 'net_profit', NULL, NULL,
           (t.direct_income - t.direct_expense) + t.indirect_income - t.indirect_expense
    FROM totals t
$$;
"""

BALANCE_SHEET_SQL = """\
CREATE OR REPLACE FUNCTION rpt.balance_sheet(p_book_id BIGINT, p_as_of DATE)
RETURNS TABLE (
    out_section    TEXT,
    out_group_id   BIGINT,
    out_group_name TEXT,
    out_parent_id  BIGINT,
    out_amount     NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH per_ledger AS (
        SELECT l.group_id AS gid,
               COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0) AS dr,
               COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0) AS cr
        FROM voucher_entries e
        JOIN ledgers l ON l.id = e.ledger_id
        JOIN vouchers v ON v.id = e.voucher_id
        WHERE e.book_id = p_book_id
          AND v.voucher_date <= p_as_of
        GROUP BY l.group_id
    ),
    per_group AS (
        SELECT c.ancestor_id AS gid,
               SUM(pl.dr) AS dr,
               SUM(pl.cr) AS cr
        FROM per_ledger pl
        JOIN group_closure c ON c.descendant_id = pl.gid
        GROUP BY c.ancestor_id
    )
    SELECT 'assets', g.id, g.name, g.parent_id, pg.dr - pg.cr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'asset'

    UNION ALL
    SELECT 'liabilities', g.id, g.name, g.parent_id, pg.cr - pg.dr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'liability'

    UNION ALL
    SELECT 'equity', g.id, g.name, g.parent_id, pg.cr - pg.dr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'equity'

    UNION ALL
    SELECT 'equity', NULL, 'Profit & Loss A/c (cumulative)', NULL,
           COALESCE(SUM(CASE WHEN g.nature = 'income' THEN pl.cr - pl.dr END), 0)
         - COALESCE(SUM(CASE WHEN g.nature = 'expense' THEN pl.dr - pl.cr END), 0)
    FROM per_ledger pl
    JOIN groups g ON g.id = pl.gid
    WHERE g.book_id = p_book_id
$$;
"""

FY_CLOSE_SQL = """\
CREATE OR REPLACE FUNCTION rpt.fy_close(p_book_id BIGINT, p_as_of DATE, p_pl_ledger_id BIGINT)
RETURNS TABLE (
    out_bucket      TEXT,
    out_ledger_id   BIGINT,
    out_ledger_name TEXT,
    out_dc          CHAR(1),
    out_amount      NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH per_ledger AS (
        SELECT e.ledger_id,
               COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0) AS dr,
               COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0) AS cr
        FROM voucher_entries e
        JOIN vouchers v ON v.id = e.voucher_id
        JOIN ledgers l ON l.id = e.ledger_id
        JOIN groups g ON g.id = l.group_id
        WHERE e.book_id = p_book_id
          AND v.voucher_date <= p_as_of
          AND g.nature IN ('income', 'expense')
        GROUP BY e.ledger_id
    ),
    balances AS (
        SELECT l.id AS ledger_id, l.name AS ledger_name, g.nature,
               CASE WHEN g.nature = 'income' THEN pl.cr - pl.dr
                    ELSE pl.dr - pl.cr END AS balance
        FROM per_ledger pl
        JOIN ledgers l ON l.id = pl.ledger_id
        JOIN groups g ON g.id = l.group_id
        WHERE l.book_id = p_book_id
    )
    SELECT 'income', b.ledger_id, b.ledger_name,
           CASE WHEN b.balance > 0 THEN 'D' ELSE 'C' END, ABS(b.balance)
    FROM balances b
    WHERE b.nature = 'income' AND b.balance <> 0

    UNION ALL
    SELECT 'expense', b.ledger_id, b.ledger_name,
           CASE WHEN b.balance > 0 THEN 'C' ELSE 'D' END, ABS(b.balance)
    FROM balances b
    WHERE b.nature = 'expense' AND b.balance <> 0

    UNION ALL
    SELECT 'profit_loss', p_pl_ledger_id,
           (SELECT name FROM ledgers WHERE id = p_pl_ledger_id),
           CASE WHEN t.net > 0 THEN 'C' ELSE 'D' END, ABS(t.net)
    FROM (
        SELECT COALESCE(SUM(balance) FILTER (WHERE nature = 'income'), 0)
             - COALESCE(SUM(balance) FILTER (WHERE nature = 'expense'), 0) AS net
        FROM balances
    ) t
    WHERE t.net <> 0
$$;
"""

RPT_DOWNGRADE_SQL = """\
-- 0004 downgrade: restore the 0003-signature report functions (no book
-- parameter). Bodies are the 0003 state verbatim.
DROP FUNCTION IF EXISTS rpt.trial_balance(BIGINT, DATE);
DROP FUNCTION IF EXISTS rpt.group_rollups(BIGINT, DATE);
DROP FUNCTION IF EXISTS rpt.profit_and_loss(BIGINT, DATE, DATE);
DROP FUNCTION IF EXISTS rpt.balance_sheet(BIGINT, DATE);
DROP FUNCTION IF EXISTS rpt.fy_close(BIGINT, DATE, BIGINT);

CREATE OR REPLACE FUNCTION rpt.trial_balance(p_as_of DATE)
RETURNS TABLE (
    out_ledger_id   BIGINT,
    out_ledger_name TEXT,
    out_group_id    BIGINT,
    out_group_name  TEXT,
    out_nature      TEXT,
    out_dr          NUMERIC,
    out_cr          NUMERIC,
    out_balance     NUMERIC,
    out_dc          CHAR(1)
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH per_ledger AS (
        SELECT e.ledger_id,
               COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0) AS dr,
               COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0) AS cr
        FROM voucher_entries e
        JOIN vouchers v ON v.id = e.voucher_id
        WHERE v.voucher_date <= p_as_of
        GROUP BY e.ledger_id
    )
    SELECT l.id, l.name, g.id, g.name, g.nature,
           pl.dr, pl.cr,
           CASE WHEN pl.dr >= pl.cr THEN pl.dr - pl.cr ELSE pl.cr - pl.dr END,
           CASE WHEN pl.dr >= pl.cr THEN 'D' ELSE 'C' END
    FROM per_ledger pl
    JOIN ledgers l ON l.id = pl.ledger_id
    JOIN groups g ON g.id = l.group_id
$$;

CREATE OR REPLACE FUNCTION rpt.group_rollups(p_as_of DATE)
RETURNS TABLE (
    out_group_id   BIGINT,
    out_group_name TEXT,
    out_parent_id  BIGINT,
    out_nature     TEXT,
    out_dr         NUMERIC,
    out_cr         NUMERIC,
    out_net        NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH per_ledger AS (
        SELECT l.group_id AS gid,
               SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END) AS dr,
               SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END) AS cr
        FROM voucher_entries e
        JOIN ledgers l ON l.id = e.ledger_id
        JOIN vouchers v ON v.id = e.voucher_id
        WHERE v.voucher_date <= p_as_of
        GROUP BY l.group_id
    ),
    per_group AS (
        SELECT c.ancestor_id AS gid,
               SUM(pl.dr) AS dr,
               SUM(pl.cr) AS cr
        FROM per_ledger pl
        JOIN group_closure c ON c.descendant_id = pl.gid
        GROUP BY c.ancestor_id
    )
    SELECT g.id, g.name, g.parent_id, g.nature, pg.dr, pg.cr,
           CASE WHEN g.nature IN ('asset', 'expense') THEN pg.dr - pg.cr
                ELSE pg.cr - pg.dr END
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid
$$;

CREATE OR REPLACE FUNCTION rpt.profit_and_loss(p_from DATE, p_to DATE)
RETURNS TABLE (
    out_section   TEXT,
    out_bucket    TEXT,
    out_group_id  BIGINT,
    out_group_name TEXT,
    out_amount    NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH per_ledger AS (
        SELECT l.group_id AS gid,
               COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0) AS dr,
               COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0) AS cr
        FROM voucher_entries e
        JOIN ledgers l ON l.id = e.ledger_id
        JOIN vouchers v ON v.id = e.voucher_id
        JOIN voucher_types vt ON vt.id = v.voucher_type_id
        WHERE v.voucher_date BETWEEN p_from AND p_to
          AND vt.code <> 'FYC'
        GROUP BY l.group_id
    ),
    ledger_class AS (
        SELECT g.nature, g.affects_gross, pl.dr, pl.cr
        FROM per_ledger pl
        JOIN groups g ON g.id = pl.gid
    ),
    per_group AS (
        SELECT c.ancestor_id AS gid,
               SUM(pl.dr) AS dr,
               SUM(pl.cr) AS cr
        FROM per_ledger pl
        JOIN group_closure c ON c.descendant_id = pl.gid
        GROUP BY c.ancestor_id
    ),
    totals AS (
        SELECT
            COALESCE(SUM(CASE WHEN nature = 'income'  AND affects_gross THEN cr - dr END), 0)
                AS direct_income,
            COALESCE(SUM(CASE WHEN nature = 'expense' AND affects_gross THEN dr - cr END), 0)
                AS direct_expense,
            COALESCE(SUM(CASE WHEN nature = 'income'  AND NOT affects_gross THEN cr - dr END), 0)
                AS indirect_income,
            COALESCE(SUM(CASE WHEN nature = 'expense' AND NOT affects_gross THEN dr - cr END), 0)
                AS indirect_expense
        FROM ledger_class
    )
    SELECT 'trading', 'direct_income', g.id, g.name, pg.cr - pg.dr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'income' AND g.affects_gross

    UNION ALL
    SELECT 'trading', 'direct_expense', g.id, g.name, pg.dr - pg.cr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'expense' AND g.affects_gross

    UNION ALL
    SELECT 'profit', 'gross_profit', NULL, NULL, t.direct_income - t.direct_expense
    FROM totals t

    UNION ALL
    SELECT 'profit', 'indirect_income', g.id, g.name, pg.cr - pg.dr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'income' AND NOT g.affects_gross

    UNION ALL
    SELECT 'profit', 'indirect_expense', g.id, g.name, pg.dr - pg.cr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'expense' AND NOT g.affects_gross

    UNION ALL
    SELECT 'profit', 'net_profit', NULL, NULL,
           (t.direct_income - t.direct_expense) + t.indirect_income - t.indirect_expense
    FROM totals t
$$;

CREATE OR REPLACE FUNCTION rpt.balance_sheet(p_as_of DATE)
RETURNS TABLE (
    out_section    TEXT,
    out_group_id   BIGINT,
    out_group_name TEXT,
    out_parent_id  BIGINT,
    out_amount     NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH per_ledger AS (
        SELECT l.group_id AS gid,
               COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0) AS dr,
               COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0) AS cr
        FROM voucher_entries e
        JOIN ledgers l ON l.id = e.ledger_id
        JOIN vouchers v ON v.id = e.voucher_id
        WHERE v.voucher_date <= p_as_of
        GROUP BY l.group_id
    ),
    per_group AS (
        SELECT c.ancestor_id AS gid,
               SUM(pl.dr) AS dr,
               SUM(pl.cr) AS cr
        FROM per_ledger pl
        JOIN group_closure c ON c.descendant_id = pl.gid
        GROUP BY c.ancestor_id
    )
    SELECT 'assets', g.id, g.name, g.parent_id, pg.dr - pg.cr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'asset'

    UNION ALL
    SELECT 'liabilities', g.id, g.name, g.parent_id, pg.cr - pg.dr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'liability'

    UNION ALL
    SELECT 'equity', g.id, g.name, g.parent_id, pg.cr - pg.dr
    FROM per_group pg
    JOIN groups g ON g.id = pg.gid AND g.nature = 'equity'

    UNION ALL
    SELECT 'equity', NULL, 'Profit & Loss A/c (cumulative)', NULL,
           COALESCE(SUM(CASE WHEN g.nature = 'income' THEN pl.cr - pl.dr END), 0)
         - COALESCE(SUM(CASE WHEN g.nature = 'expense' THEN pl.dr - pl.cr END), 0)
    FROM per_ledger pl
    JOIN groups g ON g.id = pl.gid
$$;

CREATE OR REPLACE FUNCTION rpt.fy_close(p_as_of DATE, p_pl_ledger_id BIGINT)
RETURNS TABLE (
    out_bucket      TEXT,
    out_ledger_id   BIGINT,
    out_ledger_name TEXT,
    out_dc          CHAR(1),
    out_amount      NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH per_ledger AS (
        SELECT e.ledger_id,
               COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0) AS dr,
               COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0) AS cr
        FROM voucher_entries e
        JOIN vouchers v ON v.id = e.voucher_id
        JOIN ledgers l ON l.id = e.ledger_id
        JOIN groups g ON g.id = l.group_id
        WHERE v.voucher_date <= p_as_of
          AND g.nature IN ('income', 'expense')
        GROUP BY e.ledger_id
    ),
    balances AS (
        SELECT l.id AS ledger_id, l.name AS ledger_name, g.nature,
               CASE WHEN g.nature = 'income' THEN pl.cr - pl.dr
                    ELSE pl.dr - pl.cr END AS balance
        FROM per_ledger pl
        JOIN ledgers l ON l.id = pl.ledger_id
        JOIN groups g ON g.id = l.group_id
    )
    SELECT 'income', b.ledger_id, b.ledger_name,
           CASE WHEN b.balance > 0 THEN 'D' ELSE 'C' END, ABS(b.balance)
    FROM balances b
    WHERE b.nature = 'income' AND b.balance <> 0

    UNION ALL
    SELECT 'expense', b.ledger_id, b.ledger_name,
           CASE WHEN b.balance > 0 THEN 'C' ELSE 'D' END, ABS(b.balance)
    FROM balances b
    WHERE b.nature = 'expense' AND b.balance <> 0

    UNION ALL
    SELECT 'profit_loss', p_pl_ledger_id,
           (SELECT name FROM ledgers WHERE id = p_pl_ledger_id),
           CASE WHEN t.net > 0 THEN 'C' ELSE 'D' END, ABS(t.net)
    FROM (
        SELECT COALESCE(SUM(balance) FILTER (WHERE nature = 'income'), 0)
             - COALESCE(SUM(balance) FILTER (WHERE nature = 'expense'), 0) AS net
        FROM balances
    ) t
    WHERE t.net <> 0
$$;
"""

TRIGGERS_DOWNGRADE_SQL = """\
-- 0004 downgrade: restore the pre-book trigger bodies.
CREATE OR REPLACE FUNCTION trg_entries_check_date() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_date DATE;
BEGIN
    SELECT voucher_date INTO v_date FROM vouchers WHERE id = NEW.voucher_id;
    IF v_date IS NULL THEN
        RAISE EXCEPTION 'Voucher % does not exist', NEW.voucher_id;
    END IF;
    IF v_date <> NEW.voucher_date THEN
        RAISE EXCEPTION 'Entry date % does not match voucher date %', NEW.voucher_date, v_date;
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION trg_periods_delete_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_n BIGINT;
BEGIN
    IF OLD.status <> 'open' THEN
        RAISE EXCEPTION 'Only open periods can be deleted; % is %', OLD.name, OLD.status;
    END IF;
    SELECT count(*) INTO v_n FROM vouchers
    WHERE voucher_date BETWEEN OLD.starts_on AND OLD.ends_on;
    IF v_n > 0 THEN
        RAISE EXCEPTION 'Period % contains % voucher(s) and cannot be deleted',
            OLD.name, v_n;
    END IF;
    RETURN OLD;
END $$;

CREATE OR REPLACE FUNCTION trg_vouchers_check_period() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_status TEXT;
    v_name   TEXT;
BEGIN
    -- Opt-in: books that define no periods accept every date (pre-feature
    -- behavior; existing installs are unaffected until they adopt periods).
    IF NOT EXISTS (SELECT 1 FROM periods) THEN
        RETURN NEW;
    END IF;
    SELECT p.status, p.name INTO v_status, v_name
    FROM periods p
    WHERE NEW.voucher_date >= p.starts_on AND NEW.voucher_date <= p.ends_on
    FOR SHARE OF p;
    IF v_status IS NULL THEN
        RAISE EXCEPTION 'No period covers voucher date %: create the period first',
            NEW.voucher_date;
    END IF;
    IF v_status = 'closed' THEN
        RAISE EXCEPTION 'Period % is closed: its dates are final', v_name;
    END IF;
    IF v_status = 'locked' THEN
        RAISE EXCEPTION 'Period % is locked: postings are frozen until it is closed or reopened',
            v_name;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_periods_overlap ON periods;
DROP FUNCTION IF EXISTS trg_periods_no_overlap();
"""

FOLD_BACK_SQL = """\
-- 0004 downgrade: fold every book back into the implicit single book.
-- settings regains the per-book keys from the default book first.
DO $$
DECLARE
    b RECORD;
BEGIN
    SELECT * INTO b FROM books ORDER BY (slug = 'default') DESC, id LIMIT 1;
    INSERT INTO settings (key, value) VALUES
        ('company_name', b.name),
        ('base_currency', b.base_currency),
        ('fy_start_month', b.fy_start_month::TEXT)
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
END $$;

UPDATE doc_counters
SET scope_key = split_part(scope_key, ':', 2) || ':' || split_part(scope_key, ':', 3);

-- Book-scoped uniqueness and hierarchy guards out; global ones back.
ALTER TABLE groups DROP CONSTRAINT uq_groups_book_name;
ALTER TABLE groups DROP CONSTRAINT fk_groups_parent_same_book;
ALTER TABLE groups DROP CONSTRAINT uq_groups_book_id;
ALTER TABLE groups ADD CONSTRAINT groups_name_key UNIQUE (name);
ALTER TABLE groups ADD CONSTRAINT groups_parent_id_fkey
    FOREIGN KEY (parent_id) REFERENCES groups(id) ON DELETE RESTRICT;
DROP INDEX uq_groups_single_root;
CREATE UNIQUE INDEX uq_groups_single_root ON groups (is_root) WHERE is_root;

ALTER TABLE ledgers DROP CONSTRAINT uq_ledgers_book_name;
ALTER TABLE ledgers ADD CONSTRAINT ledgers_name_key UNIQUE (name);

ALTER TABLE voucher_types DROP CONSTRAINT uq_voucher_types_book_code;
ALTER TABLE voucher_types ADD CONSTRAINT voucher_types_code_key UNIQUE (code);

ALTER TABLE vouchers DROP CONSTRAINT uq_voucher_client_token_book;
ALTER TABLE vouchers ADD CONSTRAINT vouchers_client_token_key UNIQUE (client_token);

DROP INDEX idx_vouchers_date;
CREATE INDEX idx_vouchers_date ON vouchers (voucher_date);
DROP INDEX idx_vouchers_type_date;
CREATE INDEX idx_vouchers_type_date ON vouchers (voucher_type_id, voucher_date);

DROP INDEX idx_entries_ledger_date;
CREATE INDEX idx_entries_ledger_date
    ON voucher_entries (ledger_id, voucher_date) INCLUDE (dc, amount);

ALTER TABLE vouchers DROP CONSTRAINT uq_voucher_no_per_type;
ALTER TABLE vouchers ADD CONSTRAINT uq_voucher_no_per_type
    UNIQUE (voucher_type_id, voucher_no);

ALTER TABLE periods DROP CONSTRAINT uq_periods_book_name;
ALTER TABLE periods ADD CONSTRAINT periods_name_key UNIQUE (name);
ALTER TABLE periods ADD CONSTRAINT uq_periods_no_overlap EXCLUDE USING gist
    (daterange(starts_on, ends_on, '[]') WITH &&);

ALTER TABLE fx_rates DROP CONSTRAINT uq_fx_rates_pair_day;
ALTER TABLE fx_rates ADD CONSTRAINT uq_fx_rates_pair_day
    UNIQUE (from_ccy, to_ccy, valid_on);

-- The discriminator itself goes last (drops its FKs; book-scoped uniques
-- above were already removed explicitly to control their names).
ALTER TABLE groups DROP COLUMN book_id;
ALTER TABLE ledgers DROP COLUMN book_id;
ALTER TABLE voucher_types DROP COLUMN book_id;
ALTER TABLE vouchers DROP COLUMN book_id;
ALTER TABLE voucher_entries DROP COLUMN book_id;
ALTER TABLE periods DROP COLUMN book_id;
ALTER TABLE fx_rates DROP COLUMN book_id;

DROP TABLE books;
"""


def upgrade() -> None:
    op.execute(BOOKS_SQL)
    op.execute(DEFAULT_BOOK_SQL)
    op.execute(IMMUTABLE_OFF_SQL)
    op.execute(DISCRIMINATOR_SQL)
    op.execute(IMMUTABLE_ON_SQL)
    op.execute(GROUPS_SQL)
    op.execute(ENTRIES_TRIGGER_SQL)
    op.execute(PERIODS_SQL)
    for sql in (
        TRIAL_BALANCE_SQL,
        GROUP_ROLLUPS_SQL,
        PROFIT_LOSS_SQL,
        BALANCE_SHEET_SQL,
        FY_CLOSE_SQL,
    ):
        op.execute("DROP FUNCTION IF EXISTS " + _old_signature(sql))
        op.execute(sql)
    op.execute("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA rpt TO ledgerforge_app")


def _old_signature(sql: str) -> str:
    """rpt.foo(p_book_id BIGINT, …) -> the pre-0004 signature to drop first."""
    header = sql.split("RETURNS", 1)[0]
    name = header.split("FUNCTION ", 1)[1].split("(", 1)[0]
    params = header.split("(", 1)[1].rsplit(")", 1)[0]
    kept = [p.strip() for p in params.split(",") if not p.strip().startswith("p_book_id")]
    return f"{name}({', '.join(kept)})" if kept else f"{name}()"


def downgrade() -> None:
    op.execute(RPT_DOWNGRADE_SQL)
    op.execute("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA rpt TO ledgerforge_app")
    op.execute(TRIGGERS_DOWNGRADE_SQL)
    op.execute(FOLD_BACK_SQL)
