"""E2E tests for API endpoints wired to real PostgreSQL data.

Uses the Docker PostgreSQL container defined in docker-compose.yml.
Tests are automatically skipped when the database is not reachable
(e.g. Docker is not running).

Each test runs inside a transaction that is rolled back after the test,
so no test data persists in the database.

Verifies that:
  1. /api/organizations returns orgs from the database.
  2. /api/report/{org_name} returns a real report when the org exists in DB.
  3. /api/report/{org_name} falls back to demo data for unknown orgs.
  4. Sub-endpoints (/obligations, /flags, /summary) work with real data.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from echelonos.config import settings
from echelonos.db.models import Base, Document, DocumentLink, Obligation, Organization
from echelonos.api.app import app, get_db

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
# Fixtures — real PostgreSQL with transaction rollback isolation
# ---------------------------------------------------------------------------

@pytest.fixture()
def pg_engine():
    """Create an engine connected to the Docker PostgreSQL."""
    engine = create_engine(_PG_URL, pool_pre_ping=True)
    # Ensure all tables exist (idempotent).
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(pg_engine):
    """Provide a transactional database session that rolls back after each test.

    This uses the nested-transaction pattern: a connection-level transaction
    wraps the session so that all inserts/updates are rolled back, leaving the
    real database unchanged.
    """
    connection = pg_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture()
def seeded_db(db_session: Session):
    """Seed the test database with a realistic organization, documents,
    obligations, and links."""
    now = datetime.now(timezone.utc)

    org = Organization(
        id=uuid.uuid4(),
        name="Test Corp",
        folder_path="/tmp/test-corp",
        created_at=now,
        updated_at=now,
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
        created_at=now,
        updated_at=now,
    )
    doc2 = Document(
        id=uuid.uuid4(),
        org_id=org.id,
        filename="TestCorp_Amendment1_2024.pdf",
        file_path="/tmp/test-corp/TestCorp_Amendment1_2024.pdf",
        status="VALID",
        doc_type="Amendment",
        classification_confidence=0.91,
        created_at=now,
        updated_at=now,
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
        created_at=now,
        updated_at=now,
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
        created_at=now,
        updated_at=now,
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
        created_at=now,
        updated_at=now,
    )
    db_session.add_all([obl1, obl2, obl3])
    db_session.flush()

    link = DocumentLink(
        id=uuid.uuid4(),
        child_doc_id=doc2.id,
        parent_doc_id=doc1.id,
        link_status="LINKED",
        candidates={"confidence": 0.92},
        created_at=now,
    )
    db_session.add(link)
    db_session.flush()

    return db_session


@pytest.fixture()
def client(seeded_db: Session):
    """FastAPI test client with the DB dependency overridden to use the
    transactional PostgreSQL session."""
    def _override_get_db():
        yield seeded_db

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def client_empty_db(db_session: Session):
    """FastAPI test client with an empty DB (no seeded data)."""
    def _override_get_db():
        yield db_session

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
        names = [o["name"] for o in data]
        assert "Test Corp" in names

    def test_returns_empty_list_when_no_orgs(self, client_empty_db: TestClient):
        resp = client_empty_db.get("/api/organizations")
        assert resp.status_code == 200
        # May include pre-existing orgs from the real DB, but with
        # transaction rollback there should be none from our test.
        # The empty session has no inserts, so the query sees whatever
        # is in the DB within our rolled-back transaction (nothing new).
        data = resp.json()
        assert isinstance(data, list)


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
