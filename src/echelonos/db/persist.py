"""Get-or-create / upsert persistence helpers for idempotent ingestion.

Every function queries by the table's unique key first.  If the row exists,
mutable fields are updated in-place; otherwise a new row is created.

Uses SQLAlchemy ORM only (no raw SQL, no ``on_conflict``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from echelonos.db.models import Document, DocumentLink, Obligation, Organization, Page


# ---------------------------------------------------------------------------
# organizations
# ---------------------------------------------------------------------------


def get_or_create_organization(
    db: Session,
    *,
    name: str,
    folder_path: Optional[str] = None,
) -> Organization:
    """Return the existing organization with *name*, or create one."""
    org = db.query(Organization).filter(Organization.name == name).first()
    if org is not None:
        if folder_path is not None:
            org.folder_path = folder_path
        return org

    now = datetime.now(UTC)
    org = Organization(
        id=uuid.uuid4(),
        name=name,
        folder_path=folder_path,
        created_at=now,
        updated_at=now,
    )
    db.add(org)
    db.flush()
    return org


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


def upsert_page(
    db: Session,
    *,
    doc_id: uuid.UUID,
    page_number: int,
    **fields: Any,
) -> Page:
    """Upsert a page keyed on *(doc_id, page_number)*."""
    page = (
        db.query(Page)
        .filter(Page.doc_id == doc_id, Page.page_number == page_number)
        .first()
    )
    if page is not None:
        for key, value in fields.items():
            if hasattr(page, key):
                setattr(page, key, value)
        return page

    now = datetime.now(UTC)
    page = Page(
        id=uuid.uuid4(),
        doc_id=doc_id,
        page_number=page_number,
        created_at=now,
        **fields,
    )
    db.add(page)
    db.flush()
    return page


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


def upsert_document(
    db: Session,
    *,
    org_id: uuid.UUID,
    file_path: str,
    **fields: Any,
) -> Document:
    """Upsert a document keyed on *(org_id, file_path)*.

    Any extra *fields* (e.g. ``filename``, ``status``, ``doc_type``) are set
    on both create and update.
    """
    doc = (
        db.query(Document)
        .filter(Document.org_id == org_id, Document.file_path == file_path)
        .first()
    )
    if doc is not None:
        for key, value in fields.items():
            if hasattr(doc, key):
                setattr(doc, key, value)
        return doc

    now = datetime.now(UTC)
    doc = Document(
        id=uuid.uuid4(),
        org_id=org_id,
        file_path=file_path,
        created_at=now,
        updated_at=now,
        **fields,
    )
    db.add(doc)
    db.flush()
    return doc


# ---------------------------------------------------------------------------
# obligations
# ---------------------------------------------------------------------------


def upsert_obligation(
    db: Session,
    *,
    doc_id: uuid.UUID,
    source_clause: str,
    obligation_text: str,
    **fields: Any,
) -> Obligation:
    """Upsert an obligation keyed on *(doc_id, source_clause, obligation_text)*."""
    obl = (
        db.query(Obligation)
        .filter(
            Obligation.doc_id == doc_id,
            Obligation.source_clause == source_clause,
            Obligation.obligation_text == obligation_text,
        )
        .first()
    )
    if obl is not None:
        for key, value in fields.items():
            if hasattr(obl, key):
                setattr(obl, key, value)
        return obl

    now = datetime.now(UTC)
    obl = Obligation(
        id=uuid.uuid4(),
        doc_id=doc_id,
        source_clause=source_clause,
        obligation_text=obligation_text,
        created_at=now,
        updated_at=now,
        **fields,
    )
    db.add(obl)
    db.flush()
    return obl


# ---------------------------------------------------------------------------
# document_links
# ---------------------------------------------------------------------------


def upsert_document_link(
    db: Session,
    *,
    child_doc_id: uuid.UUID,
    parent_doc_id: Optional[uuid.UUID] = None,
    **fields: Any,
) -> DocumentLink:
    """Upsert a document link keyed on *(child_doc_id, parent_doc_id)*."""
    link = (
        db.query(DocumentLink)
        .filter(
            DocumentLink.child_doc_id == child_doc_id,
            DocumentLink.parent_doc_id == parent_doc_id,
        )
        .first()
    )
    if link is not None:
        for key, value in fields.items():
            if hasattr(link, key):
                setattr(link, key, value)
        return link

    now = datetime.now(UTC)
    link = DocumentLink(
        id=uuid.uuid4(),
        child_doc_id=child_doc_id,
        parent_doc_id=parent_doc_id,
        created_at=now,
        **fields,
    )
    db.add(link)
    db.flush()
    return link
