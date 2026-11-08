# ruff: noqa: E501 (generated: the SQL slices keep their source formatting)
"""GST portal downloads: the 2A/2B facts (ADR-028).

The fourth core-schema change since 0006, deliberately — the ADR-026
argument restated for the tax portal: a downloaded GSTR-2A/2B return's
supplier rows are external facts the books must remember exactly, and the
reconciliation's portal side must aggregate them with exact-decimal
rigour. ``vouchers.meta`` cannot promise it (the download is not
double-entry and Law 1 forbids inventing a voucher to carry it), so the
migration lands the banking pack's exact shape — a batch (the import
receipt) and lines (the facts) — INSERT-only, trigger-guarded
append-only, idempotent by fingerprint (UNIQUE per book).

- ``gst_downloads`` — the import receipt: kind ('2a'/'2b'), the return
  period it covers, the registrant GSTIN it belongs to (multi-GSTIN
  books download one file per registration), a source label, line count.
- ``gst_download_lines`` — the supplier rows: GSTIN, document type
  (INV/CN/DN), number, date, taxable value, the IGST/CGST/SGST split,
  place of supply, the portal's ITC-eligibility verdict — with a
  fingerprint UNIQUE per book so a re-downloaded period imports as a
  no-op, and a batch-scoping trigger so a line rides its own receipt's
  book (the bank-line discipline).

The kernel stays a storage-and-fold seam: interpretation is the GST
pack's fold (served through the versioned plugin API); portal connectivity
is the operator's adapter, out-of-process (D3, the IRN precedent).

The upgrade blocks are generated from db/schema.sql verbatim; the
downgrade drops everything this revision created.

Revision ID: 0010_gst_2b
Revises: 0009_stock_depth
Create Date: 2026-11-08
"""

from alembic import op

revision = "0010_gst_2b"
down_revision = "0009_stock_depth"
branch_labels = None
depends_on = None

UPGRADE_SQL = """\
-- ----------------------------------------------------------------------------
-- GST portal downloads (0010/ADR-028) — the GSTR-2A/2B comparison's other
-- side, stored as the facts they are. The ADR-026 argument restated for the
-- tax portal: a downloaded return's supplier rows are external facts the
-- books must remember exactly (supplier GSTIN, document type/number/date,
-- taxable value, the IGST/CGST/SGST split, the portal's own ITC-eligibility
-- verdict). Re-downloading the same period must be a no-op (idempotent —
-- the fingerprint), two suppliers' look-alike invoices must both survive,
-- and the reconciliation's portal-side sums must aggregate with exact-
-- decimal rigour. vouchers.meta cannot promise it (the download is not
-- double-entry and Law 1 forbids inventing a voucher to carry it), so the
-- fourth core-schema change since 0006 lands two fact tables in the
-- banking-pack's exact shape: a batch (the import receipt) and lines (the
-- facts), INSERT-only, trigger-guarded append-only.
--
-- The kernel stays a storage-and-fold seam: it never interprets a
-- supplier row's tax meaning (that is the GST pack's fold, served through
-- the versioned plugin API) and never talks to the GST portal itself
-- (that is the operator's adapter, out-of-process by contract D3 — the
-- IRN precedent). Books are strangers (ADR-009): every row is book-scoped.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gst_downloads (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id      BIGINT NOT NULL REFERENCES books(id),
    kind         TEXT NOT NULL CHECK (kind IN ('2a', '2b')),
    period       TEXT NOT NULL CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    gstin        TEXT NOT NULL,
    source       TEXT,
    line_count   INTEGER NOT NULL DEFAULT 0,
    imported_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gst_download_lines (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id         BIGINT NOT NULL REFERENCES books(id),
    batch_id        BIGINT NOT NULL,
    supplier_gstin  TEXT NOT NULL,
    party           TEXT,
    doc_type        TEXT NOT NULL CHECK (doc_type IN ('INV', 'CN', 'DN')),
    doc_no          TEXT NOT NULL,
    doc_date        DATE NOT NULL,
    taxable         NUMERIC(26, 8) NOT NULL CHECK (taxable >= 0),
    igst            NUMERIC(26, 8) NOT NULL DEFAULT 0 CHECK (igst >= 0),
    cgst            NUMERIC(26, 8) NOT NULL DEFAULT 0 CHECK (cgst >= 0),
    sgst            NUMERIC(26, 8) NOT NULL DEFAULT 0 CHECK (sgst >= 0),
    pos             TEXT,
    itc_available   BOOLEAN NOT NULL DEFAULT TRUE,
    fingerprint     TEXT NOT NULL,
    imported_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_gst_lines_fingerprint UNIQUE (book_id, fingerprint),
    CONSTRAINT fk_gst_lines_batch FOREIGN KEY (batch_id)
        REFERENCES gst_downloads (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_gst_lines_batch ON gst_download_lines (batch_id);
CREATE INDEX IF NOT EXISTS idx_gst_lines_supplier ON gst_download_lines (supplier_gstin, doc_type, doc_no);

-- Batch/line scoping at the only write path the fact tables have (INSERT —
-- they are append-only): a line must ride its own batch's book — the
-- composite discipline the bank lines follow.
CREATE OR REPLACE FUNCTION trg_gst_lines_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_batch RECORD;
BEGIN
    SELECT book_id INTO v_batch FROM gst_downloads WHERE id = NEW.batch_id;
    IF v_batch IS NULL THEN
        RAISE EXCEPTION 'Portal line references unknown download batch %', NEW.batch_id;
    END IF;
    IF v_batch.book_id <> NEW.book_id THEN
        RAISE EXCEPTION 'Portal line must ride its own batch''s book (batch % is book %; the line claims %)',
            NEW.batch_id, v_batch.book_id, NEW.book_id;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_gst_lines_check ON gst_download_lines;
CREATE TRIGGER trg_gst_lines_check
    BEFORE INSERT ON gst_download_lines
    FOR EACH ROW EXECUTE FUNCTION trg_gst_lines_check();
-- Portal download facts are history the moment they land (ADR-028), the
-- bank-statement discipline: a supplier row already reconciled against is
-- never edited — a corrected download is a new import, and the fingerprint
-- makes the re-import idempotent. The downloads (import receipts) are
-- history with them.
DROP TRIGGER IF EXISTS trg_gst_downloads_immutable ON gst_downloads;
CREATE TRIGGER trg_gst_downloads_immutable
    BEFORE UPDATE OR DELETE ON gst_downloads
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();

DROP TRIGGER IF EXISTS trg_gst_lines_immutable ON gst_download_lines;
CREATE TRIGGER trg_gst_lines_immutable
    BEFORE UPDATE OR DELETE ON gst_download_lines
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();
-- The grants ride the schema file's tail (least privilege).
GRANT INSERT ON gst_downloads, gst_download_lines TO ledgerforge_app;
GRANT SELECT ON gst_downloads, gst_download_lines TO ledgerforge_app;
GRANT USAGE ON SEQUENCE gst_downloads_id_seq, gst_download_lines_id_seq
    TO ledgerforge_app;
"""

DOWNGRADE_SQL = """\
-- Privileges first: REVOKEs need the objects to still exist.
REVOKE SELECT, INSERT ON gst_downloads, gst_download_lines FROM ledgerforge_app;
REVOKE USAGE ON SEQUENCE gst_downloads_id_seq, gst_download_lines_id_seq
    FROM ledgerforge_app;

DROP TRIGGER IF EXISTS trg_gst_lines_immutable ON gst_download_lines;
DROP TRIGGER IF EXISTS trg_gst_downloads_immutable ON gst_downloads;
DROP TRIGGER IF EXISTS trg_gst_lines_check ON gst_download_lines;
DROP TABLE IF EXISTS gst_download_lines;
DROP FUNCTION IF EXISTS trg_gst_lines_check();
DROP TABLE IF EXISTS gst_downloads;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
