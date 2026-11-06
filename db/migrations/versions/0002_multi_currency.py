"""Multi-currency support: currencies, fx_rates, entry-level FX snapshot (issue #4).

ADR-007. Three ideas:

- Rates are ``NUMERIC(30, 10)`` reference data (``currencies`` + ``fx_rates``);
  the book keeps a single base currency (``settings.base_currency``).
- Every entry still posts a base-currency ``amount`` — that is what balances
  (Law 1) and what the reports aggregate. A foreign-currency line snapshots
  the trio (``currency``, ``amount_fx``, ``fx_rate``) on the immutable entry
  row, so later rate changes can never restate history (Law 2).
- The database itself proves the snapshot consistent:
  ``amount = round(amount_fx * fx_rate, 8)`` — PostgreSQL numeric rounding is
  half-away-from-zero, which the engine mirrors with ROUND_HALF_UP (Law 3).

Downgrade drops the FX columns (their checks/FK go with them) and both new
tables; voucher history is untouched — base amounts were always self-sufficient.

Revision ID: 0002_multi_currency
Revises: 0001_baseline
Create Date: 2026-11-06
"""

from alembic import op

revision = "0002_multi_currency"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

# -- db/schema.sql (multi-currency section) — kept in sync by tests/test_migrations.py
UPGRADE_SQL = """\
-- Currencies: ISO 4217 reference data. Master-style CRUD, audited; entries
-- reference them by code, so a used currency can never be deleted (FK).
CREATE TABLE currencies (
    code        CHAR(3) PRIMARY KEY,
    name        TEXT NOT NULL,
    symbol      TEXT,
    minor_unit  SMALLINT NOT NULL DEFAULT 2
                CONSTRAINT chk_currencies_minor_unit CHECK (minor_unit BETWEEN 0 AND 8),
    is_system   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- FX rates: dated pairs, unique per (from, to, day). Advisory input data for
-- posting UIs — the rate actually applied is snapshotted on the entry row.
CREATE TABLE fx_rates (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_ccy    CHAR(3) NOT NULL REFERENCES currencies(code),
    to_ccy      CHAR(3) NOT NULL REFERENCES currencies(code),
    rate        NUMERIC(30, 10) NOT NULL
                CONSTRAINT chk_fx_rates_positive CHECK (rate > 0),
    valid_on    DATE NOT NULL,
    source      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_fx_rates_pair_day UNIQUE (from_ccy, to_ccy, valid_on),
    CONSTRAINT chk_fx_rates_pair_distinct CHECK (from_ccy <> to_ccy)
);

-- Entry-level FX snapshot: all three fields or none, and the base amount
-- must equal the product of the foreign amount and the rate (both rounded
-- per PostgreSQL numeric semantics — see the migration docstring).
ALTER TABLE voucher_entries
    ADD COLUMN currency  CHAR(3) REFERENCES currencies(code),
    ADD COLUMN amount_fx NUMERIC(26, 8)
        CONSTRAINT chk_entry_amount_fx_positive CHECK (amount_fx > 0),
    ADD COLUMN fx_rate   NUMERIC(30, 10)
        CONSTRAINT chk_entry_fx_rate_positive CHECK (fx_rate > 0);

ALTER TABLE voucher_entries
    ADD CONSTRAINT chk_entry_fx_consistent CHECK (
        (currency IS NULL AND amount_fx IS NULL AND fx_rate IS NULL)
        OR (
            currency IS NOT NULL
            AND amount_fx IS NOT NULL
            AND fx_rate IS NOT NULL
            AND amount = round(amount_fx * fx_rate, 8)
        )
    );

-- Currencies are code-keyed (ISO 4217): audited with the code variant.
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

CREATE TRIGGER trg_audit_currencies
    AFTER INSERT OR UPDATE OR DELETE ON currencies
    FOR EACH ROW EXECUTE FUNCTION trg_audit_master_code();

-- Only corrections are journaled: rate INSERTs are routine daily loads and
-- would flood the audit log; entries snapshot rates, so rows are advisory.
CREATE TRIGGER trg_audit_fx_rates
    AFTER UPDATE OR DELETE ON fx_rates
    FOR EACH ROW EXECUTE FUNCTION trg_audit_master();

GRANT SELECT, INSERT, UPDATE, DELETE ON currencies TO ledgerforge_app;
GRANT SELECT, INSERT, UPDATE ON fx_rates TO ledgerforge_app;
GRANT USAGE ON SEQUENCE fx_rates_id_seq TO ledgerforge_app;
"""

DOWNGRADE_SQL = """\
ALTER TABLE voucher_entries
    DROP COLUMN IF EXISTS currency,
    DROP COLUMN IF EXISTS amount_fx,
    DROP COLUMN IF EXISTS fx_rate;
DROP TABLE IF EXISTS fx_rates;
DROP TABLE IF EXISTS currencies;
DROP FUNCTION IF EXISTS trg_audit_master_code();
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
