"""SQLAlchemy 2.0 ORM models for the EchelonOS contract obligation extraction pipeline."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all EchelonOS models."""

    pass


# ---------------------------------------------------------------------------
# organizations
# ---------------------------------------------------------------------------


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("name", name="uq_organizations_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(nullable=False)
    folder_path: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    # relationships
    documents: Mapped[list[Document]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("org_id", "file_path", name="uq_documents_org_file_path"),
        Index("ix_documents_parties", "parties", postgresql_using="gin"),
        Index("ix_documents_effective_date", "effective_date"),
        Index("ix_documents_org_id", "org_id"),
        Index("ix_documents_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(nullable=False)
    file_path: Mapped[str] = mapped_column(nullable=False)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        nullable=False, default="VALID", comment="VALID | INVALID | NEEDS_PASSWORD"
    )
    doc_type: Mapped[str] = mapped_column(
        nullable=False,
        default="UNKNOWN",
        comment="MSA | SOW | Amendment | Addendum | NDA | Order Form | Other | UNKNOWN",
    )
    parties: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    effective_date: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    parent_reference_raw: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    classification_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    # relationships
    organization: Mapped[Organization] = relationship(back_populates="documents")
    fingerprint: Mapped[Optional[Fingerprint]] = relationship(
        back_populates="document", cascade="all, delete-orphan", uselist=False
    )
    pages: Mapped[list[Page]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    obligations: Mapped[list[Obligation]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    evidence_rows: Mapped[list[Evidence]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    child_links: Mapped[list[DocumentLink]] = relationship(
        back_populates="child_document",
        foreign_keys="DocumentLink.child_doc_id",
        cascade="all, delete-orphan",
    )
    parent_links: Mapped[list[DocumentLink]] = relationship(
        back_populates="parent_document",
        foreign_keys="DocumentLink.parent_doc_id",
    )
    dangling_references: Mapped[list[DanglingReference]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# document_links
# ---------------------------------------------------------------------------


class DocumentLink(Base):
    __tablename__ = "document_links"
    __table_args__ = (
        UniqueConstraint(
            "child_doc_id", "parent_doc_id",
            name="uq_document_links_child_parent",
        ),
        Index("ix_document_links_child_doc_id", "child_doc_id"),
        Index("ix_document_links_parent_doc_id", "parent_doc_id"),
        Index("ix_document_links_link_status", "link_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    child_doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    parent_doc_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    link_status: Mapped[str] = mapped_column(
        nullable=False, default="UNLINKED", comment="LINKED | UNLINKED | AMBIGUOUS"
    )
    candidates: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, nullable=False)

    # relationships
    child_document: Mapped[Document] = relationship(
        back_populates="child_links", foreign_keys=[child_doc_id]
    )
    parent_document: Mapped[Optional[Document]] = relationship(
        back_populates="parent_links", foreign_keys=[parent_doc_id]
    )


# ---------------------------------------------------------------------------
# fingerprints
# ---------------------------------------------------------------------------


class Fingerprint(Base):
    __tablename__ = "fingerprints"
    __table_args__ = (
        UniqueConstraint("doc_id", name="uq_fingerprints_doc_id"),
        Index("ix_fingerprints_sha256", "sha256"),
        Index("ix_fingerprints_content_hash", "content_hash"),
        Index("ix_fingerprints_structural_fingerprint", "structural_fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    sha256: Mapped[Optional[str]] = mapped_column(nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(nullable=True)
    simhash: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    minhash_signature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    identity_tokens: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    blocking_keys: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    structural_fingerprint: Mapped[Optional[str]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, nullable=False)

    # relationships
    document: Mapped[Document] = relationship(back_populates="fingerprint")


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (
        Index("ix_pages_doc_id_page_number", "doc_id", "page_number", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tables_markdown: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ocr_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, nullable=False)

    # relationships
    document: Mapped[Document] = relationship(back_populates="pages")


# ---------------------------------------------------------------------------
# obligations
# ---------------------------------------------------------------------------


class Obligation(Base):
    __tablename__ = "obligations"
    __table_args__ = (
        UniqueConstraint(
            "doc_id", "source_clause", "obligation_text",
            name="uq_obligations_doc_clause_text",
        ),
        Index("ix_obligations_doc_id", "doc_id"),
        Index("ix_obligations_status", "status"),
        Index("ix_obligations_obligation_type", "obligation_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    obligation_text: Mapped[str] = mapped_column(Text, nullable=False)
    obligation_type: Mapped[Optional[str]] = mapped_column(nullable=True)
    responsible_party: Mapped[Optional[str]] = mapped_column(nullable=True)
    counterparty: Mapped[Optional[str]] = mapped_column(nullable=True)
    frequency: Mapped[Optional[str]] = mapped_column(nullable=True)
    deadline: Mapped[Optional[str]] = mapped_column(nullable=True)
    source_clause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_page: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        nullable=False,
        default="ACTIVE",
        comment="ACTIVE | SUPERSEDED | UNRESOLVED | TERMINATED",
    )
    extraction_model: Mapped[Optional[str]] = mapped_column(nullable=True)
    verification_model: Mapped[Optional[str]] = mapped_column(nullable=True)
    verification_result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    # relationships
    document: Mapped[Document] = relationship(back_populates="obligations")
    evidence_rows: Mapped[list[Evidence]] = relationship(
        back_populates="obligation", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# evidence  (APPEND-ONLY -- no updated_at column)
# ---------------------------------------------------------------------------


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_obligation_id", "obligation_id"),
        Index("ix_evidence_doc_id", "doc_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    obligation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("obligations.id", ondelete="CASCADE"), nullable=False
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    section_reference: Mapped[Optional[str]] = mapped_column(nullable=True)
    source_clause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extraction_model: Mapped[Optional[str]] = mapped_column(nullable=True)
    verification_model: Mapped[Optional[str]] = mapped_column(nullable=True)
    verification_result: Mapped[Optional[str]] = mapped_column(nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    amendment_history: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, nullable=False)
    # NOTE: No updated_at -- this table is append-only by design.

    # relationships
    obligation: Mapped[Obligation] = relationship(back_populates="evidence_rows")
    document: Mapped[Document] = relationship(back_populates="evidence_rows")


# ---------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------


class Flag(Base):
    __tablename__ = "flags"
    __table_args__ = (
        Index("ix_flags_entity", "entity_type", "entity_id"),
        Index("ix_flags_flag_type", "flag_type"),
        Index("ix_flags_resolved", "resolved"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[str] = mapped_column(nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    flag_type: Mapped[str] = mapped_column(
        nullable=False,
        comment="UNVERIFIED | UNLINKED | AMBIGUOUS | UNRESOLVED | LOW_CONFIDENCE",
    )
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


# ---------------------------------------------------------------------------
# dangling_references
# ---------------------------------------------------------------------------


class DanglingReference(Base):
    __tablename__ = "dangling_references"
    __table_args__ = (Index("ix_dangling_references_doc_id", "doc_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    reference_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempted_matches: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, nullable=False)

    # relationships
    document: Mapped[Document] = relationship(back_populates="dangling_references")
