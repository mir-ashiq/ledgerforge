# ruff: noqa: E501 (generated: the SQL slices keep their source formatting)
"""Attendance registers: the payroll depth's fact tables (ADR-029).

The fifth core-schema change since 0006, deliberately — the ADR-026 argument
restated for the shop floor: present/absent/half-day/leave-with-pay counts
are observations about the world, not double-entry movements (Law 1 forbids
inventing a voucher to carry them), and the books must remember them exactly
so per-day pay heads can derive from them. ``vouchers.meta`` cannot promise
it (a voucher must balance), so the migration lands the banking pack's exact
shape — a batch (the register receipt) and lines (the day counts) —
INSERT-only, trigger-guarded append-only, idempotent by content fingerprint
(UNIQUE per book, revision-carrying so a corrected register re-states whole).

- ``payroll_attendance_batches`` — the register receipt: the ``YYYY-MM``
  period it covers, a source label, line count.
- ``payroll_attendance_lines`` — the day counts: employee, present, absent,
  half-day, leave-with-pay (``NUMERIC(6,1)`` — a half-day is 0.5, exact), a
  note, and the fingerprint — with a batch-scoping trigger so a line rides
  its own receipt's book (the bank-line discipline).

The kernel stays a storage-and-fold seam: payable days are the payroll
pack's arithmetic (served through the versioned plugin API); the live
register is the period's latest batch — the 2B latest-snapshot discipline.

The upgrade blocks are generated from db/schema.sql verbatim; the
downgrade drops everything this revision created.

Revision ID: 0011_payroll_depth
Revises: 0010_gst_2b
Create Date: 2026-11-08
"""

from alembic import op

revision = "0011_payroll_depth"
down_revision = "0010_gst_2b"
branch_labels = None
depends_on = None

UPGRADE_SQL = """\
-- ----------------------------------------------------------------------------
-- Attendance registers (0011/ADR-029) — the payroll depth's fact tables, the
-- ADR-026 argument restated for the shop floor: present/absent/half-day/
-- leave-with-pay counts are observations about the world, not double-entry
-- movements (Law 1 forbids inventing a voucher to carry them), and the books
-- must remember them exactly. A batch is one period's register; a corrected
-- register is a new batch (append-only history, Law 2) and the fold reads
-- the period's latest batch only — the 2B latest-snapshot discipline. Day
-- counts are NUMERIC(6,1): a half-day is 0.5, exact. The kernel stores and
-- validates; payable days are the payroll pack's arithmetic (ADR-029: the
-- kernel stays meaning-blind). Books are strangers (ADR-009): book-scoped.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll_attendance_batches (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id      BIGINT NOT NULL REFERENCES books(id),
    period       TEXT NOT NULL CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    source       TEXT,
    line_count   INTEGER NOT NULL DEFAULT 0,
    imported_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payroll_attendance_lines (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id         BIGINT NOT NULL REFERENCES books(id),
    batch_id        BIGINT NOT NULL,
    employee        TEXT NOT NULL,
    days_present    NUMERIC(6, 1) NOT NULL DEFAULT 0 CHECK (days_present >= 0),
    days_absent     NUMERIC(6, 1) NOT NULL DEFAULT 0 CHECK (days_absent >= 0),
    days_half       NUMERIC(6, 1) NOT NULL DEFAULT 0 CHECK (days_half >= 0),
    days_leave_paid NUMERIC(6, 1) NOT NULL DEFAULT 0 CHECK (days_leave_paid >= 0),
    note            TEXT,
    fingerprint     TEXT NOT NULL,
    imported_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_attendance_lines_fingerprint UNIQUE (book_id, fingerprint),
    CONSTRAINT fk_attendance_lines_batch FOREIGN KEY (batch_id)
        REFERENCES payroll_attendance_batches (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_attendance_lines_batch ON payroll_attendance_lines (batch_id);
CREATE INDEX IF NOT EXISTS idx_attendance_lines_period
    ON payroll_attendance_batches (book_id, period, id);

-- Batch/line scoping at the only write path the fact tables have (INSERT —
-- they are append-only): a line must ride its own batch's book — the
-- composite discipline the bank and portal lines follow.
CREATE OR REPLACE FUNCTION trg_attendance_lines_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_batch RECORD;
BEGIN
    SELECT book_id INTO v_batch FROM payroll_attendance_batches WHERE id = NEW.batch_id;
    IF v_batch IS NULL THEN
        RAISE EXCEPTION 'Attendance line references unknown register batch %', NEW.batch_id;
    END IF;
    IF v_batch.book_id <> NEW.book_id THEN
        RAISE EXCEPTION 'Attendance line must ride its own batch''s book (batch % is book %; the line claims %)',
            NEW.batch_id, v_batch.book_id, NEW.book_id;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_attendance_lines_check ON payroll_attendance_lines;
CREATE TRIGGER trg_attendance_lines_check
    BEFORE INSERT ON payroll_attendance_lines
    FOR EACH ROW EXECUTE FUNCTION trg_attendance_lines_check();
-- Attendance registers are history the moment they land (ADR-029), the
-- bank-statement and portal-row discipline: a day count a payroll run
-- already derived pay from is never edited — a corrected register is a new
-- import, and the fingerprint makes the re-import idempotent. The batches
-- (register receipts) are history with them.
DROP TRIGGER IF EXISTS trg_attendance_batches_immutable ON payroll_attendance_batches;
CREATE TRIGGER trg_attendance_batches_immutable
    BEFORE UPDATE OR DELETE ON payroll_attendance_batches
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();

DROP TRIGGER IF EXISTS trg_attendance_lines_immutable ON payroll_attendance_lines;
CREATE TRIGGER trg_attendance_lines_immutable
    BEFORE UPDATE OR DELETE ON payroll_attendance_lines
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();
-- The grants ride the schema file's tail (least privilege).
GRANT INSERT ON payroll_attendance_batches, payroll_attendance_lines TO ledgerforge_app;
GRANT SELECT ON payroll_attendance_batches, payroll_attendance_lines TO ledgerforge_app;
GRANT USAGE ON SEQUENCE payroll_attendance_batches_id_seq,
    payroll_attendance_lines_id_seq TO ledgerforge_app;
"""

DOWNGRADE_SQL = """\
-- Privileges first: REVOKEs need the objects to still exist.
REVOKE SELECT, INSERT ON payroll_attendance_batches, payroll_attendance_lines
    FROM ledgerforge_app;
REVOKE USAGE ON SEQUENCE payroll_attendance_batches_id_seq,
    payroll_attendance_lines_id_seq FROM ledgerforge_app;

DROP TRIGGER IF EXISTS trg_attendance_lines_immutable ON payroll_attendance_lines;
DROP TRIGGER IF EXISTS trg_attendance_batches_immutable ON payroll_attendance_batches;
DROP TRIGGER IF EXISTS trg_attendance_lines_check ON payroll_attendance_lines;
DROP TABLE IF EXISTS payroll_attendance_lines;
DROP FUNCTION IF EXISTS trg_attendance_lines_check();
DROP TABLE IF EXISTS payroll_attendance_batches;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
