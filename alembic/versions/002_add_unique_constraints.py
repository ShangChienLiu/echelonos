"""Add unique constraints for idempotent ingestion.

Revision ID: 002
Revises: 001
Create Date: 2026-02-27

Adds unique constraints to:
    - organizations (name)
    - documents (org_id, file_path)
    - obligations (doc_id, source_clause, obligation_text)
    - document_links (child_doc_id, parent_doc_id)
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_organizations_name", "organizations", ["name"]
    )
    op.create_unique_constraint(
        "uq_documents_org_file_path", "documents", ["org_id", "file_path"]
    )
    op.create_unique_constraint(
        "uq_obligations_doc_clause_text",
        "obligations",
        ["doc_id", "source_clause", "obligation_text"],
    )
    op.create_unique_constraint(
        "uq_document_links_child_parent",
        "document_links",
        ["child_doc_id", "parent_doc_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_document_links_child_parent", "document_links", type_="unique")
    op.drop_constraint("uq_obligations_doc_clause_text", "obligations", type_="unique")
    op.drop_constraint("uq_documents_org_file_path", "documents", type_="unique")
    op.drop_constraint("uq_organizations_name", "organizations", type_="unique")
