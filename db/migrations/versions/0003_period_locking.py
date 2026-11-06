"""Period locking / financial-year close (issue #5).

ADR-008. The design in one breath:

- ``periods`` are named, non-overlapping date ranges (a gist EXCLUDE on
  ``daterange(starts_on, ends_on, '[]')``) with a status:
  ``open → locked → closed`` (reversible only between open/locked).
- The posting gate is a BEFORE INSERT trigger on ``vouchers`` that takes a
  ``FOR SHARE`` lock on the covering period row, so a lock serializes against
  in-flight postings instead of racing them. With no periods defined the
  feature is off — every date posts (existing books are unaffected).
- Closing is two-step: lock (freeze for review), then close. The close posts
  the P&L sweep — ``rpt.fy_close`` — as an ``FYC`` voucher dated the FIRST
  day of the next period (so the closed year's own P&L report stays intact),
  zeroing every income/expense ledger's closing balance into Profit & Loss A/c,
  then flips the period to ``closed``, which is final.

Downgrade drops the gate, the guard functions, the sweep report and the
``periods`` table. Vouchers and entries are not period-owned, so removing
periods never touches financial history.

Revision ID: 0003_period_locking
Revises: 0002_multi_currency
Create Date: 2026-11-06
"""

from alembic import op

revision = "0003_period_locking"
down_revision = "0002_multi_currency"
branch_labels = None
depends_on = None

# -- db/schema.sql (period-locking section) + db/functions.sql (rpt.fy_close)
# — kept in sync by tests/test_migrations.py
UPGRADE_SQL = """\
-- Periods: named, non-overlapping fiscal ranges with a close state machine.
CREATE TABLE periods (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    starts_on   DATE NOT NULL,
    ends_on     DATE NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
                CONSTRAINT chk_periods_status CHECK (status IN ('open', 'locked', 'closed')),
    locked_at   TIMESTAMPTZ,
    closed_at   TIMESTAMPTZ,
    closed_by   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_periods_order CHECK (starts_on <= ends_on),
    CONSTRAINT uq_periods_no_overlap EXCLUDE USING gist
        (daterange(starts_on, ends_on, '[]') WITH &&)
);

-- Status transitions: open <-> locked, locked -> closed (final). Dates are
-- immutable facts; the timestamps are stamped by the trigger itself.
CREATE OR REPLACE FUNCTION trg_periods_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.starts_on <> OLD.starts_on OR NEW.ends_on <> OLD.ends_on THEN
        RAISE EXCEPTION 'Period dates are immutable: % .. %', OLD.starts_on, OLD.ends_on;
    END IF;
    IF NEW.status = OLD.status THEN
        RETURN NEW;  -- metadata-only update
    END IF;
    IF OLD.status = 'open' AND NEW.status = 'locked' THEN
        NEW.locked_at := now();
        RETURN NEW;
    END IF;
    IF OLD.status = 'locked' AND NEW.status = 'open' THEN
        NEW.locked_at := NULL;
        RETURN NEW;
    END IF;
    IF OLD.status = 'locked' AND NEW.status = 'closed' THEN
        IF COALESCE(NEW.closed_by, '') = '' THEN
            RAISE EXCEPTION 'Closing a period requires closed_by (who closed it)';
        END IF;
        NEW.closed_at := now();
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Invalid period status transition: % -> %', OLD.status, NEW.status;
END $$;

-- Deletion is only safe on an open, empty period.
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

-- The posting gate. FOR SHARE serializes against a concurrent lock/close of
-- the same period row: whichever commits first defines the outcome for the
-- other (a posting sees the new status, a lock waits for in-flight postings).
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

DROP TRIGGER IF EXISTS trg_vouchers_period ON vouchers;
CREATE TRIGGER trg_vouchers_period
    BEFORE INSERT ON vouchers
    FOR EACH ROW EXECUTE FUNCTION trg_vouchers_check_period();

DROP TRIGGER IF EXISTS trg_periods_guard ON periods;
CREATE TRIGGER trg_periods_guard
    BEFORE UPDATE ON periods
    FOR EACH ROW EXECUTE FUNCTION trg_periods_guard();

DROP TRIGGER IF EXISTS trg_periods_delete_guard ON periods;
CREATE TRIGGER trg_periods_delete_guard
    BEFORE DELETE ON periods
    FOR EACH ROW EXECUTE FUNCTION trg_periods_delete_guard();

CREATE TRIGGER trg_audit_periods
    AFTER INSERT OR UPDATE OR DELETE ON periods
    FOR EACH ROW EXECUTE FUNCTION trg_audit_master();

GRANT SELECT, INSERT, UPDATE, DELETE ON periods TO ledgerforge_app;
GRANT USAGE ON SEQUENCE periods_id_seq TO ledgerforge_app;
"""

FY_CLOSE_SQL = """\
-- The financial-year close: the exact voucher that zeroes the year. For each
-- income/expense ledger with a non-zero closing balance as of p_as_of, the
-- sweep line that cancels it; the net difference lands on the Profit & Loss
-- A/c ledger (p_pl_ledger_id). Balanced by construction: every line is an
-- ABS of a balance, and the P&L line absorbs the algebraic sum.
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

DOWN_RESTORE_PL_SQL = """\
-- 0003 downgrade: restore the pre-close profit_and_loss (from the frozen
-- baseline), which did not exclude FYC sweeps.
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
        WHERE v.voucher_date BETWEEN p_from AND p_to
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

# Same function as db/functions.sql ships post-0003 — profit_and_loss learns
# to exclude FYC close sweeps so the year after a close does not report the
# transfer as a loss. Kept byte-identical to functions.sql by the drift guard.
PL_SQL = DOWN_RESTORE_PL_SQL.replace(
    "-- 0003 downgrade: restore the pre-close profit_and_loss (from the frozen\n"
    "-- baseline), which did not exclude FYC sweeps.\n",
    "",
).replace(
    """        FROM voucher_entries e
        JOIN ledgers l ON l.id = e.ledger_id
        JOIN vouchers v ON v.id = e.voucher_id
        WHERE v.voucher_date BETWEEN p_from AND p_to
        GROUP BY l.group_id""",
    """        FROM voucher_entries e
        JOIN ledgers l ON l.id = e.ledger_id
        JOIN vouchers v ON v.id = e.voucher_id
        JOIN voucher_types vt ON vt.id = v.voucher_type_id
        WHERE v.voucher_date BETWEEN p_from AND p_to
          AND vt.code <> 'FYC'
        GROUP BY l.group_id""",
)

DOWNGRADE_SQL = """\
DROP TRIGGER IF EXISTS trg_vouchers_period ON vouchers;
DROP TABLE IF EXISTS periods;  -- drops its triggers, freeing the functions
DROP FUNCTION IF EXISTS trg_vouchers_check_period();
DROP FUNCTION IF EXISTS trg_periods_guard();
DROP FUNCTION IF EXISTS trg_periods_delete_guard();
DROP FUNCTION IF EXISTS rpt.fy_close(DATE, BIGINT);
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)
    op.execute(FY_CLOSE_SQL)
    op.execute(PL_SQL)
    op.execute("GRANT EXECUTE ON FUNCTION rpt.fy_close(DATE, BIGINT) TO ledgerforge_app")


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
    op.execute(DOWN_RESTORE_PL_SQL)
