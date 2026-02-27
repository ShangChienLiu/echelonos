"""Replace SimHash with MinHash + LSH in fingerprints table.

Revision ID: 003
Revises: 002
Create Date: 2026-02-27

Adds minhash_signature and identity_tokens columns to fingerprints.
Drops the ix_fingerprints_simhash index (simhash column kept for
backwards compatibility but is no longer populated).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fingerprints",
        sa.Column("minhash_signature", sa.Text(), nullable=True),
    )
    op.add_column(
        "fingerprints",
        sa.Column("identity_tokens", sa.Text(), nullable=True),
    )
    op.drop_index("ix_fingerprints_simhash", table_name="fingerprints")


def downgrade() -> None:
    op.create_index("ix_fingerprints_simhash", "fingerprints", ["simhash"])
    op.drop_column("fingerprints", "identity_tokens")
    op.drop_column("fingerprints", "minhash_signature")
