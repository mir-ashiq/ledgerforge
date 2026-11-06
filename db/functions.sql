-- ============================================================================
-- LedgerForge — reporting engine (SQL functions, schema `rpt`)
-- ----------------------------------------------------------------------------
-- Callable as raw SQL, e.g.:
--   SELECT * FROM rpt.trial_balance('2026-11-06');
--   SELECT * FROM rpt.profit_and_loss('2026-04-01', '2027-03-31');
--   SELECT * FROM rpt.balance_sheet('2026-11-06');
-- All aggregation is exact NUMERIC arithmetic; all rollups are closure-table
-- joins (ADR-001). Idempotent (CREATE OR REPLACE).
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS rpt;

GRANT USAGE ON SCHEMA rpt TO ledgerforge_app;

-- ----------------------------------------------------------------------------
-- Trial Balance: per-ledger debit/credit/closing as of a date.
-- Each ledger is classified by its OWN group's nature (single counting).
-- ----------------------------------------------------------------------------
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

-- ----------------------------------------------------------------------------
-- Group rollups: every group's subtree debit/credit as of a date, in one
-- closure-table pass (the query the closure model exists for).
-- ----------------------------------------------------------------------------
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

-- ----------------------------------------------------------------------------
-- Profit & Loss: trading (direct, affects_gross) and indirect sections, with
-- gross and net profit rows. Ledger-level classification for totals; group
-- rows are subtree rollups for display. Financial-year close sweeps (voucher
-- type FYC) are excluded: they transfer results to Profit & Loss A/c, they
-- are not earnings — without this, the year after a close would report the
-- sweep as a loss (ADR-008).
-- ----------------------------------------------------------------------------
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
            COALESCE(SUM(CASE WHEN nature = 'income'  AND affects_gross THEN cr - dr END), 0) AS direct_income,
            COALESCE(SUM(CASE WHEN nature = 'expense' AND affects_gross THEN dr - cr END), 0) AS direct_expense,
            COALESCE(SUM(CASE WHEN nature = 'income'  AND NOT affects_gross THEN cr - dr END), 0) AS indirect_income,
            COALESCE(SUM(CASE WHEN nature = 'expense' AND NOT affects_gross THEN dr - cr END), 0) AS indirect_expense
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

-- ----------------------------------------------------------------------------
-- Balance Sheet: assets / liabilities / equity group rollups as of a date,
-- plus the cumulative profit-and-loss plug under equity. Section totals are
-- computed by the API from rpt.trial_balance (ledger-level, single-counted);
-- group rows here are subtree rollups for the display tree.
-- ----------------------------------------------------------------------------
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

-- ----------------------------------------------------------------------------
-- Financial-year close: the exact sweep that zeroes the year (issue #5,
-- ADR-008). For every income/expense ledger with a non-zero closing balance
-- as of p_as_of, the reversing line; the net difference lands on the
-- Profit & Loss A/c ledger (p_pl_ledger_id). Balanced by construction —
-- every line is an ABS of a balance and the P&L line absorbs the algebraic
-- sum, so Law 1 holds exactly. The close action posts these rows verbatim as
-- an FYC voucher dated the first day of the NEXT period.
-- ----------------------------------------------------------------------------
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

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA rpt TO ledgerforge_app;
