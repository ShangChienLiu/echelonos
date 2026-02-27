"""Add blocking_keys JSONB column to fingerprints table.

Revision ID: 004
Revises: 003
Create Date: 2026-02-27

Adds blocking_keys JSONB column to store Claude-extracted structured
fields (vendor_name, po_number, total_amount, etc.) for dedup post-filtering.
The identity_tokens column is retained for backwards compatibility.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fingerprints",
        sa.Column("blocking_keys", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("fingerprints", "blocking_keys")
