# ruff: noqa: E501 (generated: the SQL slices keep their source formatting)
"""Profit & loss: the period's own aggregation (ADR-032's certification fix).

The 10x performance certification caught the old shape answering a 10x
book's P&L in 4.6 seconds: it computed TWO as-of folds — sums(p_to) and
sums(p_from − 1) — and the earlier one can never be served by the
single-horizon balance cache (a cache row only exists for horizons at or
before the earliest open period), so every historical P&L degraded into
per-ledger LATERAL scans over the whole partitioned history.

The fix is an identity: sums over [from, to] equal sums(to) −
sums(from − 1) exactly (NUMERIC addition associates), and ONE range
aggregation — partition-pruned to the period, grouped once — answers in
milliseconds with or without the cache. ``p_fast`` stays in the signature
for compatibility and is ignored: the between scan IS the fast path for a
period statement. The as-of family (trial balance, balance sheet) keeps
the cache; the cache exists for them.

The upgrade block is db/functions.sql's ``rpt.profit_and_loss`` verbatim;
the downgrade restores the previous two-fold shape, byte-for-byte.

Revision ID: 0013_pnl_between
Revises: 0012_data_exchange
Create Date: 2026-11-09
"""

from alembic import op

revision = "0013_pnl_between"
down_revision = "0012_data_exchange"
branch_labels = None
depends_on = None

_FN = """
CREATE OR REPLACE FUNCTION rpt.profit_and_loss(
    p_book_id BIGINT, p_from DATE, p_to DATE, p_fast BOOLEAN DEFAULT false)
RETURNS TABLE (
    out_section   TEXT,
    out_bucket    TEXT,
    out_group_id  BIGINT,
    out_group_name TEXT,
    out_amount    NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    -- The period's own movement, directly: sums over [p_from, p_to] are
    -- mathematically identical to sums(p_to) − sums(p_from − 1) (exact
    -- NUMERIC addition associates), and ONE range aggregation is an order
    -- of magnitude cheaper than TWO as-of folds — especially the earlier
    -- one, which no single-horizon balance cache can ever serve (ADR-032's
    -- 10x certification caught the old shape answering a 10x book's P&L in
    -- 4.6s; this shape does it in milliseconds, cache or no cache).
    -- p_fast is accepted for signature compatibility and ignored: the
    -- between scan IS the fast path for a period statement.
    WITH per_ledger AS (
        SELECT l.group_id AS gid,
               COALESCE(SUM(e.amount) FILTER (WHERE e.dc = 'D'), 0) AS dr,
               COALESCE(SUM(e.amount) FILTER (WHERE e.dc = 'C'), 0) AS cr
        FROM voucher_entries e
        JOIN vouchers v ON v.id = e.voucher_id
        JOIN ledgers l ON l.id = e.ledger_id
        WHERE e.book_id = p_book_id
          AND e.voucher_date >= p_from
          AND e.voucher_date <= p_to
          AND v.voucher_type_id NOT IN
              (SELECT vt.id FROM voucher_types vt WHERE vt.code = 'FYC')
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
    ORDER BY 1, 2, 3 NULLS LAST
$$;
"""

_PREV = """
CREATE OR REPLACE FUNCTION rpt.profit_and_loss(
    p_book_id BIGINT, p_from DATE, p_to DATE, p_fast BOOLEAN DEFAULT false)
RETURNS TABLE (
    out_section   TEXT,
    out_bucket    TEXT,
    out_group_id  BIGINT,
    out_group_name TEXT,
    out_amount    NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH s_to AS (
        SELECT * FROM rpt._ledger_sums_upto(p_book_id, p_to, true, p_fast)
    ),
    s_prev AS (
        SELECT * FROM rpt._ledger_sums_upto(p_book_id, p_from - 1, true, p_fast)
    ),
    per_ledger AS (
        SELECT l.group_id AS gid,
               COALESCE(t.out_dr, 0) - COALESCE(p.out_dr, 0) AS dr,
               COALESCE(t.out_cr, 0) - COALESCE(p.out_cr, 0) AS cr
        FROM s_to t
        FULL JOIN s_prev p ON p.out_ledger_id = t.out_ledger_id
        JOIN ledgers l ON l.id = COALESCE(t.out_ledger_id, p.out_ledger_id)
        WHERE COALESCE(t.out_dr, 0) - COALESCE(p.out_dr, 0) <> 0
           OR COALESCE(t.out_cr, 0) - COALESCE(p.out_cr, 0) <> 0
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
    ORDER BY 1, 2, 3 NULLS LAST
$$;
"""


def upgrade() -> None:
    op.execute(_FN)


def downgrade() -> None:
    op.execute(_PREV)
