# ruff: noqa: E501 (generated: the SQL slices keep their source formatting)
"""Cost centres & cost categories: the allocation store (ADR-025).

The first core-schema change since 0006, deliberately: allocations must
aggregate with the same rigour as entries — exact sums, closure rollups,
book scoping, append-only history — which ``vouchers.meta`` cannot promise.

- ``cost_categories`` + ``cost_category_closure`` — the classification axes,
  a tree with one root per book, the adjacency+closure pair of ADR-001;
- ``cost_centres`` + ``cost_centre_closure`` — the allocation tree, the same
  machinery; every real centre answers to exactly one category;
- ``cost_allocations`` — entry x centre x amount, Dr/Cr preserved from the
  entry, amounts exact; per-row integrity at insert (same book, same D/C,
  same date, never the root centre) and the conservation law at commit:
  an entry's allocations sum to the entry's amount exactly (fully allocated
  or unallocated, never partly) — the same deferrable-trigger discipline as
  Law 1's ``trg_entries_balanced``;
- ``rpt.cost_centre_summary`` / ``rpt.cost_centre_breakup`` /
  ``rpt.cost_category_summary`` — the break-up reports through the closure
  machinery, FYC sweeps excluded, with the Unallocated mass surfaced so
  allocated + unallocated is always the window's whole entry mass.

The upgrade blocks are generated from db/schema.sql and db/functions.sql
verbatim; the downgrade drops everything this revision created.

Revision ID: 0007_cost_centres
Revises: 0006_report_scale_out
Create Date: 2026-11-08
"""

from alembic import op

revision = "0007_cost_centres"
down_revision = "0006_report_scale_out"
branch_labels = None
depends_on = None

UPGRADE_SQL = """\
-- ----------------------------------------------------------------------------
-- Cost categories (Phase 4, ADR-025; migration 0007) — the axes along which
-- money is classified ("Department", "Location", "Project"…). A tree with
-- one root per book, the same adjacency+closure pair as groups (ADR-001):
-- categories classify CENTRES, centres carry the allocations.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cost_categories (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id    BIGINT NOT NULL REFERENCES books(id),
    name       TEXT NOT NULL,
    parent_id  BIGINT,
    is_root    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_cost_categories_book_id UNIQUE (book_id, id),
    CONSTRAINT uq_cost_categories_book_name UNIQUE (book_id, name),
    CONSTRAINT fk_cost_categories_parent_same_book FOREIGN KEY (book_id, parent_id)
        REFERENCES cost_categories (book_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_cost_categories_parent ON cost_categories (parent_id);

-- Exactly one root cost category per book.
CREATE UNIQUE INDEX IF NOT EXISTS uq_cost_categories_single_root
    ON cost_categories (book_id) WHERE is_root;

-- Category closure — the same trigger-maintained read model as group_closure.
CREATE TABLE IF NOT EXISTS cost_category_closure (
    ancestor_id   BIGINT NOT NULL REFERENCES cost_categories(id) ON DELETE CASCADE,
    descendant_id BIGINT NOT NULL REFERENCES cost_categories(id) ON DELETE CASCADE,
    depth         INT NOT NULL CHECK (depth >= 0),
    PRIMARY KEY (ancestor_id, descendant_id)
);

CREATE INDEX IF NOT EXISTS idx_cost_category_closure_descendant
    ON cost_category_closure (descendant_id);

-- ----------------------------------------------------------------------------
-- Cost centres — the allocation tree (like groups: adjacency is the source
-- of truth, the closure answers hierarchy questions). Every real centre
-- answers to exactly one cost category (TallyPrime's rule); the root is the
-- tree's container and receives no allocations.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cost_centres (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id     BIGINT NOT NULL REFERENCES books(id),
    name        TEXT NOT NULL,
    code        TEXT,
    parent_id   BIGINT,
    category_id BIGINT,
    is_root     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_cost_centres_book_id UNIQUE (book_id, id),
    CONSTRAINT uq_cost_centres_book_name UNIQUE (book_id, name),
    CONSTRAINT fk_cost_centres_parent_same_book FOREIGN KEY (book_id, parent_id)
        REFERENCES cost_centres (book_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_cost_centres_category_same_book FOREIGN KEY (book_id, category_id)
        REFERENCES cost_categories (book_id, id) ON DELETE RESTRICT,
    -- the root is the container (no category of its own); every real centre
    -- is classified by exactly one category
    CONSTRAINT chk_cost_centre_root_category
        CHECK ((is_root AND category_id IS NULL) OR (NOT is_root AND category_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_cost_centres_parent ON cost_centres (parent_id);
CREATE INDEX IF NOT EXISTS idx_cost_centres_category ON cost_centres (category_id);

-- Exactly one root cost centre per book.
CREATE UNIQUE INDEX IF NOT EXISTS uq_cost_centres_single_root
    ON cost_centres (book_id) WHERE is_root;

-- Centre closure — trigger-maintained, the machinery reports read ONLY.
CREATE TABLE IF NOT EXISTS cost_centre_closure (
    ancestor_id   BIGINT NOT NULL REFERENCES cost_centres(id) ON DELETE CASCADE,
    descendant_id BIGINT NOT NULL REFERENCES cost_centres(id) ON DELETE CASCADE,
    depth         INT NOT NULL CHECK (depth >= 0),
    PRIMARY KEY (ancestor_id, descendant_id)
);

CREATE INDEX IF NOT EXISTS idx_cost_centre_closure_descendant
    ON cost_centre_closure (descendant_id);

-- Category closure maintenance — the group triggers (ADR-001) with the
-- tables renamed: insert inherits the parent's ancestor chain, re-parenting
-- is guarded against cycles and root unrooting, a move rewrites the subtree.
CREATE OR REPLACE FUNCTION trg_cost_categories_after_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO cost_category_closure (ancestor_id, descendant_id, depth)
    SELECT a.ancestor_id, NEW.id, a.depth + 1
    FROM cost_category_closure a
    WHERE a.descendant_id = NEW.parent_id;

    INSERT INTO cost_category_closure (ancestor_id, descendant_id, depth)
    VALUES (NEW.id, NEW.id, 0);
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION trg_cost_categories_before_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.is_root AND (NOT NEW.is_root OR NEW.parent_id IS NOT NULL) THEN
        RAISE EXCEPTION 'The root cost category cannot be re-parented or unrooted';
    END IF;
    IF NEW.parent_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM cost_category_closure
        WHERE ancestor_id = NEW.id AND descendant_id = NEW.parent_id
    ) THEN
        RAISE EXCEPTION 'Cycle detected: cost category "%" cannot be moved under its own descendant', NEW.name;
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION trg_cost_categories_after_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.parent_id IS NOT DISTINCT FROM OLD.parent_id THEN
        RETURN NEW;
    END IF;

    DELETE FROM cost_category_closure
    WHERE descendant_id IN (SELECT descendant_id FROM cost_category_closure WHERE ancestor_id = NEW.id)
      AND ancestor_id IN (SELECT ancestor_id FROM cost_category_closure WHERE descendant_id = OLD.parent_id);

    INSERT INTO cost_category_closure (ancestor_id, descendant_id, depth)
    SELECT sup.ancestor_id, sub.descendant_id, sup.depth + sub.depth + 1
    FROM cost_category_closure sup, cost_category_closure sub
    WHERE sup.descendant_id = NEW.parent_id
      AND sub.ancestor_id = NEW.id;

    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION trg_cost_categories_before_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.is_root THEN
        RAISE EXCEPTION 'The root cost category cannot be deleted';
    END IF;
    RETURN OLD;
END $$;

DROP TRIGGER IF EXISTS trg_cost_categories_ins ON cost_categories;
CREATE TRIGGER trg_cost_categories_ins
    AFTER INSERT ON cost_categories
    FOR EACH ROW EXECUTE FUNCTION trg_cost_categories_after_insert();

DROP TRIGGER IF EXISTS trg_cost_categories_upd_cycle ON cost_categories;
CREATE TRIGGER trg_cost_categories_upd_cycle
    BEFORE UPDATE OF parent_id, is_root ON cost_categories
    FOR EACH ROW EXECUTE FUNCTION trg_cost_categories_before_update();

DROP TRIGGER IF EXISTS trg_cost_categories_upd ON cost_categories;
CREATE TRIGGER trg_cost_categories_upd
    AFTER UPDATE OF parent_id ON cost_categories
    FOR EACH ROW EXECUTE FUNCTION trg_cost_categories_after_update();

DROP TRIGGER IF EXISTS trg_cost_categories_del ON cost_categories;
CREATE TRIGGER trg_cost_categories_del
    BEFORE DELETE ON cost_categories
    FOR EACH ROW EXECUTE FUNCTION trg_cost_categories_before_delete();

-- Centre closure maintenance — the same four triggers, the centre tables.
CREATE OR REPLACE FUNCTION trg_cost_centres_after_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO cost_centre_closure (ancestor_id, descendant_id, depth)
    SELECT a.ancestor_id, NEW.id, a.depth + 1
    FROM cost_centre_closure a
    WHERE a.descendant_id = NEW.parent_id;

    INSERT INTO cost_centre_closure (ancestor_id, descendant_id, depth)
    VALUES (NEW.id, NEW.id, 0);
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION trg_cost_centres_before_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.is_root AND (NOT NEW.is_root OR NEW.parent_id IS NOT NULL) THEN
        RAISE EXCEPTION 'The root cost centre cannot be re-parented or unrooted';
    END IF;
    IF NEW.parent_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM cost_centre_closure
        WHERE ancestor_id = NEW.id AND descendant_id = NEW.parent_id
    ) THEN
        RAISE EXCEPTION 'Cycle detected: cost centre "%" cannot be moved under its own descendant', NEW.name;
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION trg_cost_centres_after_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.parent_id IS NOT DISTINCT FROM OLD.parent_id THEN
        RETURN NEW;
    END IF;

    DELETE FROM cost_centre_closure
    WHERE descendant_id IN (SELECT descendant_id FROM cost_centre_closure WHERE ancestor_id = NEW.id)
      AND ancestor_id IN (SELECT ancestor_id FROM cost_centre_closure WHERE descendant_id = OLD.parent_id);

    INSERT INTO cost_centre_closure (ancestor_id, descendant_id, depth)
    SELECT sup.ancestor_id, sub.descendant_id, sup.depth + sub.depth + 1
    FROM cost_centre_closure sup, cost_centre_closure sub
    WHERE sup.descendant_id = NEW.parent_id
      AND sub.ancestor_id = NEW.id;

    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION trg_cost_centres_before_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.is_root THEN
        RAISE EXCEPTION 'The root cost centre cannot be deleted';
    END IF;
    RETURN OLD;
END $$;

DROP TRIGGER IF EXISTS trg_cost_centres_ins ON cost_centres;
CREATE TRIGGER trg_cost_centres_ins
    AFTER INSERT ON cost_centres
    FOR EACH ROW EXECUTE FUNCTION trg_cost_centres_after_insert();

DROP TRIGGER IF EXISTS trg_cost_centres_upd_cycle ON cost_centres;
CREATE TRIGGER trg_cost_centres_upd_cycle
    BEFORE UPDATE OF parent_id, is_root ON cost_centres
    FOR EACH ROW EXECUTE FUNCTION trg_cost_centres_before_update();

DROP TRIGGER IF EXISTS trg_cost_centres_upd ON cost_centres;
CREATE TRIGGER trg_cost_centres_upd
    AFTER UPDATE OF parent_id ON cost_centres
    FOR EACH ROW EXECUTE FUNCTION trg_cost_centres_after_update();

DROP TRIGGER IF EXISTS trg_cost_centres_del ON cost_centres;
CREATE TRIGGER trg_cost_centres_del
    BEFORE DELETE ON cost_centres
    FOR EACH ROW EXECUTE FUNCTION trg_cost_centres_before_delete();

DROP TRIGGER IF EXISTS trg_audit_cost_categories ON cost_categories;
CREATE TRIGGER trg_audit_cost_categories
    AFTER INSERT OR UPDATE OR DELETE ON cost_categories
    FOR EACH ROW EXECUTE FUNCTION trg_audit_master();

DROP TRIGGER IF EXISTS trg_audit_cost_centres ON cost_centres;
CREATE TRIGGER trg_audit_cost_centres
    AFTER INSERT OR UPDATE OR DELETE ON cost_centres
    FOR EACH ROW EXECUTE FUNCTION trg_audit_master();

-- ----------------------------------------------------------------------------
-- Cost allocations (ADR-025) — the first core financial table since the
-- baseline that is not a voucher: one row per (entry, centre, amount),
-- Dr/Cr preserved from the entry, amount exact (Law 3). The conservation
-- law is database-enforced like Law 1: at commit, an entry's allocations
-- sum to the entry's amount EXACTLY — a line is either fully allocated or
-- carries no allocations at all, never partly. voucher_date and book_id
-- are denormalized from the entry (mirroring voucher_entries) so reports
-- never join the write path. Append-only like the entries they split.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cost_allocations (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id     BIGINT NOT NULL,
    centre_id    BIGINT NOT NULL REFERENCES cost_centres(id) ON DELETE RESTRICT,
    book_id      BIGINT NOT NULL REFERENCES books(id),
    dc           TEXT NOT NULL CHECK (dc IN ('D', 'C')),
    amount       NUMERIC(26, 8) NOT NULL CHECK (amount > 0),
    voucher_date DATE NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The entry FK exists only on the PLAIN voucher_entries shape: once
-- ops.partition_entries_by_year swaps in the partitioned table, the PK is
-- (id, voucher_date) and no unique index on id alone can exist. On that
-- shape the insert-time trigger below keeps the integrity (same book, same
-- D/C, same date, the entry exists) — and ops.unpartition_entries re-adds
-- this constraint when the plain shape returns.
DO $$
BEGIN
    IF (SELECT c.relkind FROM pg_class c
        WHERE c.oid = 'public.voucher_entries'::regclass) = 'r'
       AND NOT EXISTS (
           SELECT 1 FROM pg_constraint
           WHERE conname = 'cost_allocations_entry_id_fkey'
             AND conrelid = 'public.cost_allocations'::regclass) THEN
        ALTER TABLE public.cost_allocations
            ADD CONSTRAINT cost_allocations_entry_id_fkey
            FOREIGN KEY (entry_id) REFERENCES public.voucher_entries(id) ON DELETE RESTRICT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_cost_allocations_entry ON cost_allocations (entry_id);
CREATE INDEX IF NOT EXISTS idx_cost_allocations_centre ON cost_allocations (centre_id);
CREATE INDEX IF NOT EXISTS idx_cost_allocations_book_date ON cost_allocations (book_id, voucher_date);

-- Per-row integrity at insert time: same book (entry, centre), Dr/Cr
-- preserved from the entry, the entry's own voucher_date, and never the
-- root centre (the container receives no allocations — TallyPrime's rule).
CREATE OR REPLACE FUNCTION trg_cost_allocations_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_book       BIGINT;
    v_dc         TEXT;
    v_date       DATE;
    v_centre     RECORD;
BEGIN
    SELECT book_id, dc, voucher_date INTO v_book, v_dc, v_date
    FROM voucher_entries WHERE id = NEW.entry_id;
    IF v_book IS NULL THEN
        RAISE EXCEPTION 'Cost allocation references unknown entry %', NEW.entry_id;
    END IF;
    IF NEW.book_id <> v_book THEN
        RAISE EXCEPTION 'Cost allocation book mismatch: entry % belongs to book %, the allocation claims %',
            NEW.entry_id, v_book, NEW.book_id;
    END IF;
    IF NEW.dc <> v_dc THEN
        RAISE EXCEPTION 'Cost allocation D/C mismatch on entry %: allocations preserve the entry''s Dr/Cr (%)',
            NEW.entry_id, v_dc;
    END IF;
    IF NEW.voucher_date <> v_date THEN
        RAISE EXCEPTION 'Cost allocation date mismatch on entry %: % is not the voucher''s date %',
            NEW.entry_id, NEW.voucher_date, v_date;
    END IF;
    SELECT book_id, is_root INTO v_centre FROM cost_centres WHERE id = NEW.centre_id;
    IF v_centre IS NULL THEN
        RAISE EXCEPTION 'Cost allocation references unknown cost centre %', NEW.centre_id;
    END IF;
    IF v_centre.book_id <> NEW.book_id THEN
        RAISE EXCEPTION 'Cost allocation centre % belongs to another book', NEW.centre_id;
    END IF;
    IF v_centre.is_root THEN
        RAISE EXCEPTION 'The root cost centre is the tree''s container — allocate to a real centre';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_cost_allocations_check ON cost_allocations;
CREATE TRIGGER trg_cost_allocations_check
    BEFORE INSERT ON cost_allocations
    FOR EACH ROW EXECUTE FUNCTION trg_cost_allocations_check();

-- The conservation law, database-enforced at commit (deferrable so a
-- voucher's allocations all land before the verdict — the same discipline
-- as Law 1's trg_entries_balanced).
CREATE OR REPLACE FUNCTION trg_cost_allocations_exact() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_amount   NUMERIC;
    v_allocated NUMERIC;
BEGIN
    SELECT amount INTO v_amount FROM voucher_entries WHERE id = NEW.entry_id;
    SELECT COALESCE(SUM(amount), 0) INTO v_allocated
    FROM cost_allocations WHERE entry_id = NEW.entry_id;

    IF v_allocated <> v_amount THEN
        RAISE EXCEPTION 'Cost allocation mismatch on entry %: % allocated against an amount of % — a line is fully allocated or left unallocated, never partly',
            NEW.entry_id, v_allocated, v_amount;
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS trg_cost_allocations_exact ON cost_allocations;
CREATE CONSTRAINT TRIGGER trg_cost_allocations_exact
    AFTER INSERT ON cost_allocations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION trg_cost_allocations_exact();


-- Cost allocations are financial history too (ADR-025): they split entries,
-- they are corrected by reversing the voucher — never by editing the split.
DROP TRIGGER IF EXISTS trg_cost_allocations_immutable ON cost_allocations;
CREATE TRIGGER trg_cost_allocations_immutable
    BEFORE UPDATE OR DELETE ON cost_allocations
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();


-- Cost centres (0007/ADR-025): masters like groups (closure follows by
-- trigger, the group_closure precedent); allocations are financial history —
-- INSERT only, append-only like voucher_entries. The last two lines are
-- what the blanket grants above already give the new objects — stated here
-- so the migration path (which has no blanket re-run) matches exactly.
GRANT SELECT, INSERT, UPDATE, DELETE ON cost_categories, cost_centres TO ledgerforge_app;
GRANT INSERT ON cost_allocations TO ledgerforge_app;
GRANT SELECT ON cost_allocations, cost_category_closure, cost_centre_closure TO ledgerforge_app;
GRANT USAGE ON SEQUENCE cost_categories_id_seq, cost_centres_id_seq, cost_allocations_id_seq
    TO ledgerforge_app;


-- ============================================================================
-- Cost centres & cost categories — the break-up reports (Phase 4, ADR-025).
--
-- The same closure-table pass as group_rollups, twice: centre allocations
-- roll up through cost_centre_closure (a parent centre carries its subtree),
-- and the centre→category edge rolls up through cost_category_closure (a
-- primary category carries its secondaries). Financial-year close sweeps
-- (voucher type FYC) are excluded on both sides — they transfer results,
-- they are not operating facts (the profit_and_loss precedent). Entries
-- nobody allocated surface as the 'Unallocated' row (out_centre_id /
-- out_category_id NULL), so allocated + unallocated is always the window's
-- whole entry mass.
-- ============================================================================

CREATE OR REPLACE FUNCTION rpt.cost_centre_summary(p_book_id BIGINT, p_from DATE, p_to DATE)
RETURNS TABLE (
    out_centre_id    BIGINT,
    out_centre_name  TEXT,
    out_parent_id    BIGINT,
    out_is_root      BOOLEAN,
    out_category_id  BIGINT,
    out_category_name TEXT,
    out_dr           NUMERIC,
    out_cr           NUMERIC,
    out_net          NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH alloc AS (
        SELECT ca.centre_id AS cid,
               CASE WHEN ca.dc = 'D' THEN ca.amount ELSE 0 END AS dr,
               CASE WHEN ca.dc = 'C' THEN ca.amount ELSE 0 END AS cr
        FROM cost_allocations ca
        JOIN voucher_entries ve ON ve.id = ca.entry_id
        JOIN vouchers v ON v.id = ve.voucher_id
        JOIN voucher_types vt ON vt.id = v.voucher_type_id
        WHERE ca.book_id = p_book_id
          AND ca.voucher_date BETWEEN p_from AND p_to
          AND vt.code <> 'FYC'
    ),
    per_centre AS (
        SELECT c.ancestor_id AS cid, SUM(a.dr) AS dr, SUM(a.cr) AS cr
        FROM alloc a
        JOIN cost_centre_closure c ON c.descendant_id = a.cid
        GROUP BY c.ancestor_id
    )
    SELECT cc.id, cc.name, cc.parent_id, cc.is_root, cc.category_id, cat.name,
           pc.dr, pc.cr, pc.dr - pc.cr
    FROM per_centre pc
    JOIN cost_centres cc ON cc.id = pc.cid
    LEFT JOIN cost_categories cat ON cat.id = cc.category_id
    WHERE cc.book_id = p_book_id

    UNION ALL
    SELECT NULL, 'Unallocated', NULL, false, NULL, NULL,
           COALESCE(SUM(u.dr), 0), COALESCE(SUM(u.cr), 0),
           COALESCE(SUM(u.dr), 0) - COALESCE(SUM(u.cr), 0)
    FROM (
        SELECT CASE WHEN ve.dc = 'D' THEN ve.amount ELSE 0 END AS dr,
               CASE WHEN ve.dc = 'C' THEN ve.amount ELSE 0 END AS cr
        FROM voucher_entries ve
        JOIN vouchers v ON v.id = ve.voucher_id
        JOIN voucher_types vt ON vt.id = v.voucher_type_id
        WHERE ve.book_id = p_book_id
          AND ve.voucher_date BETWEEN p_from AND p_to
          AND vt.code <> 'FYC'
          AND NOT EXISTS (SELECT 1 FROM cost_allocations ca WHERE ca.entry_id = ve.id)
    ) u
    ORDER BY 1 NULLS LAST
$$;

CREATE OR REPLACE FUNCTION rpt.cost_centre_breakup(
    p_book_id BIGINT, p_centre_id BIGINT, p_from DATE, p_to DATE)
RETURNS TABLE (
    out_bucket      TEXT,
    out_ledger_id   BIGINT,
    out_ledger_name TEXT,
    out_group_name  TEXT,
    out_nature      TEXT,
    out_dr          NUMERIC,
    out_cr          NUMERIC,
    out_net         NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH lines AS (
        SELECT ve.ledger_id AS lid,
               CASE WHEN ca.dc = 'D' THEN ca.amount ELSE 0 END AS dr,
               CASE WHEN ca.dc = 'C' THEN ca.amount ELSE 0 END AS cr
        FROM cost_allocations ca
        JOIN voucher_entries ve ON ve.id = ca.entry_id
        JOIN vouchers v ON v.id = ve.voucher_id
        JOIN voucher_types vt ON vt.id = v.voucher_type_id
        WHERE ca.book_id = p_book_id
          AND ca.centre_id = p_centre_id
          AND ca.voucher_date BETWEEN p_from AND p_to
          AND vt.code <> 'FYC'
    ),
    per_ledger AS (
        SELECT lid, SUM(dr) AS dr, SUM(cr) AS cr FROM lines GROUP BY lid
    )
    SELECT 'ledger', l.id, l.name, g.name, g.nature, pl.dr, pl.cr,
           CASE WHEN g.nature IN ('asset', 'expense') THEN pl.dr - pl.cr
                ELSE pl.cr - pl.dr END
    FROM per_ledger pl
    JOIN ledgers l ON l.id = pl.lid
    JOIN groups g ON g.id = l.group_id

    UNION ALL
    SELECT 'total', NULL, NULL, NULL, NULL,
           COALESCE((SELECT SUM(dr) FROM lines), 0),
           COALESCE((SELECT SUM(cr) FROM lines), 0),
           COALESCE((SELECT SUM(dr) FROM lines), 0) - COALESCE((SELECT SUM(cr) FROM lines), 0)
$$;

CREATE OR REPLACE FUNCTION rpt.cost_category_summary(p_book_id BIGINT, p_from DATE, p_to DATE)
RETURNS TABLE (
    out_category_id   BIGINT,
    out_category_name TEXT,
    out_parent_id     BIGINT,
    out_is_root       BOOLEAN,
    out_dr            NUMERIC,
    out_cr            NUMERIC,
    out_net           NUMERIC
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH alloc AS (
        SELECT cs.category_id AS catid,
               CASE WHEN ca.dc = 'D' THEN ca.amount ELSE 0 END AS dr,
               CASE WHEN ca.dc = 'C' THEN ca.amount ELSE 0 END AS cr
        FROM cost_allocations ca
        JOIN cost_centres cs ON cs.id = ca.centre_id
        JOIN voucher_entries ve ON ve.id = ca.entry_id
        JOIN vouchers v ON v.id = ve.voucher_id
        JOIN voucher_types vt ON vt.id = v.voucher_type_id
        WHERE ca.book_id = p_book_id
          AND ca.voucher_date BETWEEN p_from AND p_to
          AND vt.code <> 'FYC'
    ),
    per_category AS (
        SELECT c.ancestor_id AS catid, SUM(a.dr) AS dr, SUM(a.cr) AS cr
        FROM alloc a
        JOIN cost_category_closure c ON c.descendant_id = a.catid
        GROUP BY c.ancestor_id
    )
    SELECT cc.id, cc.name, cc.parent_id, cc.is_root, pc.dr, pc.cr, pc.dr - pc.cr
    FROM per_category pc
    JOIN cost_categories cc ON cc.id = pc.catid
    WHERE cc.book_id = p_book_id

    UNION ALL
    SELECT NULL, 'Unallocated', NULL, false,
           COALESCE(SUM(u.dr), 0), COALESCE(SUM(u.cr), 0),
           COALESCE(SUM(u.dr), 0) - COALESCE(SUM(u.cr), 0)
    FROM (
        SELECT CASE WHEN ve.dc = 'D' THEN ve.amount ELSE 0 END AS dr,
               CASE WHEN ve.dc = 'C' THEN ve.amount ELSE 0 END AS cr
        FROM voucher_entries ve
        JOIN vouchers v ON v.id = ve.voucher_id
        JOIN voucher_types vt ON vt.id = v.voucher_type_id
        WHERE ve.book_id = p_book_id
          AND ve.voucher_date BETWEEN p_from AND p_to
          AND vt.code <> 'FYC'
          AND NOT EXISTS (SELECT 1 FROM cost_allocations ca WHERE ca.entry_id = ve.id)
    ) u
    ORDER BY 1 NULLS LAST
$$;


-- The shape-aware swap functions replace 0006's (cost allocations
-- reference entries now); verbatim from db/functions.sql
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

    -- Cost allocations reference entries (0007/ADR-025); the partitioned
    -- shape's PK is (id, voucher_date) — an FK on id alone cannot exist
    -- there. It comes off before the swap; trg_cost_allocations_check
    -- keeps the integrity on both shapes; ops.unpartition_entries re-adds
    -- it when the plain shape returns.
    ALTER TABLE public.cost_allocations DROP CONSTRAINT IF EXISTS cost_allocations_entry_id_fkey;

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


-- the fresh-bootstrap path grants EXECUTE on every rpt function after
-- functions.sql lands; repeat it here so the three new functions get
-- the same grant on the migration path
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA rpt TO ledgerforge_app;
"""


DOWNGRADE_SQL = """\
-- privileges first: REVOKEs need the objects to still exist. The rpt
-- blanket EXECUTE is re-granted right after so only THIS revision's three
-- functions lose theirs (the rest keep the grant 0001-0006 gave them).
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA rpt FROM ledgerforge_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA rpt TO ledgerforge_app;
REVOKE SELECT, INSERT, UPDATE, DELETE ON cost_categories, cost_centres FROM ledgerforge_app;
REVOKE INSERT ON cost_allocations FROM ledgerforge_app;
REVOKE SELECT ON cost_allocations, cost_category_closure, cost_centre_closure FROM ledgerforge_app;
REVOKE USAGE ON SEQUENCE cost_categories_id_seq, cost_centres_id_seq, cost_allocations_id_seq
    FROM ledgerforge_app;

DROP FUNCTION IF EXISTS rpt.cost_centre_summary(BIGINT, DATE, DATE);
DROP FUNCTION IF EXISTS rpt.cost_centre_breakup(BIGINT, BIGINT, DATE, DATE);
DROP FUNCTION IF EXISTS rpt.cost_category_summary(BIGINT, DATE, DATE);

DROP TRIGGER IF EXISTS trg_cost_allocations_immutable ON cost_allocations;
DROP TRIGGER IF EXISTS trg_cost_allocations_exact ON cost_allocations;
DROP TRIGGER IF EXISTS trg_cost_allocations_check ON cost_allocations;
DROP TABLE IF EXISTS cost_allocations;
DROP FUNCTION IF EXISTS trg_cost_allocations_check();
DROP FUNCTION IF EXISTS trg_cost_allocations_exact();

DROP TRIGGER IF EXISTS trg_audit_cost_centres ON cost_centres;
DROP TRIGGER IF EXISTS trg_cost_centres_ins ON cost_centres;
DROP TRIGGER IF EXISTS trg_cost_centres_upd_cycle ON cost_centres;
DROP TRIGGER IF EXISTS trg_cost_centres_upd ON cost_centres;
DROP TRIGGER IF EXISTS trg_cost_centres_del ON cost_centres;
DROP FUNCTION IF EXISTS trg_cost_centres_after_insert();
DROP FUNCTION IF EXISTS trg_cost_centres_before_update();
DROP FUNCTION IF EXISTS trg_cost_centres_after_update();
DROP FUNCTION IF EXISTS trg_cost_centres_before_delete();
DROP TABLE IF EXISTS cost_centre_closure;
DROP TABLE IF EXISTS cost_centres;

DROP TRIGGER IF EXISTS trg_audit_cost_categories ON cost_categories;
DROP TRIGGER IF EXISTS trg_cost_categories_ins ON cost_categories;
DROP TRIGGER IF EXISTS trg_cost_categories_upd_cycle ON cost_categories;
DROP TRIGGER IF EXISTS trg_cost_categories_upd ON cost_categories;
DROP TRIGGER IF EXISTS trg_cost_categories_del ON cost_categories;
DROP FUNCTION IF EXISTS trg_cost_categories_after_insert();
DROP FUNCTION IF EXISTS trg_cost_categories_before_update();
DROP FUNCTION IF EXISTS trg_cost_categories_after_update();
DROP FUNCTION IF EXISTS trg_cost_categories_before_delete();
DROP TABLE IF EXISTS cost_category_closure;
DROP TABLE IF EXISTS cost_categories;

-- 0006's swap functions stay in their cost-aware replacement: the only
-- 0007 references left are `DROP CONSTRAINT IF EXISTS` (a no-op with the
-- cost tables gone) and a guarded re-add that never fires (the constraint
-- cannot exist without cost_allocations). Functionally identical to
-- 0006's text on a database without cost centres.
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
