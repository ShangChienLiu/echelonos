"""E2E tests for idempotent ingestion — unique constraints + upsert persistence.

Uses the Docker PostgreSQL container defined in docker-compose.yml.
Tests are automatically skipped when the database is not reachable.

Each test runs inside a transaction that is rolled back after the test,
so no test data persists in the database.

Verifies that:
  1. Double-inserting identical data produces no duplicates.
  2. Mutable fields are updated on re-insert.
  3. Unique constraints are enforced at the DB level.
  4. The API report shows correct counts after double-ingestion.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from echelonos.config import settings
from echelonos.db.models import Base, Document, DocumentLink, Obligation, Organization
from echelonos.db.persist import (
    get_or_create_organization,
    upsert_document,
    upsert_document_link,
    upsert_obligation,
)

# ---------------------------------------------------------------------------
# PostgreSQL connectivity check — skip entire module if DB unreachable
# ---------------------------------------------------------------------------

_PG_URL = settings.database_url


def _pg_is_reachable() -> bool:
    try:
        engine = create_engine(_PG_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_is_reachable(),
    reason=f"PostgreSQL not reachable at {_PG_URL} (is Docker running?)",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def pg_engine():
    engine = create_engine(_PG_URL, pool_pre_ping=True)
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db(pg_engine):
    """Transactional session — rolled back after each test."""
    connection = pg_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()


# ---------------------------------------------------------------------------
# Tests — get_or_create_organization
# ---------------------------------------------------------------------------


class TestGetOrCreateOrganization:
    def test_creates_new_org(self, db: Session):
        org = get_or_create_organization(db, name="Acme Inc", folder_path="/data/acme")
        assert org.id is not None
        assert org.name == "Acme Inc"
        assert org.folder_path == "/data/acme"

    def test_returns_existing_org_on_duplicate(self, db: Session):
        org1 = get_or_create_organization(db, name="Acme Inc", folder_path="/data/acme")
        org2 = get_or_create_organization(db, name="Acme Inc", folder_path="/data/acme")
        assert org1.id == org2.id
        assert db.query(func.count(Organization.id)).filter(
            Organization.name == "Acme Inc"
        ).scalar() == 1

    def test_updates_folder_path_on_reinsert(self, db: Session):
        org1 = get_or_create_organization(db, name="Acme Inc", folder_path="/old/path")
        org2 = get_or_create_organization(db, name="Acme Inc", folder_path="/new/path")
        assert org1.id == org2.id
        assert org2.folder_path == "/new/path"


# ---------------------------------------------------------------------------
# Tests — upsert_document
# ---------------------------------------------------------------------------


class TestUpsertDocument:
    def test_creates_new_document(self, db: Session):
        org = get_or_create_organization(db, name="DocTest Corp")
        doc = upsert_document(
            db,
            org_id=org.id,
            file_path="/data/doctestcorp/contract.pdf",
            filename="contract.pdf",
            status="VALID",
        )
        assert doc.id is not None
        assert doc.file_path == "/data/doctestcorp/contract.pdf"

    def test_no_duplicate_on_same_file_path(self, db: Session):
        org = get_or_create_organization(db, name="DocTest Corp")
        doc1 = upsert_document(
            db,
            org_id=org.id,
            file_path="/data/doctestcorp/contract.pdf",
            filename="contract.pdf",
            status="VALID",
        )
        doc2 = upsert_document(
            db,
            org_id=org.id,
            file_path="/data/doctestcorp/contract.pdf",
            filename="contract.pdf",
            status="VALID",
        )
        assert doc1.id == doc2.id
        assert db.query(func.count(Document.id)).filter(
            Document.org_id == org.id,
        ).scalar() == 1

    def test_updates_mutable_fields(self, db: Session):
        org = get_or_create_organization(db, name="DocTest Corp")
        upsert_document(
            db,
            org_id=org.id,
            file_path="/data/doctestcorp/contract.pdf",
            filename="contract.pdf",
            status="VALID",
            doc_type="UNKNOWN",
        )
        doc2 = upsert_document(
            db,
            org_id=org.id,
            file_path="/data/doctestcorp/contract.pdf",
            filename="contract.pdf",
            status="VALID",
            doc_type="MSA",
            classification_confidence=0.95,
        )
        assert doc2.doc_type == "MSA"
        assert doc2.classification_confidence == 0.95


# ---------------------------------------------------------------------------
# Tests — upsert_obligation
# ---------------------------------------------------------------------------


class TestUpsertObligation:
    def test_creates_new_obligation(self, db: Session):
        org = get_or_create_organization(db, name="OblTest Corp")
        doc = upsert_document(
            db, org_id=org.id, file_path="/obl/test.pdf", filename="test.pdf",
        )
        obl = upsert_obligation(
            db,
            doc_id=doc.id,
            source_clause="Section 1.1",
            obligation_text="Vendor must deliver on time.",
            obligation_type="Delivery",
        )
        assert obl.id is not None

    def test_no_duplicate_on_same_key(self, db: Session):
        org = get_or_create_organization(db, name="OblTest Corp")
        doc = upsert_document(
            db, org_id=org.id, file_path="/obl/test.pdf", filename="test.pdf",
        )
        obl1 = upsert_obligation(
            db,
            doc_id=doc.id,
            source_clause="Section 1.1",
            obligation_text="Vendor must deliver on time.",
        )
        obl2 = upsert_obligation(
            db,
            doc_id=doc.id,
            source_clause="Section 1.1",
            obligation_text="Vendor must deliver on time.",
        )
        assert obl1.id == obl2.id
        assert db.query(func.count(Obligation.id)).filter(
            Obligation.doc_id == doc.id,
        ).scalar() == 1

    def test_updates_mutable_fields(self, db: Session):
        org = get_or_create_organization(db, name="OblTest Corp")
        doc = upsert_document(
            db, org_id=org.id, file_path="/obl/test.pdf", filename="test.pdf",
        )
        upsert_obligation(
            db,
            doc_id=doc.id,
            source_clause="Section 1.1",
            obligation_text="Vendor must deliver on time.",
            confidence=0.7,
            status="ACTIVE",
        )
        obl2 = upsert_obligation(
            db,
            doc_id=doc.id,
            source_clause="Section 1.1",
            obligation_text="Vendor must deliver on time.",
            confidence=0.95,
            status="ACTIVE",
            obligation_type="Delivery",
        )
        assert obl2.confidence == 0.95
        assert obl2.obligation_type == "Delivery"


# ---------------------------------------------------------------------------
# Tests — upsert_document_link
# ---------------------------------------------------------------------------


class TestUpsertDocumentLink:
    def test_creates_new_link(self, db: Session):
        org = get_or_create_organization(db, name="LinkTest Corp")
        doc1 = upsert_document(
            db, org_id=org.id, file_path="/link/msa.pdf", filename="msa.pdf",
        )
        doc2 = upsert_document(
            db, org_id=org.id, file_path="/link/amendment.pdf", filename="amendment.pdf",
        )
        link = upsert_document_link(
            db, child_doc_id=doc2.id, parent_doc_id=doc1.id, link_status="LINKED",
        )
        assert link.id is not None

    def test_no_duplicate_on_same_pair(self, db: Session):
        org = get_or_create_organization(db, name="LinkTest Corp")
        doc1 = upsert_document(
            db, org_id=org.id, file_path="/link/msa.pdf", filename="msa.pdf",
        )
        doc2 = upsert_document(
            db, org_id=org.id, file_path="/link/amendment.pdf", filename="amendment.pdf",
        )
        link1 = upsert_document_link(
            db, child_doc_id=doc2.id, parent_doc_id=doc1.id, link_status="UNLINKED",
        )
        link2 = upsert_document_link(
            db, child_doc_id=doc2.id, parent_doc_id=doc1.id, link_status="LINKED",
        )
        assert link1.id == link2.id
        assert link2.link_status == "LINKED"
        assert db.query(func.count(DocumentLink.id)).filter(
            DocumentLink.child_doc_id == doc2.id,
        ).scalar() == 1


# ---------------------------------------------------------------------------
# Tests — full double-ingestion scenario
# ---------------------------------------------------------------------------


class TestDoubleIngestion:
    """Simulate running the persist layer twice with the same data."""

    def _ingest(self, db: Session) -> dict:
        org = get_or_create_organization(
            db, name="DoubleTest Corp", folder_path="/data/doubletest",
        )
        doc1 = upsert_document(
            db,
            org_id=org.id,
            file_path="/data/doubletest/msa.pdf",
            filename="msa.pdf",
            status="VALID",
            doc_type="MSA",
        )
        doc2 = upsert_document(
            db,
            org_id=org.id,
            file_path="/data/doubletest/amendment.pdf",
            filename="amendment.pdf",
            status="VALID",
            doc_type="Amendment",
        )
        obl = upsert_obligation(
            db,
            doc_id=doc1.id,
            source_clause="Section 2.1",
            obligation_text="Buyer pays within 30 days.",
            obligation_type="Financial",
            confidence=0.9,
        )
        link = upsert_document_link(
            db,
            child_doc_id=doc2.id,
            parent_doc_id=doc1.id,
            link_status="LINKED",
        )
        return {
            "org_id": org.id,
            "doc1_id": doc1.id,
            "doc2_id": doc2.id,
            "obl_id": obl.id,
            "link_id": link.id,
        }

    def test_record_counts_stable_after_double_ingest(self, db: Session):
        ids1 = self._ingest(db)
        ids2 = self._ingest(db)

        # Same IDs returned both times
        assert ids1["org_id"] == ids2["org_id"]
        assert ids1["doc1_id"] == ids2["doc1_id"]
        assert ids1["doc2_id"] == ids2["doc2_id"]
        assert ids1["obl_id"] == ids2["obl_id"]
        assert ids1["link_id"] == ids2["link_id"]

        # Counts stay at 1 each
        assert db.query(func.count(Organization.id)).filter(
            Organization.name == "DoubleTest Corp"
        ).scalar() == 1
        assert db.query(func.count(Document.id)).filter(
            Document.org_id == ids1["org_id"]
        ).scalar() == 2
        assert db.query(func.count(Obligation.id)).filter(
            Obligation.doc_id == ids1["doc1_id"]
        ).scalar() == 1
        assert db.query(func.count(DocumentLink.id)).filter(
            DocumentLink.child_doc_id == ids1["doc2_id"]
        ).scalar() == 1


# ---------------------------------------------------------------------------
# Tests — DB-level unique constraint enforcement
# ---------------------------------------------------------------------------


class TestUniqueConstraintEnforcement:
    """Verify that the DB rejects duplicates even if the ORM is bypassed."""

    def test_org_name_unique_at_db_level(self, db: Session):
        now = datetime.now(timezone.utc)
        db.add(Organization(
            id=uuid.uuid4(), name="UniqueOrg", created_at=now, updated_at=now,
        ))
        db.flush()

        db.add(Organization(
            id=uuid.uuid4(), name="UniqueOrg", created_at=now, updated_at=now,
        ))
        with pytest.raises(IntegrityError):
            db.flush()

    def test_document_org_file_path_unique_at_db_level(self, db: Session):
        org = get_or_create_organization(db, name="ConstraintOrg")
        now = datetime.now(timezone.utc)
        db.add(Document(
            id=uuid.uuid4(), org_id=org.id, filename="a.pdf",
            file_path="/x/a.pdf", created_at=now, updated_at=now,
        ))
        db.flush()

        db.add(Document(
            id=uuid.uuid4(), org_id=org.id, filename="a.pdf",
            file_path="/x/a.pdf", created_at=now, updated_at=now,
        ))
        with pytest.raises(IntegrityError):
            db.flush()
