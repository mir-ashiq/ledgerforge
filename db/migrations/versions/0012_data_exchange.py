# ruff: noqa: E501 (generated: the SQL slices keep their source formatting)
"""Personal API tokens and the analyst surface (ADR-030).

The sixth core-schema change since 0006 — two objects, deliberately small:

- ``api_tokens`` — the desktop's non-OIDC answer for integrations
  (ADR-030 §1): long-lived, scoped, revocable credentials. The raw token
  is returned once and never stored; the table holds its SHA-256, the
  role ceiling, the books scope (the ``books``-claim semantics), an
  optional expiry and the revocation timestamp. A credential store, not
  financial history — mint, touch and revoke are all its writes.
- The ``views`` schema — the analyst's read-only SQL contract (ADR-030
  §1): seven flat projections (books, groups, ledgers, vouchers,
  voucher_lines with cost-centre names, ledger_monthly, ledger_balances)
  plus the NOLOGIN role ``ledgerforge_readonly`` holding SELECT on the
  views and nothing else. Cross-book by construction — pivot tables don't
  know books; an operator who doesn't want that never mints the analyst
  login. Amounts stay NUMERIC end to end — the ODBC driver receives
  exact decimals, never floats (Law 3).

The upgrade blocks are generated from db/schema.sql verbatim; the
downgrade drops everything this revision created.

Revision ID: 0012_data_exchange
Revises: 0011_payroll_depth
Create Date: 2026-11-08
"""

from alembic import op

revision = "0012_data_exchange"
down_revision = "0011_payroll_depth"
branch_labels = None
depends_on = None

UPGRADE_SQL = """\
-- ----------------------------------------------------------------------------
-- Personal API tokens (0012/ADR-030) — the desktop's non-OIDC answer for
-- integrations: long-lived, scoped, revocable credentials. The raw token
-- (`lft_…`, 32 urlsafe chars) is returned exactly once at mint and never
-- stored; the table holds its SHA-256 (a high-entropy random token needs
-- no salted KDF — a lookup hash is the honest construction), the role
-- ceiling, the books scope (the `books`-claim semantics: ["*"] or ids),
-- an optional expiry, and the revocation timestamp. Mutable by design —
-- a token is a credential, not financial history; revocation is a soft
-- verdict the row remembers (append-only is the ledgers' law, not the
-- security store's).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_tokens (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         TEXT NOT NULL CHECK (name <> ''),
    token_prefix TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    role         TEXT NOT NULL CHECK (role IN ('reader', 'bookkeeper', 'admin')),
    books        JSONB NOT NULL DEFAULT '["*"]'::jsonb,
    expires_at   TIMESTAMPTZ,
    created_by   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens (token_hash);

GRANT SELECT, INSERT, UPDATE ON api_tokens TO ledgerforge_app;
GRANT USAGE ON SEQUENCE api_tokens_id_seq TO ledgerforge_app;

-- ----------------------------------------------------------------------------
-- The analyst surface (0012/ADR-030): read-only SQL views + a named role
-- for ODBC. The `views` schema is the analyst's contract — flat, joinable,
-- stable, and deliberately cross-book (pivot tables don't know books; an
-- operator who doesn't want that never mints the analyst login). The
-- ledgerforge_readonly role holds SELECT on the views and nothing else:
-- the base tables stay the engine's. Amounts stay NUMERIC end to end —
-- the ODBC driver receives exact decimals, never floats (Law 3).
-- ----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS views;

CREATE OR REPLACE VIEW views.books AS
SELECT b.id, b.name, b.slug, b.base_currency, b.fy_start_month,
       b.is_archived, b.created_at
FROM public.books b;

CREATE OR REPLACE VIEW views.groups AS
SELECT bk.name AS book, g.id, g.name, g.code, parent.name AS parent,
       g.nature, g.affects_gross, g.is_root
FROM public.groups g
JOIN public.books bk ON bk.id = g.book_id
LEFT JOIN public.groups parent ON parent.id = g.parent_id;

CREATE OR REPLACE VIEW views.ledgers AS
SELECT bk.name AS book, l.id, l.name, l.code, g.name AS "group",
       l.is_system, l.created_at
FROM public.ledgers l
JOIN public.groups g ON g.id = l.group_id
JOIN public.books bk ON bk.id = l.book_id;

CREATE OR REPLACE VIEW views.vouchers AS
SELECT bk.name AS book, v.id, v.voucher_no, v.voucher_date,
       t.code AS voucher_type, t.name AS type_name, v.narration,
       v.reversal_of, v.posted_at, v.uuid
FROM public.vouchers v
JOIN public.books bk ON bk.id = v.book_id
JOIN public.voucher_types t ON t.id = v.voucher_type_id;

CREATE OR REPLACE VIEW views.voucher_lines AS
SELECT bk.name AS book, v.voucher_no, v.voucher_date,
       t.code AS voucher_type, l.name AS ledger, g.name AS "group",
       e.line_no, e.dc, e.amount, e.currency, e.amount_fx, e.fx_rate,
       e.narration AS line_narration, v.narration,
       cc.name AS cost_centre, ca.amount AS cost_centre_amount
FROM public.voucher_entries e
JOIN public.vouchers v ON v.id = e.voucher_id
JOIN public.voucher_types t ON t.id = v.voucher_type_id
JOIN public.ledgers l ON l.id = e.ledger_id
JOIN public.groups g ON g.id = l.group_id
JOIN public.books bk ON bk.id = v.book_id
LEFT JOIN public.cost_allocations ca ON ca.entry_id = e.id
LEFT JOIN public.cost_centres cc ON cc.id = ca.centre_id;

CREATE OR REPLACE VIEW views.ledger_monthly AS
SELECT bk.name AS book, l.name AS ledger, g.name AS "group",
       date_trunc('month', e.voucher_date)::date AS "month",
       SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END) AS debit,
       SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END) AS credit
FROM public.voucher_entries e
JOIN public.ledgers l ON l.id = e.ledger_id
JOIN public.groups g ON g.id = l.group_id
JOIN public.books bk ON bk.id = e.book_id
GROUP BY bk.name, l.name, g.name, date_trunc('month', e.voucher_date)::date;

CREATE OR REPLACE VIEW views.ledger_balances AS
SELECT bk.name AS book, l.name AS ledger, g.name AS "group",
       SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END) AS closing_debit,
       SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END) AS closing_credit,
       SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE -e.amount END) AS closing_net
FROM public.voucher_entries e
JOIN public.ledgers l ON l.id = e.ledger_id
JOIN public.groups g ON g.id = l.group_id
JOIN public.books bk ON bk.id = e.book_id
GROUP BY bk.name, l.name, g.name;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ledgerforge_readonly') THEN
        CREATE ROLE ledgerforge_readonly NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA views TO ledgerforge_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA views TO ledgerforge_readonly;
-- The application never reads the views (it owns the tables); the
-- analyst role never reads the tables. The one exception is deliberate:
-- ledgerforge_app gets USAGE on the schema so its own diagnostics can
-- prove the views answer — SELECT stays the analyst's alone.
GRANT USAGE ON SCHEMA views TO ledgerforge_app;


-- The shape conversions (ADR-018) swap voucher_entries underneath the
-- three views that read it; this revision teaches them to lift the views
-- for the swap and put them back verbatim (existing deployments get the
-- updated functions through this migration; fresh bootstraps through
-- db/functions.sql). The blocks below are generated from db/functions.sql
-- verbatim.

CREATE OR REPLACE FUNCTION ops.recreate_analyst_views()
RETURNS void
LANGUAGE sql AS $$
    CREATE OR REPLACE VIEW views.voucher_lines AS
    SELECT bk.name AS book, v.voucher_no, v.voucher_date,
           t.code AS voucher_type, l.name AS ledger, g.name AS "group",
           e.line_no, e.dc, e.amount, e.currency, e.amount_fx, e.fx_rate,
           e.narration AS line_narration, v.narration,
           cc.name AS cost_centre, ca.amount AS cost_centre_amount
    FROM public.voucher_entries e
    JOIN public.vouchers v ON v.id = e.voucher_id
    JOIN public.voucher_types t ON t.id = v.voucher_type_id
    JOIN public.ledgers l ON l.id = e.ledger_id
    JOIN public.groups g ON g.id = l.group_id
    JOIN public.books bk ON bk.id = v.book_id
    LEFT JOIN public.cost_allocations ca ON ca.entry_id = e.id
    LEFT JOIN public.cost_centres cc ON cc.id = ca.centre_id;

    CREATE OR REPLACE VIEW views.ledger_monthly AS
    SELECT bk.name AS book, l.name AS ledger, g.name AS "group",
           date_trunc('month', e.voucher_date)::date AS "month",
           SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END) AS debit,
           SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END) AS credit
    FROM public.voucher_entries e
    JOIN public.ledgers l ON l.id = e.ledger_id
    JOIN public.groups g ON g.id = l.group_id
    JOIN public.books bk ON bk.id = e.book_id
    GROUP BY bk.name, l.name, g.name, date_trunc('month', e.voucher_date)::date;

    CREATE OR REPLACE VIEW views.ledger_balances AS
    SELECT bk.name AS book, l.name AS ledger, g.name AS "group",
           SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END) AS closing_debit,
           SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END) AS closing_credit,
           SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE -e.amount END) AS closing_net
    FROM public.voucher_entries e
    JOIN public.ledgers l ON l.id = e.ledger_id
    JOIN public.groups g ON g.id = l.group_id
    JOIN public.books bk ON bk.id = e.book_id
    GROUP BY bk.name, l.name, g.name;

    GRANT SELECT ON views.voucher_lines, views.ledger_monthly, views.ledger_balances
        TO ledgerforge_readonly;
$$;
CREATE OR REPLACE FUNCTION ops.partition_entries_by_year(p_headroom INT DEFAULT 2)
RETURNS TABLE (out_step TEXT, out_detail TEXT)
LANGUAGE plpgsql AS $$
DECLARE
    v_y0        INT;
    v_y1        INT;
    v_rows      BIGINT;
    v_max_id    BIGINT;
    v_this_year INT := EXTRACT(YEAR FROM current_date)::int;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('ops.partition_entries_by_year', 0));

    IF (SELECT c.relkind FROM pg_class c
        WHERE c.oid = 'public.voucher_entries'::regclass) <> 'r' THEN
        RAISE EXCEPTION 'voucher_entries is not a plain table — already partitioned?';
    END IF;
    IF p_headroom < 0 OR p_headroom > 20 THEN
        RAISE EXCEPTION 'p_headroom must be between 0 and 20, got %', p_headroom;
    END IF;

    SELECT COALESCE(min(EXTRACT(YEAR FROM e.voucher_date)::int), v_this_year),
           COALESCE(max(EXTRACT(YEAR FROM e.voucher_date)::int), v_this_year)
    INTO v_y0, v_y1
    FROM voucher_entries e;

    RETURN QUERY SELECT 'shadow', format(
        'creating the partitioned shadow (data years %s..%s, headroom to %s)',
        v_y0, v_y1, greatest(v_y1, v_this_year) + p_headroom);

    EXECUTE $ddl$
        CREATE TABLE public.voucher_entries_part (
            id           BIGINT GENERATED ALWAYS AS IDENTITY,
            voucher_id   BIGINT NOT NULL REFERENCES vouchers(id) ON DELETE RESTRICT,
            line_no      INT NOT NULL CHECK (line_no > 0),
            ledger_id    BIGINT NOT NULL REFERENCES ledgers(id) ON DELETE RESTRICT,
            book_id      BIGINT NOT NULL REFERENCES books(id),
            dc           CHAR(1) NOT NULL CHECK (dc IN ('D', 'C')),
            amount       NUMERIC(26, 8) NOT NULL CHECK (amount > 0),
            voucher_date DATE NOT NULL,
            narration    TEXT,
            currency     CHAR(3) REFERENCES currencies(code),
            amount_fx    NUMERIC(26, 8)
                         CONSTRAINT chk_entry_amount_fx_positive CHECK (amount_fx > 0),
            fx_rate      NUMERIC(30, 10)
                         CONSTRAINT chk_entry_fx_rate_positive CHECK (fx_rate > 0),
            CONSTRAINT voucher_entries_part_pkey PRIMARY KEY (id, voucher_date),
            CONSTRAINT uq_part_entry_line UNIQUE (voucher_id, line_no, voucher_date),
            CONSTRAINT chk_entry_fx_consistent CHECK (
                (currency IS NULL AND amount_fx IS NULL AND fx_rate IS NULL)
                OR (
                    currency IS NOT NULL
                    AND amount_fx IS NOT NULL
                    AND fx_rate IS NOT NULL
                    AND amount = round(amount_fx * fx_rate, 8)
                )
            )
        ) PARTITION BY RANGE (voucher_date)
    $ddl$;

    PERFORM ops._ensure_year_partitions(
        'public.voucher_entries_part'::regclass,
        v_y0, greatest(v_y1, v_this_year) + p_headroom);

    RETURN QUERY SELECT 'copy', 'copying rows in voucher_date order (BRIN-friendly)';

    INSERT INTO public.voucher_entries_part
        (id, voucher_id, line_no, ledger_id, book_id, dc, amount,
         voucher_date, narration, currency, amount_fx, fx_rate)
    OVERRIDING SYSTEM VALUE
    SELECT id, voucher_id, line_no, ledger_id, book_id, dc, amount,
           voucher_date, narration, currency, amount_fx, fx_rate
    FROM public.voucher_entries
    ORDER BY voucher_date, id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    SELECT max(id) INTO v_max_id FROM public.voucher_entries_part;
    IF v_max_id IS NOT NULL THEN
        PERFORM setval(pg_get_serial_sequence('public.voucher_entries_part', 'id'), v_max_id);
    END IF;

    RETURN QUERY SELECT 'swap', format(
        '%s rows, max id %s — atomic rename-and-drop', v_rows, COALESCE(v_max_id::text, 'n/a'));

    -- Cost allocations reference entries (0007/ADR-025); the partitioned
    -- shape's PK is (id, voucher_date) — an FK on id alone cannot exist
    -- there. It comes off before the swap; trg_cost_allocations_check
    -- keeps the integrity on both shapes; ops.unpartition_entries re-adds
    -- it when the plain shape returns.
    ALTER TABLE public.cost_allocations DROP CONSTRAINT IF EXISTS cost_allocations_entry_id_fkey;

    -- The analyst views (0012/ADR-030) sit on voucher_entries; the swap
    -- drops the original table underneath them, so they come off first
    -- and go back on after (ops.recreate_analyst_views owns the verbatim
    -- definitions — the views are the schema's own objects, never data).
    DROP VIEW IF EXISTS views.voucher_lines, views.ledger_monthly, views.ledger_balances;

    ALTER TABLE public.voucher_entries RENAME TO voucher_entries_plain;
    ALTER TABLE public.voucher_entries_part RENAME TO voucher_entries;
    DROP TABLE public.voucher_entries_plain;

    PERFORM ops.recreate_analyst_views();

    CREATE TRIGGER trg_entries_date
        BEFORE INSERT ON public.voucher_entries
        FOR EACH ROW EXECUTE FUNCTION trg_entries_check_date();
    CREATE CONSTRAINT TRIGGER trg_entries_balanced
        AFTER INSERT ON public.voucher_entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION trg_entries_check_balance();
    CREATE TRIGGER trg_voucher_entries_immutable
        BEFORE UPDATE OR DELETE ON public.voucher_entries
        FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();

    CREATE INDEX idx_entries_ledger_date
        ON public.voucher_entries (book_id, ledger_id, voucher_date) INCLUDE (dc, amount);
    CREATE INDEX idx_entries_brin_date
        ON public.voucher_entries USING brin (voucher_date);

    CREATE TRIGGER trg_vouchers_entry_partition
        BEFORE INSERT ON public.vouchers
        FOR EACH ROW EXECUTE FUNCTION ops.ensure_entry_partition_for_voucher();

    GRANT SELECT, INSERT ON public.voucher_entries TO ledgerforge_app;
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO ledgerforge_app;

    ANALYZE public.voucher_entries;

    RETURN QUERY SELECT 'done', 'voucher_entries is now partitioned by year (range, voucher_date)';
END $$;

-- The mirror image: partitioned voucher_entries -> the plain 0001 shape.
-- Balance-cache rows are shape-agnostic and survive untouched.
-- The analyst views (0012/ADR-030) depend on voucher_entries; the swap
-- drops the original underneath them — they come off first (see the
-- conversion) and this recreates them verbatim after either direction.
CREATE OR REPLACE FUNCTION ops.recreate_analyst_views()
RETURNS void
LANGUAGE sql AS $$
    CREATE OR REPLACE VIEW views.voucher_lines AS
    SELECT bk.name AS book, v.voucher_no, v.voucher_date,
           t.code AS voucher_type, l.name AS ledger, g.name AS "group",
           e.line_no, e.dc, e.amount, e.currency, e.amount_fx, e.fx_rate,
           e.narration AS line_narration, v.narration,
           cc.name AS cost_centre, ca.amount AS cost_centre_amount
    FROM public.voucher_entries e
    JOIN public.vouchers v ON v.id = e.voucher_id
    JOIN public.voucher_types t ON t.id = v.voucher_type_id
    JOIN public.ledgers l ON l.id = e.ledger_id
    JOIN public.groups g ON g.id = l.group_id
    JOIN public.books bk ON bk.id = v.book_id
    LEFT JOIN public.cost_allocations ca ON ca.entry_id = e.id
    LEFT JOIN public.cost_centres cc ON cc.id = ca.centre_id;

    CREATE OR REPLACE VIEW views.ledger_monthly AS
    SELECT bk.name AS book, l.name AS ledger, g.name AS "group",
           date_trunc('month', e.voucher_date)::date AS "month",
           SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END) AS debit,
           SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END) AS credit
    FROM public.voucher_entries e
    JOIN public.ledgers l ON l.id = e.ledger_id
    JOIN public.groups g ON g.id = l.group_id
    JOIN public.books bk ON bk.id = e.book_id
    GROUP BY bk.name, l.name, g.name, date_trunc('month', e.voucher_date)::date;

    CREATE OR REPLACE VIEW views.ledger_balances AS
    SELECT bk.name AS book, l.name AS ledger, g.name AS "group",
           SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END) AS closing_debit,
           SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END) AS closing_credit,
           SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE -e.amount END) AS closing_net
    FROM public.voucher_entries e
    JOIN public.ledgers l ON l.id = e.ledger_id
    JOIN public.groups g ON g.id = l.group_id
    JOIN public.books bk ON bk.id = e.book_id
    GROUP BY bk.name, l.name, g.name;

    GRANT SELECT ON views.voucher_lines, views.ledger_monthly, views.ledger_balances
        TO ledgerforge_readonly;
$$;
CREATE OR REPLACE FUNCTION ops.unpartition_entries()
RETURNS TABLE (out_step TEXT, out_detail TEXT)
LANGUAGE plpgsql AS $$
DECLARE
    v_rows   BIGINT;
    v_max_id BIGINT;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('ops.partition_entries_by_year', 0));

    IF (SELECT c.relkind FROM pg_class c
        WHERE c.oid = 'public.voucher_entries'::regclass) <> 'p' THEN
        RAISE EXCEPTION 'voucher_entries is not partitioned — nothing to undo';
    END IF;

    RETURN QUERY SELECT 'shadow', 'creating the plain shadow (the original 0001 shape)';

    EXECUTE $ddl$
        CREATE TABLE public.voucher_entries_plain (
            id           BIGINT GENERATED ALWAYS AS IDENTITY,
            voucher_id   BIGINT NOT NULL REFERENCES vouchers(id) ON DELETE RESTRICT,
            line_no      INT NOT NULL CHECK (line_no > 0),
            ledger_id    BIGINT NOT NULL REFERENCES ledgers(id) ON DELETE RESTRICT,
            book_id      BIGINT NOT NULL REFERENCES books(id),
            dc           CHAR(1) NOT NULL CHECK (dc IN ('D', 'C')),
            amount       NUMERIC(26, 8) NOT NULL CHECK (amount > 0),
            voucher_date DATE NOT NULL,
            narration    TEXT,
            currency     CHAR(3) REFERENCES currencies(code),
            amount_fx    NUMERIC(26, 8)
                         CONSTRAINT chk_entry_amount_fx_positive CHECK (amount_fx > 0),
            fx_rate      NUMERIC(30, 10)
                         CONSTRAINT chk_entry_fx_rate_positive CHECK (fx_rate > 0),
            CONSTRAINT voucher_entries_plain_pkey PRIMARY KEY (id),
            CONSTRAINT uq_plain_entry_line UNIQUE (voucher_id, line_no),
            CONSTRAINT chk_entry_fx_consistent CHECK (
                (currency IS NULL AND amount_fx IS NULL AND fx_rate IS NULL)
                OR (
                    currency IS NOT NULL
                    AND amount_fx IS NOT NULL
                    AND fx_rate IS NOT NULL
                    AND amount = round(amount_fx * fx_rate, 8)
                )
            )
        )
    $ddl$;

    INSERT INTO public.voucher_entries_plain
        (id, voucher_id, line_no, ledger_id, book_id, dc, amount,
         voucher_date, narration, currency, amount_fx, fx_rate)
    OVERRIDING SYSTEM VALUE
    SELECT id, voucher_id, line_no, ledger_id, book_id, dc, amount,
           voucher_date, narration, currency, amount_fx, fx_rate
    FROM public.voucher_entries
    ORDER BY id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    SELECT max(id) INTO v_max_id FROM public.voucher_entries_plain;
    IF v_max_id IS NOT NULL THEN
        PERFORM setval(pg_get_serial_sequence('public.voucher_entries_plain', 'id'), v_max_id);
    END IF;

    DROP TRIGGER IF EXISTS trg_vouchers_entry_partition ON public.vouchers;

    RETURN QUERY SELECT 'swap', format('%s rows — atomic rename-and-drop', v_rows);

    -- the analyst views come off for the swap (see ops.recreate_analyst_views)
    DROP VIEW IF EXISTS views.voucher_lines, views.ledger_monthly, views.ledger_balances;

    ALTER TABLE public.voucher_entries RENAME TO voucher_entries_old;
    ALTER TABLE public.voucher_entries_plain RENAME TO voucher_entries;
    DROP TABLE public.voucher_entries_old;

    PERFORM ops.recreate_analyst_views();

    CREATE TRIGGER trg_entries_date
        BEFORE INSERT ON public.voucher_entries
        FOR EACH ROW EXECUTE FUNCTION trg_entries_check_date();
    CREATE CONSTRAINT TRIGGER trg_entries_balanced
        AFTER INSERT ON public.voucher_entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION trg_entries_check_balance();
    CREATE TRIGGER trg_voucher_entries_immutable
        BEFORE UPDATE OR DELETE ON public.voucher_entries
        FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();

    CREATE INDEX idx_entries_ledger_date
        ON public.voucher_entries (book_id, ledger_id, voucher_date) INCLUDE (dc, amount);

    -- The plain shape is back: the allocation→entry FK returns with it
    -- (it cannot exist on the partitioned shape — see partition_entries_
    -- by_year). Guarded so a DB that never had it (partitioned before
    -- 0007) simply gains it now, with existing rows validated by ADD's
    -- default check — allocations always had real entries (the trigger).
    -- A tagged DO (not bare dollar-dollar): this block sits inside the
    -- function's own dollar-quoted body.
    DO $fk$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'cost_allocations_entry_id_fkey'
              AND conrelid = 'public.cost_allocations'::regclass) THEN
            ALTER TABLE public.cost_allocations
                ADD CONSTRAINT cost_allocations_entry_id_fkey
                FOREIGN KEY (entry_id) REFERENCES public.voucher_entries(id) ON DELETE RESTRICT;
        END IF;
    END $fk$;

    GRANT SELECT, INSERT ON public.voucher_entries TO ledgerforge_app;
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO ledgerforge_app;

    ANALYZE public.voucher_entries;

    RETURN QUERY SELECT 'done', 'plain shape restored; balance-cache rows were shape-agnostic and survive';
END $$;

-- The admin screen: shape, partition count, entry estimate (catalog
-- statistics — never a count(*) on a hot table) and cache state per book.
CREATE OR REPLACE FUNCTION ops.scale_out_status()
RETURNS TABLE (out_name TEXT, out_value TEXT)
LANGUAGE sql STABLE AS $$
    SELECT 'entries_shape',
           CASE c.relkind
                WHEN 'p' THEN 'partitioned by year (range, voucher_date)'
                WHEN 'r' THEN 'plain'
                ELSE c.relkind::text END
    FROM pg_class c WHERE c.oid = 'public.voucher_entries'::regclass
    UNION ALL
    SELECT 'partition_count', count(*)::text
    FROM pg_inherits i WHERE i.inhparent = 'public.voucher_entries'::regclass
    UNION ALL
    SELECT 'entry_estimate', c.reltuples::bigint::text || ' rows (catalog estimate)'
    FROM pg_class c WHERE c.oid = 'public.voucher_entries'::regclass
    UNION ALL
    SELECT 'cache_rows', count(*)::text FROM rpt.balance_cache
    UNION ALL
    SELECT 'book: ' || b.name,
           'cache rows ' || count(c.ledger_id)
           || ', horizon ' || COALESCE(min(c.horizon)::text, 'none')
           || ', refreshed ' || COALESCE(max(c.refreshed_at)::text, 'never')
    FROM books b
    LEFT JOIN rpt.balance_cache c ON c.book_id = b.id
    GROUP BY b.name
    ORDER BY 1
$$;
"""

DOWNGRADE_SQL = """\
-- Privileges first: REVOKEs need the objects to still exist.
REVOKE USAGE ON SCHEMA views FROM ledgerforge_app;
REVOKE USAGE ON SCHEMA views FROM ledgerforge_readonly;
REVOKE SELECT ON ALL TABLES IN SCHEMA views FROM ledgerforge_readonly;
DROP VIEW IF EXISTS views.ledger_balances, views.ledger_monthly,
    views.voucher_lines, views.vouchers, views.ledgers, views.groups, views.books;
DROP SCHEMA IF EXISTS views;

REVOKE SELECT, INSERT, UPDATE ON api_tokens FROM ledgerforge_app;
REVOKE USAGE ON SEQUENCE api_tokens_id_seq FROM ledgerforge_app;
DROP TABLE IF EXISTS api_tokens;
-- ledgerforge_readonly is the migration's own role; the DB owner may have
-- granted it to logins — dropping it would cascade-deny, so it stays (the
-- documented downgrade note: drop it by hand once its members are gone).
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
