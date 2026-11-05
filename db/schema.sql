-- ============================================================================
-- LedgerForge — core accounting schema (PostgreSQL 16+)
-- ----------------------------------------------------------------------------
-- Design decisions: docs/adr/001 (hierarchy), docs/adr/002 (write path),
-- docs/database-design.md.  This script is idempotent: it can be re-run
-- safely on an existing database.
-- ============================================================================

-- Optional trigram index for ledger name search (skip silently if contrib
-- extensions are unavailable in this build).
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pg_trgm unavailable; ledger name search will use plain indexes';
END $$;

-- ----------------------------------------------------------------------------
-- Settings (single-book configuration)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- Groups — the chart-of-accounts tree (adjacency list = source of truth)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS groups (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    code          TEXT,
    parent_id     BIGINT REFERENCES groups(id) ON DELETE RESTRICT,
    nature        TEXT NOT NULL
                  CHECK (nature IN ('asset', 'liability', 'equity', 'income', 'expense')),
    affects_gross BOOLEAN NOT NULL DEFAULT FALSE,
    is_root       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_groups_parent ON groups (parent_id);

-- Exactly one root group.
CREATE UNIQUE INDEX IF NOT EXISTS uq_groups_single_root ON groups (is_root) WHERE is_root;

-- ----------------------------------------------------------------------------
-- Closure table — trigger-maintained read model for all hierarchy queries
-- (ADR-001). Reports read ONLY this table; parent_id remains authoritative.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS group_closure (
    ancestor_id   BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    descendant_id BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    depth         INT NOT NULL CHECK (depth >= 0),
    PRIMARY KEY (ancestor_id, descendant_id)
);

CREATE INDEX IF NOT EXISTS idx_group_closure_descendant ON group_closure (descendant_id);

-- Closure maintenance: new node inherits its parent's ancestor chain.
CREATE OR REPLACE FUNCTION trg_groups_after_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO group_closure (ancestor_id, descendant_id, depth)
    SELECT a.ancestor_id, NEW.id, a.depth + 1
    FROM group_closure a
    WHERE a.descendant_id = NEW.parent_id;

    INSERT INTO group_closure (ancestor_id, descendant_id, depth)
    VALUES (NEW.id, NEW.id, 0);
    RETURN NEW;
END $$;

-- Cycle + root protection before re-parenting.
CREATE OR REPLACE FUNCTION trg_groups_before_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.is_root AND (NOT NEW.is_root OR NEW.parent_id IS NOT NULL) THEN
        RAISE EXCEPTION 'The root group cannot be re-parented or unrooted';
    END IF;
    IF NEW.parent_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM group_closure
        WHERE ancestor_id = NEW.id AND descendant_id = NEW.parent_id
    ) THEN
        RAISE EXCEPTION 'Cycle detected: group "%" cannot be moved under its own descendant', NEW.name;
    END IF;
    RETURN NEW;
END $$;

-- Closure rewrite on subtree move (two set-based statements; ADR-001).
CREATE OR REPLACE FUNCTION trg_groups_after_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.parent_id IS NOT DISTINCT FROM OLD.parent_id THEN
        RETURN NEW;
    END IF;

    DELETE FROM group_closure
    WHERE descendant_id IN (SELECT descendant_id FROM group_closure WHERE ancestor_id = NEW.id)
      AND ancestor_id IN (SELECT ancestor_id FROM group_closure WHERE descendant_id = OLD.parent_id);

    INSERT INTO group_closure (ancestor_id, descendant_id, depth)
    SELECT sup.ancestor_id, sub.descendant_id, sup.depth + sub.depth + 1
    FROM group_closure sup, group_closure sub
    WHERE sup.descendant_id = NEW.parent_id
      AND sub.ancestor_id = NEW.id;

    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION trg_groups_before_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.is_root THEN
        RAISE EXCEPTION 'The root group cannot be deleted';
    END IF;
    RETURN OLD;
END $$;

DROP TRIGGER IF EXISTS trg_groups_ins ON groups;
CREATE TRIGGER trg_groups_ins
    AFTER INSERT ON groups
    FOR EACH ROW EXECUTE FUNCTION trg_groups_after_insert();

DROP TRIGGER IF EXISTS trg_groups_upd_cycle ON groups;
CREATE TRIGGER trg_groups_upd_cycle
    BEFORE UPDATE OF parent_id, is_root ON groups
    FOR EACH ROW EXECUTE FUNCTION trg_groups_before_update();

DROP TRIGGER IF EXISTS trg_groups_upd ON groups;
CREATE TRIGGER trg_groups_upd
    AFTER UPDATE OF parent_id ON groups
    FOR EACH ROW EXECUTE FUNCTION trg_groups_after_update();

DROP TRIGGER IF EXISTS trg_groups_del ON groups;
CREATE TRIGGER trg_groups_del
    BEFORE DELETE ON groups
    FOR EACH ROW EXECUTE FUNCTION trg_groups_before_delete();

-- ----------------------------------------------------------------------------
-- Ledgers (names unique book-wide, as in TallyPrime)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledgers (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    code       TEXT,
    group_id   BIGINT NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
    is_system  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ledgers_group ON ledgers (group_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_ledgers_name_trgm ON ledgers USING gin (name gin_trgm_ops)';
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- Voucher types (TallyPrime's set) and numbering configuration
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voucher_types (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code           TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    number_prefix  TEXT NOT NULL DEFAULT '',
    restart_yearly BOOLEAN NOT NULL DEFAULT TRUE,
    is_system      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- Vouchers — immutable financial history (Law 2). Correct only via reversal.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vouchers (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid            UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    client_token    UUID NOT NULL UNIQUE,
    voucher_type_id BIGINT NOT NULL REFERENCES voucher_types(id),
    voucher_no      TEXT NOT NULL,
    sequence        BIGINT NOT NULL,
    voucher_date    DATE NOT NULL,
    narration       TEXT,
    reversal_of     BIGINT REFERENCES vouchers(id),
    posted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_voucher_no_per_type UNIQUE (voucher_type_id, voucher_no),
    CONSTRAINT chk_no_self_reversal CHECK (reversal_of IS NULL OR reversal_of <> id)
);

CREATE INDEX IF NOT EXISTS idx_vouchers_date ON vouchers (voucher_date);
CREATE INDEX IF NOT EXISTS idx_vouchers_type_date ON vouchers (voucher_type_id, voucher_date);
CREATE INDEX IF NOT EXISTS idx_vouchers_reversal_of ON vouchers (reversal_of);

-- ----------------------------------------------------------------------------
-- Voucher entries — the append-only fact table (ADR-002, Law 2)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voucher_entries (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    voucher_id   BIGINT NOT NULL REFERENCES vouchers(id) ON DELETE RESTRICT,
    line_no      INT NOT NULL CHECK (line_no > 0),
    ledger_id    BIGINT NOT NULL REFERENCES ledgers(id) ON DELETE RESTRICT,
    dc           CHAR(1) NOT NULL CHECK (dc IN ('D', 'C')),
    amount       NUMERIC(26, 8) NOT NULL CHECK (amount > 0),
    -- denormalized from vouchers for the covering reporting index; proven
    -- equal by trg_entries_check_date below (immutability makes drift
    -- impossible after insert)
    voucher_date DATE NOT NULL,
    narration    TEXT,
    CONSTRAINT uq_entry_line UNIQUE (voucher_id, line_no)
);

-- The covering index every balance / report query uses.
CREATE INDEX IF NOT EXISTS idx_entries_ledger_date
    ON voucher_entries (ledger_id, voucher_date) INCLUDE (dc, amount);

-- Entry date must match its voucher's date.
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

-- Law 1, database-enforced: at commit time, every voucher must have >= 2
-- entries and total debits exactly equal to total credits. Deferrable so
-- multi-row inserts never observe a partially-loaded voucher.
CREATE OR REPLACE FUNCTION trg_entries_check_balance() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_n  BIGINT;
    v_dr NUMERIC;
    v_cr NUMERIC;
BEGIN
    SELECT count(*),
           COALESCE(SUM(CASE WHEN dc = 'D' THEN amount END), 0),
           COALESCE(SUM(CASE WHEN dc = 'C' THEN amount END), 0)
    INTO v_n, v_dr, v_cr
    FROM voucher_entries
    WHERE voucher_id = NEW.voucher_id;

    IF v_n < 2 THEN
        RAISE EXCEPTION 'Voucher % must contain at least 2 entries', NEW.voucher_id;
    END IF;
    IF v_dr <> v_cr THEN
        RAISE EXCEPTION 'Unbalanced voucher %: total debit % does not equal total credit %',
            NEW.voucher_id, v_dr, v_cr;
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS trg_entries_date ON voucher_entries;
CREATE TRIGGER trg_entries_date
    BEFORE INSERT ON voucher_entries
    FOR EACH ROW EXECUTE FUNCTION trg_entries_check_date();

DROP TRIGGER IF EXISTS trg_entries_balanced ON voucher_entries;
CREATE CONSTRAINT TRIGGER trg_entries_balanced
    AFTER INSERT ON voucher_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION trg_entries_check_balance();

-- ----------------------------------------------------------------------------
-- Gapless document numbering — the only shared mutable row in the write path
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc_counters (
    scope_key  TEXT PRIMARY KEY,
    last_value BIGINT NOT NULL DEFAULT 0
);

-- ----------------------------------------------------------------------------
-- Append-only audit journal for master data changes
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT,
    table_name  TEXT NOT NULL,
    operation   TEXT NOT NULL,
    record_id   BIGINT,
    row_data    JSONB
);

CREATE INDEX IF NOT EXISTS idx_audit_table_record ON audit_log (table_name, record_id);

CREATE OR REPLACE FUNCTION trg_audit_master() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO audit_log (actor, table_name, operation, record_id, row_data)
    VALUES (
        NULLIF(current_setting('app.actor', true), ''),
        TG_TABLE_NAME,
        TG_OP,
        COALESCE(NEW.id, OLD.id),
        to_jsonb(CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END)
    );
    RETURN COALESCE(NEW, OLD);
END $$;

DROP TRIGGER IF EXISTS trg_audit_groups ON groups;
CREATE TRIGGER trg_audit_groups
    AFTER INSERT OR UPDATE OR DELETE ON groups
    FOR EACH ROW EXECUTE FUNCTION trg_audit_master();

DROP TRIGGER IF EXISTS trg_audit_ledgers ON ledgers;
CREATE TRIGGER trg_audit_ledgers
    AFTER INSERT OR UPDATE OR DELETE ON ledgers
    FOR EACH ROW EXECUTE FUNCTION trg_audit_master();

DROP TRIGGER IF EXISTS trg_audit_voucher_types ON voucher_types;
CREATE TRIGGER trg_audit_voucher_types
    AFTER INSERT OR UPDATE OR DELETE ON voucher_types
    FOR EACH ROW EXECUTE FUNCTION trg_audit_master();

-- ----------------------------------------------------------------------------
-- Law 2 defense in depth: financial history is append-only at the trigger
-- level and at the privilege level.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'Table % is append-only: % is not permitted on financial records; correct mistakes with a reversal voucher',
        TG_TABLE_NAME, TG_OP;
END $$;

DROP TRIGGER IF EXISTS trg_vouchers_immutable ON vouchers;
CREATE TRIGGER trg_vouchers_immutable
    BEFORE UPDATE OR DELETE ON vouchers
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();

DROP TRIGGER IF EXISTS trg_voucher_entries_immutable ON voucher_entries;
CREATE TRIGGER trg_voucher_entries_immutable
    BEFORE UPDATE OR DELETE ON voucher_entries
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();

DROP TRIGGER IF EXISTS trg_audit_log_immutable ON audit_log;
CREATE TRIGGER trg_audit_log_immutable
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION trg_forbid_mutation();

-- ----------------------------------------------------------------------------
-- Application role: least privilege. Even a compromised application cannot
-- update or delete financial history.
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ledgerforge_app') THEN
        CREATE ROLE ledgerforge_app NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO ledgerforge_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ledgerforge_app;
GRANT INSERT ON vouchers, voucher_entries, audit_log TO ledgerforge_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON groups, ledgers, voucher_types, doc_counters, settings
    TO ledgerforge_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO ledgerforge_app;
