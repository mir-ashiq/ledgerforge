"""Voucher meta: the plugin document store (ADR-010).

Until now ``meta`` was request-scoped: plugins read it on the way in, the
engine persisted the *accounting lines* they produced, and the commercial
document itself — the invoice items, the GST config, the workflow tags —
evaporated at commit. A voucher is both an accounting artefact and a
business document; this revision gives the document a durable home.

- ``vouchers.meta JSONB NOT NULL DEFAULT '{}'`` — plugin input plus plugin
  output: transformers may annotate computed totals into the draft meta and
  the engine persists the post-transform draft, so the stored document
  reflects what was actually posted.
- Reversals are new documents and start from empty meta — ``reversal_of``
  is the provenance link, and a credit note carries its own narration.
- The API round-trips it: ``POST /vouchers`` already accepted ``meta``;
  ``GET /vouchers/{id}`` now returns it, and the ``voucher.posted`` domain
  event carries it so after-commit bridges (e-invoicing, BI sync) see the
  complete document, not just its accounting shadow.

This is *not* ERPNext-style custom fields (§8): meta is opaque to the
kernel. Core validation never reads it, ``rpt.*`` functions never query it,
and it carries no schema — plugins own their keys, the kernel only stores
them (ADR-010).

Revision ID: 0005_voucher_meta
Revises: 0004_multi_book
Create Date: 2026-11-06
"""

from alembic import op

revision = "0005_voucher_meta"
down_revision = "0004_multi_book"
branch_labels = None
depends_on = None

# Appended last on purpose: the drift guard fingerprints (relname, attnum)
# order, and ALTER TABLE ADD COLUMN appends — db/schema.sql places the
# column in the same last position so both paths converge (ADR-006).
UPGRADE_SQL = """\
ALTER TABLE vouchers ADD COLUMN meta JSONB NOT NULL DEFAULT '{}';
"""

DOWNGRADE_SQL = """\
ALTER TABLE vouchers DROP COLUMN meta;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
