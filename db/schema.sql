-- ============================================================================
-- LedgerForge — core accounting schema (PostgreSQL 16+)
-- ----------------------------------------------------------------------------
-- Design decisions: docs/adr/001 (hierarchy), docs/adr/002 (write path),
-- docs/adr/009 (multi-book), docs/database-design.md.  This script is
-- idempotent: it can be re-run safely on an existing database.
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
-- Settings — system-wide configuration only. Per-book facts (company name,
-- base currency, fiscal-year start) live on the books row itself (ADR-009).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- Currencies — ISO 4217 reference data shared by every book (issue #4,
-- ADR-007). A book's base currency is books.base_currency, a pointer.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS currencies (
    code        CHAR(3) PRIMARY KEY,
    name        TEXT NOT NULL,
    symbol      TEXT,
    minor_unit  SMALLINT NOT NULL DEFAULT 2
                CONSTRAINT chk_currencies_minor_unit CHECK (minor_unit BETWEEN 0 AND 8),
    is_system   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- Books — one row per company / legal entity (ADR-009, migration 0004).
-- Everything book-owned below carries its discriminator; currencies are the
-- one shared master. slug is the stable machine key (the bootstrap default
-- book is slug='default'); name is the human label.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS books (
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

-- ----------------------------------------------------------------------------
-- FX rates — per-book dated pairs (issue #4, ADR-007; book-scoped by 0004).
-- Advisory input data for posting UIs — the rate actually applied is
-- snapshotted on the entry row.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fx_rates (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id     BIGINT NOT NULL REFERENCES books(id),
    from_ccy    CHAR(3) NOT NULL REFERENCES currencies(code),
    to_ccy      CHAR(3) NOT NULL REFERENCES currencies(code),
    rate        NUMERIC(30, 10) NOT NULL
                CONSTRAINT chk_fx_rates_positive CHECK (rate > 0),
    valid_on    DATE NOT NULL,
    source      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_fx_rates_pair_day UNIQUE (book_id, from_ccy, to_ccy, valid_on),
    CONSTRAINT chk_fx_rates_pair_distinct CHECK (from_ccy <> to_ccy)
);

-- ----------------------------------------------------------------------------
-- Groups — the chart-of-accounts tree (adjacency list = source of truth),
-- one tree per book (ADR-001/009). The composite FK (book_id, parent_id)
-- makes same-book parenting a property of the schema: a group can never be
-- re-parented into another book's tree.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS groups (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id       BIGINT NOT NULL REFERENCES books(id),
    name          TEXT NOT NULL,
    code          TEXT,
    parent_id     BIGINT,
    nature        TEXT NOT NULL
                  CHECK (nature IN ('asset', 'liability', 'equity', 'income', 'expense')),
    affects_gross BOOLEAN NOT NULL DEFAULT FALSE,
    is_root       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_groups_book_id UNIQUE (book_id, id),
    CONSTRAINT uq_groups_book_name UNIQUE (book_id, name),
    CONSTRAINT fk_groups_parent_same_book FOREIGN KEY (book_id, parent_id)
        REFERENCES groups (book_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_groups_parent ON groups (parent_id);

-- Exactly one root group per book.
CREATE UNIQUE INDEX IF NOT EXISTS uq_groups_single_root ON groups (book_id) WHERE is_root;

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
-- Ledgers — names unique within a book, as in TallyPrime (ADR-009)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledgers (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id    BIGINT NOT NULL REFERENCES books(id),
    name       TEXT NOT NULL,
    code       TEXT,
    group_id   BIGINT NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
    is_system  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_ledgers_book_name UNIQUE (book_id, name)
);

CREATE INDEX IF NOT EXISTS idx_ledgers_group ON ledgers (group_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_ledgers_name_trgm ON ledgers USING gin (name gin_trgm_ops)';
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- Voucher types (TallyPrime's set) and numbering configuration — per book
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voucher_types (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id        BIGINT NOT NULL REFERENCES books(id),
    code           TEXT NOT NULL,
    name           TEXT NOT NULL,
    number_prefix  TEXT NOT NULL DEFAULT '',
    restart_yearly BOOLEAN NOT NULL DEFAULT TRUE,
    is_system      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_voucher_types_book_code UNIQUE (book_id, code)
);

-- ----------------------------------------------------------------------------
-- Vouchers — immutable financial history (Law 2). Correct only via reversal.
-- Book-scoped from 0004: numbering restarts per book (voucher types are
-- per-book) and idempotent client_tokens are unique per book, so the same
-- offline client may retry into two books without interference.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vouchers (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id         BIGINT NOT NULL REFERENCES books(id),
    uuid            UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    client_token    UUID NOT NULL,
    voucher_type_id BIGINT NOT NULL REFERENCES voucher_types(id),
    voucher_no      TEXT NOT NULL,
    sequence        BIGINT NOT NULL,
    voucher_date    DATE NOT NULL,
    narration       TEXT,
    reversal_of     BIGINT REFERENCES vouchers(id),
    posted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_voucher_no_per_type UNIQUE (book_id, voucher_type_id, voucher_no),
    CONSTRAINT uq_voucher_client_token_book UNIQUE (book_id, client_token),
    CONSTRAINT chk_no_self_reversal CHECK (reversal_of IS NULL OR reversal_of <> id)
);

CREATE INDEX IF NOT EXISTS idx_vouchers_date ON vouchers (book_id, voucher_date);
CREATE INDEX IF NOT EXISTS idx_vouchers_type_date ON vouchers (book_id, voucher_type_id, voucher_date);
CREATE INDEX IF NOT EXISTS idx_vouchers_reversal_of ON vouchers (reversal_of);

-- ----------------------------------------------------------------------------
-- Voucher entries — the append-only fact table (ADR-002, Law 2).
-- book_id is denormalized from the voucher and proven equal by
-- trg_entries_check_date (the same pattern as voucher_date); it is the
-- future partition key for per-book scale-out (roadmap §9).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voucher_entries (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    voucher_id   BIGINT NOT NULL REFERENCES vouchers(id) ON DELETE RESTRICT,
    line_no      INT NOT NULL CHECK (line_no > 0),
    ledger_id    BIGINT NOT NULL REFERENCES ledgers(id) ON DELETE RESTRICT,
    book_id      BIGINT NOT NULL REFERENCES books(id),
    dc           CHAR(1) NOT NULL CHECK (dc IN ('D', 'C')),
    amount       NUMERIC(26, 8) NOT NULL CHECK (amount > 0),
    -- denormalized from vouchers for the covering reporting index; proven
    -- equal by trg_entries_check_date below (immutability makes drift
    -- impossible after insert)
    voucher_date DATE NOT NULL,
    narration    TEXT,
    -- FX snapshot (issue #4): all three or none, and the base amount must be
    -- the exact product — enforced by chk_entry_fx_consistent below so the
    -- invariant survives any client, not just the engine (Law 3)
    currency     CHAR(3) REFERENCES currencies(code),
    amount_fx    NUMERIC(26, 8)
                 CONSTRAINT chk_entry_amount_fx_positive CHECK (amount_fx > 0),
    fx_rate      NUMERIC(30, 10)
                 CONSTRAINT chk_entry_fx_rate_positive CHECK (fx_rate > 0),
    CONSTRAINT uq_entry_line UNIQUE (voucher_id, line_no),
    CONSTRAINT chk_entry_fx_consistent CHECK (
        (currency IS NULL AND amount_fx IS NULL AND fx_rate IS NULL)
        OR (
            currency IS NOT NULL
            AND amount_fx IS NOT NULL
            AND fx_rate IS NOT NULL
            AND amount = round(amount_fx * fx_rate, 8)
        )
    )
);

-- The covering index every balance / report query uses (book-led).
CREATE INDEX IF NOT EXISTS idx_entries_ledger_date
    ON voucher_entries (book_id, ledger_id, voucher_date) INCLUDE (dc, amount);

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
-- Gapless document numbering — the only shared mutable row in the write
-- path. scope_key is '{book_id}:{voucher_type_id}:{fy}' (ADR-009): every
-- book's SAL-2026-27-00001 starts at 1.
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

-- Variant for code-keyed tables (currencies): no surrogate id, so record_id
-- is NULL and the natural key lives in row_data.
CREATE OR REPLACE FUNCTION trg_audit_master_code() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO audit_log (actor, table_name, operation, record_id, row_data)
    VALUES (
        NULLIF(current_setting('app.actor', true), ''),
        TG_TABLE_NAME,
        TG_OP,
        NULL,
        to_jsonb(CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END)
    );
    RETURN COALESCE(NEW, OLD);
END $$;

DROP TRIGGER IF EXISTS trg_audit_books ON books;
CREATE TRIGGER trg_audit_books
    AFTER INSERT OR UPDATE OR DELETE ON books
    FOR EACH ROW EXECUTE FUNCTION trg_audit_master();

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

DROP TRIGGER IF EXISTS trg_audit_currencies ON currencies;
CREATE TRIGGER trg_audit_currencies
    AFTER INSERT OR UPDATE OR DELETE ON currencies
    FOR EACH ROW EXECUTE FUNCTION trg_audit_master_code();

-- Only corrections are journaled: rate INSERTs are routine daily loads and
-- would flood the audit log; entries snapshot rates, so rows are advisory.
DROP TRIGGER IF EXISTS trg_audit_fx_rates ON fx_rates;
CREATE TRIGGER trg_audit_fx_rates
    AFTER UPDATE OR DELETE ON fx_rates
    FOR EACH ROW EXECUTE FUNCTION trg_audit_master();

-- ----------------------------------------------------------------------------
-- Period locking & financial-year close (issue #5, ADR-008; book-scoped by
-- 0004/ADR-009). Periods are named, non-overlapping ranges WITHIN A BOOK
-- with a status machine; a BEFORE INSERT trigger on vouchers refuses dates
-- in locked/closed periods (and dates no period covers, once at least one
-- period exists for that book). With no periods at all, every date posts —
-- the feature is opt-in per book.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS periods (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id     BIGINT NOT NULL REFERENCES books(id),
    name        TEXT NOT NULL,
    starts_on   DATE NOT NULL,
    ends_on     DATE NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
                CONSTRAINT chk_periods_status CHECK (status IN ('open', 'locked', 'closed')),
    locked_at   TIMESTAMPTZ,
    closed_at   TIMESTAMPTZ,
    closed_by   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_periods_order CHECK (starts_on <= ends_on),
    CONSTRAINT uq_periods_book_name UNIQUE (book_id, name)
);

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

DROP TRIGGER IF EXISTS trg_vouchers_period ON vouchers;
CREATE TRIGGER trg_vouchers_period
    BEFORE INSERT ON vouchers
    FOR EACH ROW EXECUTE FUNCTION trg_vouchers_check_period();

DROP TRIGGER IF EXISTS trg_periods_overlap ON periods;
CREATE TRIGGER trg_periods_overlap
    BEFORE INSERT OR UPDATE ON periods
    FOR EACH ROW EXECUTE FUNCTION trg_periods_no_overlap();

DROP TRIGGER IF EXISTS trg_periods_guard ON periods;
CREATE TRIGGER trg_periods_guard
    BEFORE UPDATE ON periods
    FOR EACH ROW EXECUTE FUNCTION trg_periods_guard();

DROP TRIGGER IF EXISTS trg_periods_delete_guard ON periods;
CREATE TRIGGER trg_periods_delete_guard
    BEFORE DELETE ON periods
    FOR EACH ROW EXECUTE FUNCTION trg_periods_delete_guard();

DROP TRIGGER IF EXISTS trg_audit_periods ON periods;
CREATE TRIGGER trg_audit_periods
    AFTER INSERT OR UPDATE OR DELETE ON periods
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
-- Multi-currency (0002): currencies are masters; rates are correctable
-- reference data (no DELETE — a wrong rate is UPDATEd, never erased).
GRANT SELECT, INSERT, UPDATE, DELETE ON currencies TO ledgerforge_app;
GRANT SELECT, INSERT, UPDATE ON fx_rates TO ledgerforge_app;
-- Multi-book (0004): books are masters (archival, never DELETE); the engine
-- never drops a book that owns financial history.
GRANT SELECT, INSERT, UPDATE ON books TO ledgerforge_app;
-- Period locking (0003): control data — never part of financial history.
GRANT SELECT, INSERT, UPDATE, DELETE ON periods TO ledgerforge_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO ledgerforge_app;
