"""Initial schema for EchelonOS contract obligation extraction pipeline.

Revision ID: 001
Revises:
Create Date: 2026-02-26

Tables created:
    - organizations
    - documents
    - document_links
    - fingerprints
    - pages
    - obligations
    - evidence (append-only)
    - flags
    - dangling_references
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # organizations
    # ------------------------------------------------------------------
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("folder_path", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # ------------------------------------------------------------------
    # documents
    # ------------------------------------------------------------------
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="VALID",
            comment="VALID | INVALID | NEEDS_PASSWORD",
        ),
        sa.Column(
            "doc_type",
            sa.String(),
            nullable=False,
            server_default="UNKNOWN",
            comment="MSA | SOW | Amendment | Addendum | NDA | Order Form | Other | UNKNOWN",
        ),
        sa.Column("parties", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("effective_date", sa.DateTime(), nullable=True),
        sa.Column("parent_reference_raw", sa.Text(), nullable=True),
        sa.Column("classification_confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_documents_org_id", "documents", ["org_id"])
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_index("ix_documents_effective_date", "documents", ["effective_date"])
    op.create_index(
        "ix_documents_parties",
        "documents",
        ["parties"],
        postgresql_using="gin",
    )

    # ------------------------------------------------------------------
    # document_links
    # ------------------------------------------------------------------
    op.create_table(
        "document_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "child_doc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_doc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "link_status",
            sa.String(),
            nullable=False,
            server_default="UNLINKED",
            comment="LINKED | UNLINKED | AMBIGUOUS",
        ),
        sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_document_links_child_doc_id", "document_links", ["child_doc_id"])
    op.create_index("ix_document_links_parent_doc_id", "document_links", ["parent_doc_id"])
    op.create_index("ix_document_links_link_status", "document_links", ["link_status"])

    # ------------------------------------------------------------------
    # fingerprints
    # ------------------------------------------------------------------
    op.create_table(
        "fingerprints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "doc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("sha256", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("simhash", sa.BigInteger(), nullable=True),
        sa.Column("structural_fingerprint", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_unique_constraint("uq_fingerprints_doc_id", "fingerprints", ["doc_id"])
    op.create_index("ix_fingerprints_sha256", "fingerprints", ["sha256"])
    op.create_index("ix_fingerprints_content_hash", "fingerprints", ["content_hash"])
    op.create_index("ix_fingerprints_simhash", "fingerprints", ["simhash"])
    op.create_index(
        "ix_fingerprints_structural_fingerprint",
        "fingerprints",
        ["structural_fingerprint"],
    )

    # ------------------------------------------------------------------
    # pages
    # ------------------------------------------------------------------
    op.create_table(
        "pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "doc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("tables_markdown", sa.Text(), nullable=True),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_pages_doc_id_page_number",
        "pages",
        ["doc_id", "page_number"],
        unique=True,
    )

    # ------------------------------------------------------------------
    # obligations
    # ------------------------------------------------------------------
    op.create_table(
        "obligations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "doc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("obligation_text", sa.Text(), nullable=False),
        sa.Column("obligation_type", sa.String(), nullable=True),
        sa.Column("responsible_party", sa.String(), nullable=True),
        sa.Column("counterparty", sa.String(), nullable=True),
        sa.Column("frequency", sa.String(), nullable=True),
        sa.Column("deadline", sa.String(), nullable=True),
        sa.Column("source_clause", sa.Text(), nullable=True),
        sa.Column("source_page", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="ACTIVE",
            comment="ACTIVE | SUPERSEDED | UNRESOLVED | TERMINATED",
        ),
        sa.Column("extraction_model", sa.String(), nullable=True),
        sa.Column("verification_model", sa.String(), nullable=True),
        sa.Column(
            "verification_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_obligations_doc_id", "obligations", ["doc_id"])
    op.create_index("ix_obligations_status", "obligations", ["status"])
    op.create_index("ix_obligations_obligation_type", "obligations", ["obligation_type"])

    # ------------------------------------------------------------------
    # evidence  (APPEND-ONLY: no updated_at column)
    # ------------------------------------------------------------------
    op.create_table(
        "evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "obligation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("obligations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "doc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section_reference", sa.String(), nullable=True),
        sa.Column("source_clause", sa.Text(), nullable=True),
        sa.Column("extraction_model", sa.String(), nullable=True),
        sa.Column("verification_model", sa.String(), nullable=True),
        sa.Column("verification_result", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "amendment_history",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_evidence_obligation_id", "evidence", ["obligation_id"])
    op.create_index("ix_evidence_doc_id", "evidence", ["doc_id"])

    # ------------------------------------------------------------------
    # flags
    # ------------------------------------------------------------------
    op.create_table(
        "flags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "flag_type",
            sa.String(),
            nullable=False,
            comment="UNVERIFIED | UNLINKED | AMBIGUOUS | UNRESOLVED | LOW_CONFIDENCE",
        ),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_flags_entity", "flags", ["entity_type", "entity_id"])
    op.create_index("ix_flags_flag_type", "flags", ["flag_type"])
    op.create_index("ix_flags_resolved", "flags", ["resolved"])

    # ------------------------------------------------------------------
    # dangling_references
    # ------------------------------------------------------------------
    op.create_table(
        "dangling_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "doc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reference_text", sa.Text(), nullable=True),
        sa.Column(
            "attempted_matches",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_dangling_references_doc_id", "dangling_references", ["doc_id"])


def downgrade() -> None:
    # Drop in reverse dependency order.
    op.drop_table("dangling_references")
    op.drop_table("flags")
    op.drop_table("evidence")
    op.drop_table("obligations")
    op.drop_table("pages")
    op.drop_table("fingerprints")
    op.drop_table("document_links")
    op.drop_table("documents")
    op.drop_table("organizations")
