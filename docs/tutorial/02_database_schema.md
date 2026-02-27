# Tutorial 02 -- Database Schema: Models, Sessions, and Migrations

> **Linear ticket:** AKS-21 -- DB schema design

---

## Table of Contents

1. [Overview](#overview)
2. [SQLAlchemy 2.0 Patterns Used](#sqlalchemy-20-patterns-used)
3. [File Walkthrough: `db/models.py`](#file-walkthrough-dbmodelspy)
   - [Base Class](#base-class)
   - [Organization Model](#organization-model)
   - [Document Model](#document-model)
   - [DocumentLink Model](#documentlink-model)
   - [Fingerprint Model](#fingerprint-model)
   - [Page Model](#page-model)
   - [Obligation Model](#obligation-model)
   - [Evidence Model](#evidence-model)
   - [Flag Model](#flag-model)
   - [DanglingReference Model](#danglingreference-model)
4. [File Walkthrough: `db/session.py`](#file-walkthrough-dbsessionpy)
5. [File Walkthrough: `db/__init__.py`](#file-walkthrough-db__init__py)
6. [Alembic Migration Setup](#alembic-migration-setup)
7. [Entity Relationship Diagram](#entity-relationship-diagram)
8. [Key Takeaways](#key-takeaways)
9. [Watch Out For](#watch-out-for)

---

## Overview

The Echelonos database layer consists of four files:

| File | Purpose |
|------|---------|
| `src/echelonos/db/models.py` | SQLAlchemy 2.0 ORM models (8 tables) |
| `src/echelonos/db/session.py` | Engine creation, session factory, dependency injection |
| `src/echelonos/db/persist.py` | Idempotent get-or-create / upsert helpers for each table |
| `src/echelonos/db/__init__.py` | Package docstring |

The database is **PostgreSQL 16**, chosen for its JSONB support, GIN indexing, and UUID primary key type. The schema is designed around the 8-stage pipeline: each stage reads from and writes to specific tables.

---

## SQLAlchemy 2.0 Patterns Used

Before diving into the models, here are the SQLAlchemy 2.0 patterns you need to understand:

### 1. `DeclarativeBase` (replaces `declarative_base()`)

```python
class Base(DeclarativeBase):
    pass
```

In SQLAlchemy 2.0, you define a base class by inheriting from `DeclarativeBase` instead of calling the old `declarative_base()` factory function. All model classes inherit from `Base`. Alembic uses `Base.metadata` to detect schema changes.

### 2. `Mapped[T]` + `mapped_column()` (replaces `Column()`)

```python
# SQLAlchemy 2.0 style:
name: Mapped[str] = mapped_column(nullable=False)

# Old style (still works but deprecated for new code):
name = Column(String, nullable=False)
```

`Mapped[T]` is a type annotation that tells both SQLAlchemy and mypy the Python type of the column. `mapped_column()` specifies the database-side configuration (nullable, default, foreign key, etc.). When you use `Mapped[str]`, SQLAlchemy infers the column type as `String`. When you need a specific type (like `Text` or `BigInteger`), pass it as the first argument to `mapped_column()`.

### 3. `Mapped[Optional[T]]` for nullable columns

```python
file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
```

`Optional[int]` tells mypy this can be `None`. `nullable=True` tells the database. Both are needed -- one for Python, one for SQL.

### 4. UUID Primary Keys

```python
id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
)
```

Every table uses UUID v4 primary keys. `UUID(as_uuid=True)` is the PostgreSQL-specific dialect type that stores UUIDs natively (16 bytes) rather than as strings. `default=uuid.uuid4` generates a new UUID when a row is created (note: this is the function reference, not `uuid.uuid4()` -- SQLAlchemy calls it at insert time).

### 5. JSONB Columns with GIN Indexes

```python
parties: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
# ...
Index("ix_documents_parties", "parties", postgresql_using="gin")
```

PostgreSQL's `JSONB` stores JSON documents in a binary format that supports indexing. The GIN (Generalized Inverted Index) on the `parties` column enables fast `@>` (contains) queries. This is how you would query "find all documents where parties include 'CDW Government LLC'".

---

## File Walkthrough: `db/models.py`

**File:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/db/models.py`

### Base Class

```python
"""SQLAlchemy 2.0 ORM models for the EchelonOS contract obligation extraction pipeline."""  # Line 1

from __future__ import annotations                                     # Line 3
```

**Line 3 -- `from __future__ import annotations`:** This is critical. It enables PEP 563 postponed evaluation of annotations, which allows forward references in type hints. Without it, `Mapped[list[Document]]` on line 48 would fail because `Document` is not yet defined at that point in the file. With this import, all annotations are treated as strings and resolved lazily.

```python
import uuid                                                            # Line 5
from datetime import datetime                                          # Line 6
from typing import Optional                                            # Line 7

from sqlalchemy import (                                               # Line 9
    BigInteger, Boolean, Float, ForeignKey, Index, Integer, Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID                 # Line 19
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship  # Line 20
```

**Lines 9-20 -- Imports:**
- From `sqlalchemy`: Column types (`BigInteger`, `Float`, `Text`, `Integer`, `Boolean`), constraints (`ForeignKey`, `Index`, `UniqueConstraint`).
- From `sqlalchemy.dialects.postgresql`: PostgreSQL-specific types (`JSONB`, `UUID`). These only work with PostgreSQL; the schema is not portable to SQLite or MySQL.
- From `sqlalchemy.orm`: The 2.0 ORM building blocks.

```python
class Base(DeclarativeBase):                                           # Line 23
    """Declarative base for all EchelonOS models."""
    pass
```

**Lines 23-26 -- Base class:** All models inherit from this. Alembic's `env.py` imports `Base` (line 7 of `alembic/env.py`) and uses `Base.metadata` as `target_metadata` so that `alembic revision --autogenerate` can detect schema changes.

### Organization Model

```python
class Organization(Base):                                              # Line 34
    __tablename__ = "organizations"                                    # Line 35
    __table_args__ = (
        UniqueConstraint("name", name="uq_organizations_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(                             # Line 40
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(nullable=False)                  # Line 43
    folder_path: Mapped[Optional[str]] = mapped_column(nullable=True)  # Line 44
    created_at: Mapped[datetime] = mapped_column(                      # Line 42
        default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(                      # Line 43-45
        default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    # relationships
    documents: Mapped[list[Document]] = relationship(                  # Line 48
        back_populates="organization", cascade="all, delete-orphan"
    )
```

**Line 35 -- `__tablename__`:** The PostgreSQL table name. Convention: plural, lowercase.

**Lines 36-38 -- `__table_args__`:** A `UniqueConstraint` on `name` ensures that no two organizations can share the same name. The named constraint `uq_organizations_name` makes Alembic migrations and error messages clearer.

**Line 40-42 -- `id` (UUID PK):** Standard UUID v4 primary key pattern used by all models.

**Line 43 -- `name`:** Organization name (required, not nullable, unique via the constraint above).

**Line 44 -- `folder_path`:** The filesystem path to the organization's document folder. `Optional[str]` + `nullable=True` means this can be NULL in the database and `None` in Python.

**Lines 42-45 -- Timestamps:** Both `created_at` and `updated_at` default to `datetime.utcnow`. The `updated_at` column has `onupdate=datetime.utcnow`, which means SQLAlchemy automatically updates it whenever the row is modified.

**Lines 48-50 -- Relationship:** One-to-many with `Document`. `cascade="all, delete-orphan"` means:
- When an Organization is deleted, all its Documents are deleted.
- When a Document is removed from the `documents` list, it is deleted (orphan removal).
- `back_populates="organization"` creates the bidirectional link.

### Document Model

```python
class Document(Base):                                                  # Line 61
    __tablename__ = "documents"                                        # Line 62
    __table_args__ = (                                                 # Line 63
        UniqueConstraint("org_id", "file_path", name="uq_documents_org_file_path"),
        Index("ix_documents_parties", "parties", postgresql_using="gin"),
        Index("ix_documents_effective_date", "effective_date"),
        Index("ix_documents_org_id", "org_id"),
        Index("ix_documents_status", "status"),
    )
```

**Lines 63-69 -- `__table_args__`:** A tuple of table-level constraints and indexes. This is how you define indexes that do not fit on a single column definition.

- **Line 64:** `UniqueConstraint("org_id", "file_path")` ensures that within a single organization, no two documents can share the same `file_path`. This prevents duplicate ingestion of the same file. The named constraint `uq_documents_org_file_path` is referenced by the idempotent upsert logic in `db/persist.py`.
- **Line 65:** GIN index on the `parties` JSONB column. This is the most important index in the schema -- it enables fast lookups like "find all documents involving party X" using PostgreSQL's `@>` operator.
- **Lines 66-68:** B-tree indexes on `effective_date`, `org_id`, and `status` for common query patterns (filtering by org, date range, or pipeline status).

```python
    id: Mapped[uuid.UUID] = mapped_column(                             # Line 67
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(                         # Line 70
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
```

**Lines 70-73 -- `org_id` (Foreign Key):**
- `ForeignKey("organizations.id")` references the Organization table's `id` column using the table name string.
- `ondelete="CASCADE"` means when the parent Organization is deleted, all Documents are deleted at the database level (belt and suspenders with the ORM cascade).

```python
    filename: Mapped[str] = mapped_column(nullable=False)              # Line 73
    file_path: Mapped[str] = mapped_column(nullable=False)             # Line 74
    file_size_bytes: Mapped[Optional[int]] = mapped_column(            # Line 75
        BigInteger, nullable=True
    )
```

**Lines 73-76:** Basic file metadata. `file_size_bytes` uses `BigInteger` (64-bit) because files can exceed 2GB (32-bit int max). `Optional[int]` + `nullable=True` because the size might not be known yet when the Document row is first created.

```python
    status: Mapped[str] = mapped_column(                               # Line 76
        nullable=False,
        default="VALID",
        comment="VALID | INVALID | NEEDS_PASSWORD",                    # Line 78
    )
    doc_type: Mapped[str] = mapped_column(                             # Line 79
        nullable=False,
        default="UNKNOWN",
        comment="MSA | SOW | Amendment | Addendum | NDA | Order Form | Other | UNKNOWN",
    )
```

**Lines 76-83 -- Status and type columns:** These use `str` rather than PostgreSQL `ENUM` types. The `comment` parameter documents the allowed values but does not enforce them at the database level. This is a deliberate choice -- using string columns instead of ENUMs avoids painful Alembic migrations every time a new status or type is added. The enforcement happens in the application layer (Stage 2 classification, Stage 0a validation).

```python
    parties: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)  # Line 84
    effective_date: Mapped[Optional[datetime]] = mapped_column(nullable=True)  # Line 85
    parent_reference_raw: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Line 86
    classification_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Line 87
```

**Lines 84-87 -- Classification metadata:** Populated by Stage 2.
- `parties` is a JSONB column storing a list of party name strings (e.g., `["CDW Government LLC", "State of California"]`). The GIN index on this column (line 61) makes containment queries fast.
- `parent_reference_raw` uses `Text` (unlimited length) because parent references can be long free-text strings.
- `classification_confidence` stores the LLM's confidence score (0.0-1.0).

```python
    # relationships                                                    # Line 93-118
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
```

**Lines 93-118 -- Relationships:** Document is the central entity, with relationships to nearly every other table.

- **`fingerprint` (line 95):** `uselist=False` makes this a one-to-one relationship (one document has one fingerprint). `Optional[Fingerprint]` indicates it may be NULL (not yet computed).
- **`child_links` (line 107):** Links where this document is the CHILD (it references a parent). `foreign_keys="DocumentLink.child_doc_id"` disambiguates which FK to use since DocumentLink has two FKs to Document.
- **`parent_links` (line 112):** Links where this document is the PARENT. Note there is no `cascade="all, delete-orphan"` here -- if a parent document is deleted, the link rows are handled by the child side's cascade.

### DocumentLink Model

```python
class DocumentLink(Base):                                              # Line 130
    __tablename__ = "document_links"                                   # Line 131
    __table_args__ = (                                                 # Line 132
        UniqueConstraint(
            "child_doc_id", "parent_doc_id",
            name="uq_document_links_child_parent",
        ),
        Index("ix_document_links_child_doc_id", "child_doc_id"),
        Index("ix_document_links_parent_doc_id", "parent_doc_id"),
        Index("ix_document_links_link_status", "link_status"),
    )
```

**Lines 130-140:** The join table for the parent-child document relationship created by Stage 4 (Linking). The `UniqueConstraint` on `(child_doc_id, parent_doc_id)` prevents duplicate link records for the same document pair.

```python
    child_doc_id: Mapped[uuid.UUID] = mapped_column(                   # Line 137
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_doc_id: Mapped[Optional[uuid.UUID]] = mapped_column(        # Line 140
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )
```

**Lines 137-143 -- Dual foreign keys:**
- `child_doc_id` is NOT NULL with `ondelete="CASCADE"` -- if the child document is deleted, the link is deleted.
- `parent_doc_id` is NULLABLE with `ondelete="SET NULL"` -- if the parent document is deleted, the link is preserved but the parent reference is set to NULL. This preserves the linking history even when parent documents are removed.

```python
    link_status: Mapped[str] = mapped_column(                          # Line 143
        nullable=False,
        default="UNLINKED",
        comment="LINKED | UNLINKED | AMBIGUOUS",
    )
    candidates: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # Line 146
```

**Line 146 -- `candidates` (JSONB):** When `link_status` is `AMBIGUOUS`, this stores the list of candidate parent documents as JSON. This avoids creating a separate `candidates` table while still preserving the information for human review.

### Fingerprint Model

```python
class Fingerprint(Base):                                               # Line 171
    __tablename__ = "fingerprints"                                     # Line 172
    __table_args__ = (                                                 # Line 173
        UniqueConstraint("doc_id", name="uq_fingerprints_doc_id"),     # Line 174
        Index("ix_fingerprints_sha256", "sha256"),                     # Line 175
        Index("ix_fingerprints_content_hash", "content_hash"),         # Line 176
        Index("ix_fingerprints_structural_fingerprint", "structural_fingerprint"),  # Line 177
    )
```

**Lines 173-178:** Every hash layer from Stage 0b gets its own index. The `UniqueConstraint` on `doc_id` ensures a document can only have one fingerprint row.

```python
    doc_id: Mapped[uuid.UUID] = mapped_column(                         # Line 183
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,                                                   # Line 187
    )
    sha256: Mapped[Optional[str]] = mapped_column(nullable=True)       # Line 189
    content_hash: Mapped[Optional[str]] = mapped_column(nullable=True) # Line 190
    simhash: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # Line 191
    minhash_signature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Line 192
    identity_tokens: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Line 193
    blocking_keys: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # Line 194
    structural_fingerprint: Mapped[Optional[str]] = mapped_column(nullable=True)  # Line 195
```

**Lines 183-195:**
- `unique=True` on line 187 reinforces the unique constraint at the column level (belt and suspenders with the `UniqueConstraint` on line 174).
- `simhash` uses `BigInteger` because SimHash produces a 64-bit integer that can exceed the 32-bit `Integer` range.
- `minhash_signature` stores the MinHash signature as a text field. MinHash is a locality-sensitive hashing technique used for near-duplicate detection -- it estimates the Jaccard similarity between document token sets.
- `blocking_keys` is a JSONB column that stores pre-computed blocking keys used to narrow candidate pairs before the more expensive MinHash comparison.
- All hash columns are `Optional` because fingerprints may be computed incrementally (e.g., SHA-256 first, content hash after OCR).

### Page Model

```python
class Page(Base):                                                      # Line 197
    __tablename__ = "pages"                                            # Line 198
    __table_args__ = (                                                 # Line 199
        Index("ix_pages_doc_id_page_number", "doc_id", "page_number", unique=True),  # Line 200
    )
```

**Line 200 -- Composite unique index:** `(doc_id, page_number)` together must be unique. A document can have page 1, page 2, etc., but cannot have two rows for "document X, page 3". This is a natural key that prevents duplicate page insertions.

```python
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)  # Line 209
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # Line 210
    tables_markdown: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Line 211
    ocr_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Line 212
```

**Lines 209-212:** These columns directly correspond to the output of Stage 1 (OCR). `text` and `tables_markdown` use `Text` type for unlimited length. `ocr_confidence` stores the per-page Azure OCR confidence score.

### Obligation Model

```python
class Obligation(Base):                                                # Line 234
    __tablename__ = "obligations"                                      # Line 235
    __table_args__ = (                                                 # Line 236
        UniqueConstraint(
            "doc_id", "source_clause", "obligation_text",
            name="uq_obligations_doc_clause_text",
        ),
        Index("ix_obligations_doc_id", "doc_id"),
        Index("ix_obligations_status", "status"),
        Index("ix_obligations_obligation_type", "obligation_type"),
    )
```

**Lines 236-244:** A `UniqueConstraint` on `(doc_id, source_clause, obligation_text)` prevents duplicate obligation rows when a pipeline stage is re-run. This is the key used by the `upsert_obligation()` helper in `db/persist.py`. Three additional indexes cover the most common query patterns:
- By `doc_id`: "show all obligations from this document"
- By `status`: "show all ACTIVE obligations"
- By `obligation_type`: "show all SLA obligations"

```python
    obligation_text: Mapped[str] = mapped_column(Text, nullable=False)  # Line 238
    obligation_type: Mapped[Optional[str]] = mapped_column(nullable=True)  # Line 239
    responsible_party: Mapped[Optional[str]] = mapped_column(nullable=True)  # Line 240
    counterparty: Mapped[Optional[str]] = mapped_column(nullable=True)  # Line 241
    frequency: Mapped[Optional[str]] = mapped_column(nullable=True)    # Line 242
    deadline: Mapped[Optional[str]] = mapped_column(nullable=True)     # Line 243
    source_clause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Line 244
    source_page: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Line 245
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Line 246
```

**Lines 238-246:** These columns map directly to the `Obligation` Pydantic model in Stage 3 (`stage_3_extraction.py`, lines 49-60). Every field that Claude extracts has a corresponding database column.

```python
    status: Mapped[str] = mapped_column(                               # Line 247
        nullable=False,
        default="ACTIVE",
        comment="ACTIVE | SUPERSEDED | UNRESOLVED | TERMINATED",       # Line 250
    )
    extraction_model: Mapped[Optional[str]] = mapped_column(nullable=True)  # Line 252
    verification_model: Mapped[Optional[str]] = mapped_column(nullable=True)  # Line 253
    verification_result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # Line 254
```

**Lines 247-254:**
- `status` defaults to `"ACTIVE"` and is updated by Stage 5 (Amendment Resolution) to `"SUPERSEDED"` or `"TERMINATED"`.
- `extraction_model` and `verification_model` record which specific model versions produced the data (e.g., `"gpt-4o-2024-08-06"`, `"claude-sonnet-4-5-20250514"`). This is part of the evidence trail.
- `verification_result` is a JSONB column that stores the full Claude verification response (verified, confidence, reason). JSONB is used because the verification result structure may vary.

### Evidence Model

```python
class Evidence(Base):                                                  # Line 272
    __tablename__ = "evidence"                                         # Line 273
    # NOTE: APPEND-ONLY -- no updated_at column                       # Line 268
```

**Line 268 (comment) -- APPEND-ONLY design:** This is the most important design decision in the schema. The evidence table has **no `updated_at` column**. Once a row is inserted, it is never modified. Status changes create new rows. This preserves a complete, immutable audit trail.

```python
    obligation_id: Mapped[uuid.UUID] = mapped_column(                  # Line 282
        UUID(as_uuid=True),
        ForeignKey("obligations.id", ondelete="CASCADE"),
        nullable=False,
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(                         # Line 285
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    page_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Line 288
    section_reference: Mapped[Optional[str]] = mapped_column(nullable=True)  # Line 289
    source_clause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Line 290
    extraction_model: Mapped[Optional[str]] = mapped_column(nullable=True)  # Line 291
    verification_model: Mapped[Optional[str]] = mapped_column(nullable=True)  # Line 292
    verification_result: Mapped[Optional[str]] = mapped_column(nullable=True)  # Line 293
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Line 294
    amendment_history: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # Line 295
    created_at: Mapped[datetime] = mapped_column(                      # Line 296
        default=datetime.utcnow, nullable=False
    )
    # NOTE: No updated_at -- this table is append-only by design.      # Line 297
```

**Lines 282-297:** The evidence table links an obligation to its provenance:
- `obligation_id` + `doc_id`: Which obligation, from which document.
- `page_number` + `section_reference` + `source_clause`: Exact location in the source.
- `extraction_model` + `verification_model`: Which AI models produced and verified this.
- `amendment_history` (JSONB): Records how amendments have affected this obligation.
- `created_at` but NO `updated_at`: Append-only.

### Flag Model

```python
class Flag(Base):                                                      # Line 309
    __tablename__ = "flags"                                            # Line 310
    __table_args__ = (                                                 # Line 311
        Index("ix_flags_entity", "entity_type", "entity_id"),          # Line 312
        Index("ix_flags_flag_type", "flag_type"),                      # Line 313
        Index("ix_flags_resolved", "resolved"),                        # Line 314
    )
```

**Lines 311-315:** Indexes for the three main query patterns:
- Composite `(entity_type, entity_id)`: "show all flags for obligation X" or "show all flags for document Y"
- `flag_type`: "show all UNVERIFIED flags"
- `resolved`: "show all unresolved flags"

```python
    entity_type: Mapped[str] = mapped_column(nullable=False)           # Line 320
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # Line 321
    flag_type: Mapped[str] = mapped_column(                            # Line 322
        nullable=False,
        comment="UNVERIFIED | UNLINKED | AMBIGUOUS | UNRESOLVED | LOW_CONFIDENCE",
    )
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Line 326
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # Line 327
    resolved_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)  # Line 329
```

**Lines 320-329 -- Polymorphic flagging:** The `(entity_type, entity_id)` pair forms a polymorphic reference:
- `entity_type = "obligation"`, `entity_id = <obligation UUID>` -- flag on an obligation
- `entity_type = "document"`, `entity_id = <document UUID>` -- flag on a document

This avoids needing separate `obligation_flags` and `document_flags` tables. The composite index on `(entity_type, entity_id)` makes lookups efficient.

`resolved` (boolean) + `resolved_at` (nullable timestamp) track whether a human has addressed the flag. Stage 7 generates flags; the UI marks them as resolved.

### DanglingReference Model

```python
class DanglingReference(Base):                                         # Line 337
    __tablename__ = "dangling_references"                              # Line 338
    __table_args__ = (
        Index("ix_dangling_references_doc_id", "doc_id"),              # Line 339
    )

    doc_id: Mapped[uuid.UUID] = mapped_column(                         # Line 344
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    reference_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Line 347
    attempted_matches: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # Line 348
```

**Lines 337-352:** When Stage 4 (Linking) finds a child document that references a parent that does not exist in the corpus, a `DanglingReference` row is created. This enables **backfill** -- when new documents are ingested later, the system checks all dangling references to see if the new document is the missing parent (see `backfill_dangling_references()` in `stage_4_linking.py`).

- `reference_text`: The raw parent reference string (e.g., "MSA dated January 10, 2023").
- `attempted_matches` (JSONB): Records what matching was attempted and why it failed.

---

## File Walkthrough: `db/session.py`

**File:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/db/session.py`

```python
"""Database session and engine configuration."""                       # Line 1

from sqlalchemy import create_engine                                   # Line 3
from sqlalchemy.orm import Session, sessionmaker                       # Line 4
from echelonos.config import settings                                  # Line 6

engine = create_engine(settings.database_url, pool_pre_ping=True)      # Line 8
SessionLocal = sessionmaker(bind=engine)                               # Line 9
```

**Line 8 -- Engine creation:**
- `create_engine()` creates the SQLAlchemy engine from the synchronous database URL (`postgresql://...`).
- `pool_pre_ping=True` is a critical production setting. Before giving out a connection from the pool, SQLAlchemy sends a lightweight "ping" query (`SELECT 1`) to verify the connection is still alive. This prevents "connection reset" errors that occur when the database server has closed idle connections (common with PostgreSQL's `idle_in_transaction_session_timeout` or cloud database proxies).

**Line 9 -- Session factory:**
`sessionmaker(bind=engine)` creates a factory class `SessionLocal`. Calling `SessionLocal()` creates a new database session. This is the standard SQLAlchemy pattern for managing sessions.

```python
def get_db() -> Session:                                               # Line 12
    db = SessionLocal()                                                # Line 13
    try:
        yield db                                                       # Line 15
    finally:
        db.close()                                                     # Line 17
```

**Lines 12-17 -- Dependency injection generator:**
This is a **generator function** (note the `yield` on line 15) designed to be used as a FastAPI dependency or a context manager:

```python
# FastAPI usage:
@app.get("/api/items")
def get_items(db: Session = Depends(get_db)):
    return db.query(Item).all()

# Manual usage:
gen = get_db()
db = next(gen)
try:
    # use db
finally:
    gen.close()  # triggers the finally block in get_db
```

The `try/finally` ensures the session is **always** closed, even if the request handler raises an exception. This prevents connection leaks.

**Note:** This function does not call `db.commit()`. The caller is responsible for committing transactions. This is intentional -- it follows the "unit of work" pattern where the caller decides when to commit.

---

## File Walkthrough: `db/__init__.py`

**File:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/db/__init__.py`

```python
"""Database models and session management."""                          # Line 1
```

Minimal package init. Does not re-export any models or the session factory. Consumers import directly:

```python
from echelonos.db.models import Document, Obligation, Evidence
from echelonos.db.session import get_db, engine
```

---

## File Walkthrough: `db/persist.py`

**File:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/db/persist.py`

This module provides **idempotent upsert helpers** for the most commonly written tables. Each function queries by the table's unique key first; if a matching row exists, mutable fields are updated in place, otherwise a new row is created. This get-or-create pattern ensures that re-running a pipeline stage does not produce duplicate rows.

| Function | Unique Key | Purpose |
|---|---|---|
| `get_or_create_organization()` | `name` | Matches the `uq_organizations_name` constraint |
| `upsert_document()` | `(org_id, file_path)` | Matches the `uq_documents_org_file_path` constraint |
| `upsert_page()` | `(doc_id, page_number)` | Matches the composite unique index on pages |
| `upsert_obligation()` | `(doc_id, source_clause, obligation_text)` | Matches the `uq_obligations_doc_clause_text` constraint |
| `upsert_document_link()` | `(child_doc_id, parent_doc_id)` | Matches the `uq_document_links_child_parent` constraint |

All functions use plain ORM queries (no raw SQL or `ON CONFLICT` clauses) and call `db.flush()` after creating a new row so that the generated UUID is immediately available to the caller.

---

## Alembic Migration Setup

Alembic is configured with two files:

### `alembic.ini`

**File:** `/Users/shangchienliu/Github-local/echelonos/alembic.ini`

**Line 4:** `sqlalchemy.url = postgresql://echelonos:echelonos_dev@localhost:5432/echelonos`

This is the database URL used for migrations. It matches the default settings in `config.py`. In production, this should be overridden -- either by editing the file or by setting the URL programmatically in `alembic/env.py`.

### `alembic/env.py`

**File:** `/Users/shangchienliu/Github-local/echelonos/alembic/env.py`

```python
from echelonos.db.models import Base                                   # Line 7

target_metadata = Base.metadata                                        # Line 12
```

**Lines 7 and 12:** This is the critical connection between Alembic and the models. By importing `Base` and setting `target_metadata = Base.metadata`, Alembic can compare the current database schema against the model definitions and generate migration scripts automatically with:

```bash
alembic revision --autogenerate -m "add new column"
alembic upgrade head
```

**Lines 15-19 -- Offline mode:**
```python
def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
```
Offline mode generates SQL scripts without connecting to the database. Useful for generating migration SQL in CI/CD where the database is not accessible.

**Lines 22-31 -- Online mode:**
```python
def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
```
Online mode connects to the database and applies migrations directly. `poolclass=pool.NullPool` disables connection pooling for the migration runner (it only needs one connection).

---

## Entity Relationship Diagram

```
organizations
    |
    | 1:N
    v
documents --------- fingerprints (1:1)
    |
    |--- 1:N ---> pages
    |
    |--- 1:N ---> obligations -----> evidence (1:N, append-only)
    |
    |--- N:M ---> document_links (self-referential via child_doc_id/parent_doc_id)
    |
    |--- 1:N ---> dangling_references
    |
    (referenced by)
    v
flags (polymorphic: entity_type + entity_id -> obligations OR documents)
```

**Key relationships:**
- Organization -> Documents (1:N, CASCADE delete)
- Document -> Fingerprint (1:1, CASCADE delete)
- Document -> Pages (1:N, CASCADE delete)
- Document -> Obligations (1:N, CASCADE delete)
- Obligation -> Evidence (1:N, CASCADE delete, **append-only**)
- Document -> DocumentLinks (self-referential, child side CASCADEs, parent side SET NULL)
- Document -> DanglingReferences (1:N, CASCADE delete)
- Flag -> (polymorphic reference to any entity type)

---

## Key Takeaways

1. **UUID primary keys everywhere.** Every table uses `UUID(as_uuid=True)` with `default=uuid.uuid4`. This means IDs are globally unique, merge-friendly, and do not leak sequential information. The trade-off is that UUID indexes are larger than integer indexes, but for a contract management system the scale is manageable.

2. **JSONB + GIN for semi-structured data.** The `parties` column on Documents and the `candidates` column on DocumentLinks use JSONB. This avoids creating normalization tables (e.g., a `document_parties` join table) while still supporting indexed queries. The GIN index on `parties` enables fast containment queries.

3. **Append-only evidence table.** The Evidence table has no `updated_at` column. Status changes create new rows via `create_status_change_record()` in Stage 6. This is an audit-trail design where you can reconstruct the full history of any obligation by querying all Evidence rows with that `obligation_id`, ordered by `created_at`.

4. **String columns instead of ENUMs.** Status and type columns are `str` with allowed values documented in `comment=`. This is a pragmatic choice -- PostgreSQL ENUMs require an `ALTER TYPE` migration every time a new value is added, which is notoriously painful. String columns are more flexible.

5. **Polymorphic flags.** The Flag table uses `(entity_type, entity_id)` to flag both obligations and documents with a single table. The composite index makes lookups efficient.

6. **`pool_pre_ping=True`.** This one-line setting on `create_engine` prevents stale connection errors. Always use it in production.

---

## Watch Out For

1. **`datetime.utcnow` is deprecated.** All model timestamps use `default=datetime.utcnow` and `onupdate=datetime.utcnow`. In Python 3.12+, `datetime.utcnow()` raises a deprecation warning. The recommended replacement is `datetime.now(timezone.utc)`. Stage 7 already uses the new pattern (line 484 of `stage_7_report.py`), but the models have not been updated yet. This will need a migration or a default function change.

2. **Sync-only session.** The `session.py` file creates a synchronous engine and session factory. The async database URL (`async_database_url`) defined in `config.py` is not used here. If you need async database access (e.g., for FastAPI async endpoints or Prefect async tasks), you will need to create an `AsyncEngine` and `AsyncSession` separately. Consider adding an `async_session.py` module.

3. **No `updated_at` on Evidence.** This is by design (append-only), but be aware that if you accidentally update an Evidence row via ORM, there is no timestamp that reveals the modification. The only protection is discipline -- the application layer must never issue UPDATE statements on the evidence table.

4. **`ondelete="SET NULL"` on `DocumentLink.parent_doc_id`.** When a parent document is deleted, the link is preserved with `parent_doc_id = NULL`. This means you can have link rows with `link_status = "LINKED"` but `parent_doc_id = NULL`. Downstream code should check for this inconsistency.

5. **No database-level enforcement of status values.** Because status columns are `str` (not `ENUM`), the database accepts any string. If a bug in the application writes `"ACTVE"` (typo) instead of `"ACTIVE"`, the database will not catch it. Consider adding `CHECK` constraints in a migration if you want database-level validation.

6. **`UniqueConstraint` + `unique=True` on Fingerprint.doc_id.** Lines 166 and 180 both enforce uniqueness on `doc_id`. This is redundant but harmless. The `UniqueConstraint` creates a named constraint (`uq_fingerprints_doc_id`) that is easier to reference in error messages and migrations. The `unique=True` on the column is the belt-and-suspenders approach.

7. **`get_db()` does not commit.** The session generator in `session.py` only closes the session on exit. It does not commit. If a FastAPI endpoint or a Prefect task modifies data, it must call `db.commit()` explicitly. Forgetting to commit means changes are silently rolled back when the session closes.
