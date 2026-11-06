"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

LedgerForge migrations are handwritten raw SQL (ADR-006): call
``op.execute("...")`` with plain SQL strings. Alembic autogenerate is
intentionally unused — it is blind to the triggers, constraint triggers and
plpgsql functions that enforce Laws 1 and 2. Keep revision ids short and
sortable (0002_add_periods, 0003_fx_rates) and update db/schema.sql +
db/functions.sql in the same change; tests/test_migrations.py enforces the
equivalence.
"""

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
