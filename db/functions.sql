-- ============================================================================
-- LedgerForge — reporting engine (SQL functions, schema `rpt`) and the
-- scale-out machinery (schema `ops`) (ADR-018)
-- ----------------------------------------------------------------------------
-- Callable as raw SQL, e.g. (book 1 = the id from GET /api/v1/books):
--   SELECT * FROM rpt.trial_balance(1, '2026-11-06');
--   SELECT * FROM rpt.profit_and_loss(1, '2026-04-01', '2027-03-31');
--   SELECT * FROM rpt.balance_sheet(1, '2026-11-06');
-- All aggregation is exact NUMERIC arithmetic; all rollups are closure-table
-- joins (ADR-001); every function is book-scoped (ADR-009, migration 0004).
-- Idempotent (CREATE OR REPLACE).
--
-- ADR-018 adds the report scale-out, opt-in on both axes:
--   * p_fast on the read path — the balance cache (rpt.balance_cache) makes
--     reads faster, never wrong; a cold or stale cache is a full scan, not a
--     wrong number. Correctness rests on ADR-008's posting gate: once a book
--     defines periods, only OPEN-period dates post, so everything below the
--     horizon (min open starts_on) is frozen forever.
--   * ops.partition_entries_by_year() — the operator-initiated conversion of
--     voucher_entries to yearly range partitions (books past ~1M entries).
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS rpt;

GRANT USAGE ON SCHEMA rpt TO ledgerforge_app;

-- ----------------------------------------------------------------------------
-- The balance cache (ADR-018): one row per (book, ledger). The row asserts
-- "dr/cr are the exact sums of this ledger's entries with voucher_date <
-- horizon, and no entry dated below the horizon can ever be posted again."
-- That assertion is ADR-008's posting gate: below the horizon lie closed
-- periods (terminal state) and uncovered dates (refused by the gate). The
-- one administrative way frozen dates become writable again — creating or
-- reopening an open period below the horizon — deletes the affected rows in
-- the same transaction (trg_periods_cache below), so the cache can only ever
-- be slow, never wrong.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rpt.balance_cache (
    book_id      BIGINT NOT NULL REFERENCES books(id),
    ledger_id    BIGINT NOT NULL REFERENCES ledgers(id),
    horizon      DATE NOT NULL,
    dr           NUMERIC(26, 8) NOT NULL,
    cr           NUMERIC(26, 8) NOT NULL,
    -- the same sums with FYC close-sweep vouchers excluded: a period's FYC
    -- posts on the first day of the NEXT period, so it is in the open tail
    -- at posting time — but once that period closes too, the sweep enters
    -- the cached zone. Trial Balance/Balance Sheet need the inclusive sums;
    -- P&L needs the exclusive ones; the cache carries both (ADR-018).
    dr_no_fyc    NUMERIC(26, 8) NOT NULL,
    cr_no_fyc    NUMERIC(26, 8) NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_balance_cache PRIMARY KEY (book_id, ledger_id)
);

GRANT SELECT ON rpt.balance_cache TO ledgerforge_app;

-- The cache invalidation rule: a period that is (or just became) open makes
-- its dates writable again. Rows that had already folded those dates in die
-- now, inside the period's own transaction. Locked→closed transitions only
-- freeze more territory (never invalidate); open→locked never makes anything
-- writable; metadata-only updates don't fire (UPDATE OF status).
CREATE OR REPLACE FUNCTION rpt.invalidate_balance_cache() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'open' THEN
        DELETE FROM rpt.balance_cache
        WHERE book_id = NEW.book_id
          AND horizon > NEW.starts_on;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_periods_cache ON periods;
CREATE TRIGGER trg_periods_cache
    AFTER INSERT OR UPDATE OF status ON periods
    FOR EACH ROW EXECUTE FUNCTION rpt.invalidate_balance_cache();

-- Refresh the cache (all books, or one). SECURITY DEFINER so the app role
-- can drive it through the admin API without direct write grants on the
-- cache; advisory-locked per book so concurrent refreshes serialize; fully
-- idempotent. Existing rows are extended incrementally — the delta zone
-- [old horizon, new horizon) was writable at the last refresh and is frozen
-- now, so old + delta is exact. Both sum variants are maintained: the
-- inclusive one (Trial Balance / Balance Sheet) and the FYC-exclusive one
-- (P&L — an FYC sweep posts on the first day of the next period, so it
-- enters the cached zone when that period closes in turn).
CREATE OR REPLACE FUNCTION rpt.refresh_balance_cache(p_book_id BIGINT DEFAULT NULL)
RETURNS TABLE (
    out_book_id BIGINT,
    out_ledgers BIGINT,
    out_horizon DATE,
    out_action  TEXT
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = rpt, public AS $$
DECLARE
    b         BIGINT;
    v_horizon DATE;
    v_count   BIGINT;
BEGIN
    FOR b IN SELECT bk.id FROM books bk
             WHERE p_book_id IS NULL OR bk.id = p_book_id
             ORDER BY bk.id LOOP
        PERFORM pg_advisory_xact_lock(
            hashtextextended('rpt.refresh_balance_cache:' || b::text, 0));

        -- The horizon: the earliest date that can still receive a posting.
        SELECT CASE
            WHEN NOT EXISTS (SELECT 1 FROM periods p WHERE p.book_id = b) THEN NULL
            WHEN EXISTS (SELECT 1 FROM periods p WHERE p.book_id = b AND p.status = 'open')
                THEN (SELECT min(p.starts_on) FROM periods p
                      WHERE p.book_id = b AND p.status = 'open')
            ELSE '9999-12-31'::date
        END
        INTO v_horizon;

        IF v_horizon IS NULL THEN
            -- No periods defined: every date posts, nothing is provably
            -- frozen. The cache stays empty; fast reads fall back to scans.
            DELETE FROM rpt.balance_cache WHERE book_id = b;
            v_count := 0;
        ELSE
            -- Defensive belt under trg_periods_cache: rows from a shrunk
            -- horizon never survive a refresh.
            DELETE FROM rpt.balance_cache
            WHERE book_id = b AND horizon > v_horizon;

            -- Incremental extension: add the (newly frozen) delta zone to
            -- every existing row, in both variants (inclusive and FYC-free).
            -- The subquery reads pre-update rows, so cc.horizon is the old
            -- horizon during this command.
            UPDATE rpt.balance_cache c SET
                horizon      = v_horizon,
                dr           = c.dr + d.dr,
                cr           = c.cr + d.cr,
                dr_no_fyc    = c.dr_no_fyc + d.dr_no_fyc,
                cr_no_fyc    = c.cr_no_fyc + d.cr_no_fyc,
                refreshed_at = now()
            FROM (
                SELECT e.ledger_id,
                       COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0) AS dr,
                       COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0) AS cr,
                       COALESCE(SUM(CASE WHEN e.dc = 'D' AND vt.code <> 'FYC'
                                         THEN e.amount ELSE 0 END), 0) AS dr_no_fyc,
                       COALESCE(SUM(CASE WHEN e.dc = 'C' AND vt.code <> 'FYC'
                                         THEN e.amount ELSE 0 END), 0) AS cr_no_fyc
                FROM voucher_entries e
                JOIN vouchers v ON v.id = e.voucher_id
                JOIN voucher_types vt ON vt.id = v.voucher_type_id
                JOIN rpt.balance_cache cc
                  ON cc.book_id = b AND cc.ledger_id = e.ledger_id
                WHERE e.book_id = b
                  AND e.voucher_date >= cc.horizon
                  AND e.voucher_date < v_horizon
                GROUP BY e.ledger_id
            ) d
            WHERE c.book_id = b AND c.ledger_id = d.ledger_id;

            -- Ledgers below the horizon without a row get the full sums.
            INSERT INTO rpt.balance_cache
                (book_id, ledger_id, horizon, dr, cr, dr_no_fyc, cr_no_fyc, refreshed_at)
            SELECT b, e.ledger_id, v_horizon,
                   COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN e.dc = 'D' AND vt.code <> 'FYC'
                                     THEN e.amount ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN e.dc = 'C' AND vt.code <> 'FYC'
                                     THEN e.amount ELSE 0 END), 0),
                   now()
            FROM voucher_entries e
            JOIN vouchers v ON v.id = e.voucher_id
            JOIN voucher_types vt ON vt.id = v.voucher_type_id
            WHERE e.book_id = b
              AND e.voucher_date < v_horizon
              AND NOT EXISTS (SELECT 1 FROM rpt.balance_cache c
                              WHERE c.book_id = b AND c.ledger_id = e.ledger_id)
            GROUP BY e.ledger_id;

            SELECT count(*) INTO v_count FROM rpt.balance_cache WHERE book_id = b;
        END IF;

        RETURN QUERY SELECT b, v_count, v_horizon,
            CASE WHEN v_horizon IS NULL
                 THEN 'disabled (no periods — fast reads fall back to scans)'
                 ELSE 'refreshed' END;
    END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- The per-ledger aggregation primitive every point-in-time report sources
-- (ADR-018). Slow branch: the original aggregation, unchanged. Fast branch:
-- cached sums below the horizon + a tail scan above it, per ledger; ledgers
-- without a usable row (no row, or a row whose horizon overshoots p_upto —
-- e.g. an all-closed book queried as of a past date) take the direct scan.
-- Either way the result is exact: a stale cache can only cost time.
--
-- FYC exclusion (profit & loss): the cache carries both variants (inclusive
-- for Trial Balance / Balance Sheet, FYC-exclusive for P&L — an FYC sweep
-- posts on the first day of the next period, so it enters the cached zone
-- when that period closes in turn). The tail scan applies the exclusion
-- itself, exactly like the slow path.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION rpt._ledger_sums_upto(
    p_book_id     BIGINT,
    p_upto        DATE,
    p_exclude_fyc BOOLEAN DEFAULT false,
    p_fast        BOOLEAN DEFAULT false
)
RETURNS TABLE (
    out_ledger_id BIGINT,
    out_dr        NUMERIC,
    out_cr        NUMERIC
)
LANGUAGE plpgsql STABLE PARALLEL SAFE AS $$
BEGIN
    IF NOT p_fast THEN
        RETURN QUERY
            SELECT e.ledger_id,
                   COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0)
            FROM voucher_entries e
            JOIN vouchers v ON v.id = e.voucher_id
            WHERE e.book_id = p_book_id
              AND v.voucher_date <= p_upto
              AND (NOT p_exclude_fyc
                   OR v.voucher_type_id NOT IN
                       (SELECT vt.id FROM voucher_types vt WHERE vt.code = 'FYC'))
            GROUP BY e.ledger_id;
    ELSIF NOT p_exclude_fyc THEN
        -- fast, no FYC exclusion (Trial Balance / Balance Sheet / rollups):
        -- per-ledger LATERAL range scans, no vouchers join at all.
        RETURN QUERY
            WITH usable AS (
                SELECT c.ledger_id, c.horizon,
                       CASE WHEN p_exclude_fyc THEN c.dr_no_fyc ELSE c.dr END AS dr,
                       CASE WHEN p_exclude_fyc THEN c.cr_no_fyc ELSE c.cr END AS cr
                FROM rpt.balance_cache c
                WHERE c.book_id = p_book_id
                  AND c.horizon <= p_upto + 1
            ),
            tail AS (
                SELECT u.ledger_id,
                       COALESCE(SUM(CASE WHEN x.dc = 'D' THEN x.amount ELSE 0 END), 0) AS t_dr,
                       COALESCE(SUM(CASE WHEN x.dc = 'C' THEN x.amount ELSE 0 END), 0) AS t_cr
                FROM usable u
                LEFT JOIN LATERAL (
                    SELECT e.dc, e.amount
                    FROM voucher_entries e
                    WHERE e.book_id = p_book_id
                      AND e.ledger_id = u.ledger_id
                      AND e.voucher_date >= u.horizon
                      AND e.voucher_date <= p_upto
                ) x ON true
                GROUP BY u.ledger_id
            )
            SELECT u.ledger_id,
                   COALESCE(NULLIF(u.dr + COALESCE(t.t_dr, 0), 0), 0),
                   COALESCE(NULLIF(u.cr + COALESCE(t.t_cr, 0), 0), 0)
            FROM usable u
            LEFT JOIN tail t ON t.ledger_id = u.ledger_id

            UNION ALL

            SELECT s.ledger_id, s.s_dr, s.s_cr
            FROM ledgers l
            CROSS JOIN LATERAL (
                SELECT e.ledger_id,
                       COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0) AS s_dr,
                       COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0) AS s_cr
                FROM voucher_entries e
                WHERE e.book_id = p_book_id
                  AND e.ledger_id = l.id
                  AND e.voucher_date <= p_upto
                GROUP BY e.ledger_id
            ) s
            WHERE l.book_id = p_book_id
              AND NOT EXISTS (
                  SELECT 1 FROM rpt.balance_cache c
                  WHERE c.book_id = p_book_id
                    AND c.ledger_id = l.id
                    AND c.horizon <= p_upto + 1
              );
    ELSE
        -- fast, FYC excluded (Profit & Loss): identical shape, with the
        -- sweep exclusion applied to the tail (cached sums use the
        -- dr_no_fyc / cr_no_fyc variants).
        RETURN QUERY
            WITH usable AS (
                SELECT c.ledger_id, c.horizon, c.dr_no_fyc AS dr, c.cr_no_fyc AS cr
                FROM rpt.balance_cache c
                WHERE c.book_id = p_book_id
                  AND c.horizon <= p_upto + 1
            ),
            tail AS (
                SELECT u.ledger_id,
                       COALESCE(SUM(CASE WHEN x.dc = 'D' THEN x.amount ELSE 0 END), 0) AS t_dr,
                       COALESCE(SUM(CASE WHEN x.dc = 'C' THEN x.amount ELSE 0 END), 0) AS t_cr
                FROM usable u
                LEFT JOIN LATERAL (
                    SELECT e.dc, e.amount
                    FROM voucher_entries e
                    JOIN vouchers v ON v.id = e.voucher_id
                    WHERE e.book_id = p_book_id
                      AND e.ledger_id = u.ledger_id
                      AND e.voucher_date >= u.horizon
                      AND e.voucher_date <= p_upto
                      AND v.voucher_type_id NOT IN
                          (SELECT vt.id FROM voucher_types vt WHERE vt.code = 'FYC')
                ) x ON true
                GROUP BY u.ledger_id
            )
            SELECT u.ledger_id,
                   COALESCE(NULLIF(u.dr + COALESCE(t.t_dr, 0), 0), 0),
                   COALESCE(NULLIF(u.cr + COALESCE(t.t_cr, 0), 0), 0)
            FROM usable u
            LEFT JOIN tail t ON t.ledger_id = u.ledger_id

            UNION ALL

            SELECT s.ledger_id, s.s_dr, s.s_cr
            FROM ledgers l
            CROSS JOIN LATERAL (
                SELECT e.ledger_id,
                       COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0) AS s_dr,
                       COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0) AS s_cr
                FROM voucher_entries e
                JOIN vouchers v ON v.id = e.voucher_id
                WHERE e.book_id = p_book_id
                  AND e.ledger_id = l.id
                  AND e.voucher_date <= p_upto
                  AND v.voucher_type_id NOT IN
                      (SELECT vt.id FROM voucher_types vt WHERE vt.code = 'FYC')
                GROUP BY e.ledger_id
            ) s
            WHERE l.book_id = p_book_id
              AND NOT EXISTS (
                  SELECT 1 FROM rpt.balance_cache c
                  WHERE c.book_id = p_book_id
                    AND c.ledger_id = l.id
                    AND c.horizon <= p_upto + 1
              );
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- Trial Balance: per-ledger debit/credit/closing as of a date, one book.
-- Each ledger is classified by its OWN group's nature (single counting).
-- p_fast (ADR-018): source the sums from the balance cache where warm.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION rpt.trial_balance(p_book_id BIGINT, p_as_of DATE, p_fast BOOLEAN DEFAULT false)
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
        SELECT s.out_ledger_id AS ledger_id, s.out_dr AS dr, s.out_cr AS cr
        FROM rpt._ledger_sums_upto(p_book_id, p_as_of, false, p_fast) s
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
    ORDER BY l.id
$$;

-- ----------------------------------------------------------------------------
-- Group rollups: every group's subtree debit/credit as of a date, in one
-- closure-table pass (the query the closure model exists for), one book.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION rpt.group_rollups(p_book_id BIGINT, p_as_of DATE, p_fast BOOLEAN DEFAULT false)
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
        SELECT l.group_id AS gid, s.out_dr AS dr, s.out_cr AS cr
        FROM rpt._ledger_sums_upto(p_book_id, p_as_of, false, p_fast) s
        JOIN ledgers l ON l.id = s.out_ledger_id
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
    ORDER BY g.id
$$;

-- ----------------------------------------------------------------------------
-- Profit & Loss: trading (direct, affects_gross) and indirect sections, with
-- gross and net profit rows. Ledger-level classification for totals; group
-- rows are subtree rollups for display. Financial-year close sweeps (voucher
-- type FYC) are excluded: they transfer results to Profit & Loss A/c, they
-- are not earnings — without this, the year after a close would report the
-- sweep as a loss (ADR-008). Book-scoped (ADR-009).
--
-- p_fast (ADR-018): the cache is lifetime-shaped, so the range is computed
-- as S(to) − S(from − 1), both point-in-time aggregates the cache serves.
-- The FYC exclusion applies to both terms — arithmetically identical to
-- excluding it from the range. The zero-row filter keeps the row set
-- byte-identical to the slow path (a ledger's range sums are (0, 0) iff it
-- has no entries in range, since every amount is strictly positive).
-- ----------------------------------------------------------------------------
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

-- ----------------------------------------------------------------------------
-- Balance Sheet: assets / liabilities / equity group rollups as of a date,
-- plus the cumulative profit-and-loss plug under equity, one book. Section
-- totals are computed by the API from rpt.trial_balance (ledger-level,
-- single-counted); group rows here are subtree rollups for the display tree.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION rpt.balance_sheet(p_book_id BIGINT, p_as_of DATE, p_fast BOOLEAN DEFAULT false)
RETURNS TABLE (
    out_section    TEXT,
    out_group_id   BIGINT,
    out_group_name TEXT,
    out_parent_id  BIGINT,
    out_amount     NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH per_ledger AS (
        SELECT l.group_id AS gid, s.out_dr AS dr, s.out_cr AS cr
        FROM rpt._ledger_sums_upto(p_book_id, p_as_of, false, p_fast) s
        JOIN ledgers l ON l.id = s.out_ledger_id
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
    ORDER BY 1, 2 NULLS LAST
$$;

-- ----------------------------------------------------------------------------
-- Financial-year close: the exact sweep that zeroes the year (issue #5,
-- ADR-008; book-scoped by 0004). For every income/expense ledger of the book
-- with a non-zero closing balance as of p_as_of, the reversing line; the net
-- difference lands on the Profit & Loss A/c ledger (p_pl_ledger_id).
-- Balanced by construction — every line is an ABS of a balance and the P&L
-- line absorbs the algebraic sum, so Law 1 holds exactly. The close action
-- posts these rows verbatim as an FYC voucher dated the first day of the
-- NEXT period.
--
-- Deliberately untouched by ADR-018: the close runs once a year, in a
-- ceremony, not on a hot path.
-- ----------------------------------------------------------------------------
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

-- ============================================================================
-- Schema `ops` — the scale-out machinery (ADR-018).
--
-- partition_entries_by_year() and unpartition_entries() are the operator's
-- maintenance-window conversion (owner-only: deliberately NOT granted to
-- ledgerforge_app). ensure_entry_partition_for_voucher() is the SECURITY
-- DEFINER trigger function that keeps postings working across year
-- boundaries on the partitioned shape. scale_out_status() is the admin
-- screen's data source.
-- ============================================================================
CREATE SCHEMA IF NOT EXISTS ops;

GRANT USAGE ON SCHEMA ops TO ledgerforge_app;

-- Attach missing yearly partitions to a given partitioned parent. The
-- parent is a parameter because the conversion creates partitions for its
-- SHADOW while the plain voucher_entries still owns the name.
CREATE OR REPLACE FUNCTION ops._ensure_year_partitions(
    p_parent regclass, p_from_year INT, p_to_year INT)
RETURNS TABLE (out_year INT, out_created BOOLEAN)
LANGUAGE plpgsql AS $$
DECLARE
    y         INT;
    v_created BOOLEAN;
BEGIN
    FOR y IN p_from_year..p_to_year LOOP
        v_created := to_regclass(format('public.voucher_entries_%s', y)) IS NULL;
        IF v_created THEN
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS public.voucher_entries_%s '
                'PARTITION OF %s '
                'FOR VALUES FROM (%L) TO (%L)',
                y, p_parent, make_date(y, 1, 1), make_date(y + 1, 1, 1));
        END IF;
        RETURN QUERY SELECT y, v_created;
    END LOOP;
END $$;

-- Create yearly partitions for [p_from_year, p_to_year] (idempotent) on the
-- live table — the operator's year-end ritual.
CREATE OR REPLACE FUNCTION ops.ensure_entry_partitions(p_from_year INT, p_to_year INT)
RETURNS TABLE (out_year INT, out_created BOOLEAN)
LANGUAGE plpgsql AS $$
BEGIN
    IF p_to_year < p_from_year THEN
        RAISE EXCEPTION 'p_to_year (%) is before p_from_year (%)', p_to_year, p_from_year;
    END IF;
    IF (SELECT c.relkind FROM pg_class c WHERE c.oid = 'public.voucher_entries'::regclass) <> 'p' THEN
        RAISE EXCEPTION 'voucher_entries is not partitioned — run ops.partition_entries_by_year() first';
    END IF;
    RETURN QUERY SELECT * FROM ops._ensure_year_partitions(
        'public.voucher_entries'::regclass, p_from_year, p_to_year);
END $$;

-- BEFORE INSERT ON vouchers: create the missing yearly partition for the
-- voucher's date, on demand. This lives on the VOUCHERS side on purpose:
-- row triggers on a partitioned table fire after routing (too late when the
-- partition is missing), and DDL fired mid-statement on the table being
-- inserted into deadlocks on plan invalidation. SECURITY DEFINER so the app
-- role never needs DDL rights; a no-op on the plain shape.
CREATE OR REPLACE FUNCTION ops.ensure_entry_partition_for_voucher() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ops, public AS $$
DECLARE
    y INT;
BEGIN
    IF (SELECT c.relkind = 'p' FROM pg_class c
        WHERE c.oid = 'public.voucher_entries'::regclass) THEN
        y := EXTRACT(YEAR FROM NEW.voucher_date)::int;
        IF to_regclass(format('public.voucher_entries_%s', y)) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS public.voucher_entries_%s '
                'PARTITION OF public.voucher_entries '
                'FOR VALUES FROM (%L) TO (%L)',
                y, make_date(y, 1, 1), make_date(y + 1, 1, 1));
        END IF;
    END IF;
    RETURN NEW;
END $$;

-- The conversion: plain voucher_entries -> yearly range partitions, in one
-- transaction (PostgreSQL's transactional DDL makes the swap atomic to
-- every other session). The shape preserves every invariant:
--   * PK (id, voucher_date) and line uniqueness (voucher_id, line_no,
--     voucher_date) — a voucher's entries share its date (proven by
--     trg_entries_check_date), so line uniqueness is equivalent AND a
--     voucher's entries never span partitions, keeping the deferrable
--     Law-1 constraint trigger sound;
--   * the same triggers, FKs and checks, parent-level (cloned to every
--     partition, including ones created later);
--   * the covering index moves to the parent and a BRIN on voucher_date
--     is added; the copy is date-ordered so partitions land BRIN-friendly.
-- Cache rows survive: they sum by date, not by table shape.
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

    ALTER TABLE public.voucher_entries RENAME TO voucher_entries_plain;
    ALTER TABLE public.voucher_entries_part RENAME TO voucher_entries;
    DROP TABLE public.voucher_entries_plain;

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

    ALTER TABLE public.voucher_entries RENAME TO voucher_entries_old;
    ALTER TABLE public.voucher_entries_plain RENAME TO voucher_entries;
    DROP TABLE public.voucher_entries_old;

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

-- Partition/unpartition are owner-only (maintenance-window DDL); the status
-- screen and the vouchers-side ensure function are what the app role needs.
GRANT EXECUTE ON FUNCTION ops.scale_out_status() TO ledgerforge_app;
GRANT EXECUTE ON FUNCTION ops.ensure_entry_partition_for_voucher() TO ledgerforge_app;

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA rpt TO ledgerforge_app;
