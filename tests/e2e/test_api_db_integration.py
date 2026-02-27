"""E2E tests for API endpoints wired to real PostgreSQL data.

Verifies that:
  1. /api/organizations returns orgs from the database.
  2. /api/report/{org_name} returns a real report when the org exists in DB.
  3. /api/report/{org_name} falls back to demo data for unknown orgs.
  4. Sub-endpoints (/obligations, /flags, /summary) work with real data.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from echelonos.db.models import Base, Document, DocumentLink, Obligation, Organization
from echelonos.api.app import app, get_db


# ---------------------------------------------------------------------------
# SQLite compatibility — JSONB is PostgreSQL-only; render as JSON for tests
# ---------------------------------------------------------------------------

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON


@event.listens_for(Base.metadata, "before_create")
def _use_json_for_jsonb(target, connection, **kw):
    """Swap JSONB columns to JSON when using a non-PostgreSQL dialect."""
    if connection.dialect.name != "postgresql":
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = JSON()


# ---------------------------------------------------------------------------
# Fixtures — in-memory SQLite database for isolated tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session():
    """Create an in-memory SQLite database with the full schema."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def seeded_db(db_session: Session):
    """Seed the test database with a realistic organization, documents,
    obligations, and links."""
    org = Organization(
        id=uuid.uuid4(),
        name="Test Corp",
        folder_path="/tmp/test-corp",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db_session.add(org)
    db_session.flush()

    doc1 = Document(
        id=uuid.uuid4(),
        org_id=org.id,
        filename="TestCorp_MSA_2024.pdf",
        file_path="/tmp/test-corp/TestCorp_MSA_2024.pdf",
        status="VALID",
        doc_type="MSA",
        classification_confidence=0.95,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    doc2 = Document(
        id=uuid.uuid4(),
        org_id=org.id,
        filename="TestCorp_Amendment1_2024.pdf",
        file_path="/tmp/test-corp/TestCorp_Amendment1_2024.pdf",
        status="VALID",
        doc_type="Amendment",
        classification_confidence=0.91,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db_session.add_all([doc1, doc2])
    db_session.flush()

    obl1 = Obligation(
        id=uuid.uuid4(),
        doc_id=doc1.id,
        obligation_text="Vendor shall deliver monthly reports.",
        obligation_type="Delivery",
        responsible_party="Vendor",
        counterparty="Test Corp",
        source_clause="Section 4.2 - Reporting",
        status="ACTIVE",
        frequency="Monthly",
        deadline="5th business day",
        confidence=0.95,
        verification_result={"verified": True},
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    obl2 = Obligation(
        id=uuid.uuid4(),
        doc_id=doc1.id,
        obligation_text="Buyer shall pay within 30 days.",
        obligation_type="Financial",
        responsible_party="Test Corp",
        counterparty="Vendor",
        source_clause="Section 6.1 - Payment",
        status="ACTIVE",
        frequency="Per invoice",
        deadline="Net 30",
        confidence=0.98,
        verification_result={"verified": True},
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    obl3 = Obligation(
        id=uuid.uuid4(),
        doc_id=doc2.id,
        obligation_text="Vendor shall provide on-site support.",
        obligation_type="Delivery",
        responsible_party="Vendor",
        counterparty="Test Corp",
        source_clause="Section 5.8 - Support",
        status="UNRESOLVED",
        frequency=None,
        deadline="120 days",
        confidence=0.72,
        verification_result={"verified": False, "reason": "Clause not grounded"},
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db_session.add_all([obl1, obl2, obl3])
    db_session.flush()

    link = DocumentLink(
        id=uuid.uuid4(),
        child_doc_id=doc2.id,
        parent_doc_id=doc1.id,
        link_status="LINKED",
        candidates={"confidence": 0.92},
        created_at=datetime.utcnow(),
    )
    db_session.add(link)
    db_session.commit()

    return db_session


@pytest.fixture()
def client(seeded_db: Session):
    """FastAPI test client with the DB dependency overridden."""
    def _override_get_db():
        try:
            yield seeded_db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def client_empty_db(db_session: Session):
    """FastAPI test client with an empty DB (no seeded data)."""
    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests — /api/organizations
# ---------------------------------------------------------------------------


class TestOrganizationsEndpoint:
    def test_returns_orgs_from_db(self, client: TestClient):
        resp = client.get("/api/organizations")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        names = [o["name"] for o in data]
        assert "Test Corp" in names

    def test_returns_empty_list_when_no_orgs(self, client_empty_db: TestClient):
        resp = client_empty_db.get("/api/organizations")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Tests — /api/report/{org_name} with real data
# ---------------------------------------------------------------------------


class TestReportEndpointRealData:
    def test_full_report_from_db(self, client: TestClient):
        resp = client.get("/api/report/Test Corp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["org_name"] == "Test Corp"
        assert data["total_obligations"] == 3
        assert data["active_obligations"] == 2
        assert data["unresolved_obligations"] == 1
        assert len(data["obligations"]) == 3
        assert len(data["flags"]) > 0

    def test_case_insensitive_org_lookup(self, client: TestClient):
        resp = client.get("/api/report/test corp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["org_name"] == "test corp"
        assert data["total_obligations"] == 3

    def test_unknown_org_falls_back_to_demo(self, client: TestClient):
        resp = client.get("/api/report/nonexistent-org")
        assert resp.status_code == 200
        data = resp.json()
        assert data["org_name"] == "nonexistent-org"
        # Demo data has 10 obligations
        assert data["total_obligations"] == 10

    def test_obligations_sub_endpoint(self, client: TestClient):
        resp = client.get("/api/report/Test Corp/obligations")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 3

    def test_flags_sub_endpoint(self, client: TestClient):
        resp = client.get("/api/report/Test Corp/flags")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_summary_sub_endpoint(self, client: TestClient):
        resp = client.get("/api/report/Test Corp/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "by_type" in data
        assert "by_status" in data
        assert "by_party" in data


# ---------------------------------------------------------------------------
# Tests — health check still works
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def test_health(self, client: TestClient):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
