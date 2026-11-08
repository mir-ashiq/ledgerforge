# ruff: noqa: E501 (generated: the SQL slices keep their source formatting)
"""Stock masters: items, price lists, BOMs (ADR-027).

The third core-schema change since 0006, deliberately — the ADR-025
argument restated for master data: re-order levels, price lists and BOMs
are referenced by name from documents and by report after report; they
need database identity (UNIQUE names per book), referential integrity (a
BOM cannot name a ghost component) and date-effective queries (the price
lookup is one indexed predicate). The MOVEMENTS' depth — batches,
serials, expiry, godowns — stays in vouchers.meta (ADR-011): derived
state, replayed by the fold, never stored.

- ``stock_items`` — the named item: unit, tracking modes (batch/serial,
  TallyPrime's "maintain in" toggles), re-order level and quantity.
  Masters are revisable (UPDATE allowed, the cost-category precedent);
  DELETE is RESTRICTed by BOM references, price rows CASCADE with their
  item.
- ``stock_prices`` — per-party (NULL = list price for everyone),
  date-effective. One functional UNIQUE index pins one rate per
  (book, item, party-slot, date); the effective price is the latest
  ``effective_from <= date``, party-specific beating generic.
- ``stock_boms`` + ``stock_bom_lines`` — the recipe: output item and
  per-run quantity, component items with quantity per output unit.
  Same-book composite FKs throughout (stock_items carries
  UNIQUE (book_id, id)); a component may not be its own BOM's output.

The upgrade blocks are generated from db/schema.sql verbatim; the
downgrade drops everything this revision created.

Revision ID: 0009_stock_depth
Revises: 0008_banking
Create Date: 2026-11-08
"""

from alembic import op

revision = "0009_stock_depth"
down_revision = "0008_banking"
branch_labels = None
depends_on = None

UPGRADE_SQL = """\
-- ----------------------------------------------------------------------------
-- Stock masters (ADR-027, migration 0009) — the third core-schema change
-- since 0006, by the ADR-025 argument restated for master data: a stock
-- item's re-order level, a price-list row and a BOM are MASTERS, not
-- movements — referenced by name from documents and by report after
-- report, stable across thousands of movements. They need database
-- identity (UNIQUE names per book — two rows cannot claim different
-- re-order levels for the same item), referential integrity (a BOM cannot
-- name a component that does not exist) and date-effective queries (the
-- price lookup is one indexed predicate, not a document fold). The
-- MOVEMENTS' depth — batches, serials, expiry, godowns — stays in
-- vouchers.meta exactly as ADR-011 placed it: derived state, replayed by
-- the fold, never stored. The kernel stays meta-blind; the pack reads
-- these tables only through the versioned plugin API (0.12).
--
-- - stock_items — the named item with its tracking modes (batch / serial,
--   TallyPrime's "maintain in" toggles) and re-order levels. Masters are
--   revisable (the cost-category precedent): UPDATE allowed; DELETE is
--   RESTRICTed by BOM references and CASCADEd price rows follow their
--   item.
-- - stock_prices — the price lists: per-party (NULL = the list price for
--   everyone), date-effective. The effective price for (item, party,
--   date) is the row with the latest effective_from <= date, a
--   party-specific row beating the generic one. One functional UNIQUE
--   index pins one rate per (book, item, party-slot, date) — NULL party
--   and NULL-free party share the slot key via COALESCE.
-- - stock_boms + stock_bom_lines — the recipe: an output item and
--   per-run quantity, component items with a quantity per output unit.
--   Output and components are FK-validated stock items of the same book
--   (the composite-FK trick, available here because stock_items carries
--   UNIQUE (book_id, id)); a component may not be its own BOM's output
--   (production that consumes itself is a data error, not a recipe).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_items (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id          BIGINT NOT NULL REFERENCES books(id),
    name             TEXT NOT NULL,
    unit             TEXT,
    track_batches    BOOLEAN NOT NULL DEFAULT FALSE,
    track_serials    BOOLEAN NOT NULL DEFAULT FALSE,
    reorder_level    NUMERIC(26, 8),
    reorder_quantity NUMERIC(26, 8),
    notes            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_stock_items_book_id UNIQUE (book_id, id),
    CONSTRAINT uq_stock_items_book_name UNIQUE (book_id, name),
    CONSTRAINT ck_stock_items_reorder_nonnegative
        CHECK (reorder_level IS NULL OR reorder_level >= 0),
    CONSTRAINT ck_stock_items_reorder_qty_positive
        CHECK (reorder_quantity IS NULL OR reorder_quantity > 0)
);

CREATE TABLE IF NOT EXISTS stock_prices (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id         BIGINT NOT NULL REFERENCES books(id),
    item_id         BIGINT NOT NULL,
    party_ledger_id BIGINT,
    effective_from  DATE NOT NULL,
    rate            NUMERIC(26, 8) NOT NULL CHECK (rate > 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_stock_prices_book_id UNIQUE (book_id, id),
    CONSTRAINT fk_stock_prices_item_same_book FOREIGN KEY (book_id, item_id)
        REFERENCES stock_items (book_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_stock_prices_party FOREIGN KEY (party_ledger_id)
        REFERENCES ledgers (id) ON DELETE RESTRICT
);

-- One rate per slot: (book, item, party|list, date). COALESCE folds the
-- NULL party (the list price) into the same key space as party rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_prices_slot
    ON stock_prices (book_id, item_id, COALESCE(party_ledger_id, 0), effective_from);

CREATE INDEX IF NOT EXISTS idx_stock_prices_item_date ON stock_prices (item_id, effective_from);

CREATE TABLE IF NOT EXISTS stock_boms (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id        BIGINT NOT NULL REFERENCES books(id),
    name           TEXT NOT NULL,
    output_item_id BIGINT NOT NULL,
    output_qty     NUMERIC(26, 8) NOT NULL CHECK (output_qty > 0),
    notes          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_stock_boms_book_id UNIQUE (book_id, id),
    CONSTRAINT uq_stock_boms_book_name UNIQUE (book_id, name),
    CONSTRAINT fk_stock_boms_output_same_book FOREIGN KEY (book_id, output_item_id)
        REFERENCES stock_items (book_id, id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS stock_bom_lines (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id            BIGINT NOT NULL,
    bom_id             BIGINT NOT NULL,
    component_item_id  BIGINT NOT NULL,
    qty_per_output     NUMERIC(26, 8) NOT NULL CHECK (qty_per_output > 0),
    CONSTRAINT uq_stock_boms_lines_book_id UNIQUE (book_id, id),
    CONSTRAINT uq_stock_bom_lines_component UNIQUE (bom_id, component_item_id),
    CONSTRAINT fk_stock_bom_lines_bom_same_book FOREIGN KEY (book_id, bom_id)
        REFERENCES stock_boms (book_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_stock_bom_lines_component_same_book FOREIGN KEY (book_id, component_item_id)
        REFERENCES stock_items (book_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_stock_bom_lines_component ON stock_bom_lines (component_item_id);

-- ----------------------------------------------------------------------------
-- Privileges: masters like cost categories — revisable, never financial
-- history. Prices cascade with their item; items and BOMs are RESTRICTed
-- by their references (the database's own refusal).
-- ----------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON stock_items, stock_prices, stock_boms,
    stock_bom_lines TO ledgerforge_app;
GRANT USAGE ON SEQUENCE stock_items_id_seq, stock_prices_id_seq, stock_boms_id_seq,
    stock_bom_lines_id_seq TO ledgerforge_app;
"""

DOWNGRADE_SQL = """\
-- Privileges first: REVOKEs need the objects to still exist.
REVOKE SELECT, INSERT, UPDATE, DELETE ON stock_items, stock_prices, stock_boms,
    stock_bom_lines FROM ledgerforge_app;
REVOKE USAGE ON SEQUENCE stock_items_id_seq, stock_prices_id_seq, stock_boms_id_seq,
    stock_bom_lines_id_seq FROM ledgerforge_app;

DROP TABLE IF EXISTS stock_bom_lines;
DROP TABLE IF EXISTS stock_boms;
DROP TABLE IF EXISTS stock_prices;
DROP TABLE IF EXISTS stock_items;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
