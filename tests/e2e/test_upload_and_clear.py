"""E2E tests for the upload and clear-database API endpoints.

Uses the Docker PostgreSQL container defined in docker-compose.yml.
Tests are automatically skipped when the database is not reachable.

Each test runs inside a transaction that is rolled back after the test.
"""

from __future__ import annotations

import io
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, text
from sqlalchemy.orm import Session

from echelonos.config import settings
from echelonos.db.models import Base, Document, Obligation, Organization
from echelonos.db.persist import get_or_create_organization, upsert_document, upsert_obligation
from echelonos.api.app import app, get_db

# ---------------------------------------------------------------------------
# PostgreSQL connectivity check
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
def db_session(pg_engine):
    connection = pg_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture()
def client(db_session: Session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_pdf() -> bytes:
    """Create a minimal valid PDF file (1 page, contains text)."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length 44>>stream\n"
        b"BT /F1 12 Tf 100 700 Td (Hello World) Tj ET\n"
        b"endstream\nendobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000266 00000 n \n"
        b"0000000360 00000 n \n"
        b"trailer<</Size 6/Root 1 0 R>>\n"
        b"startxref\n431\n%%EOF"
    )


# ---------------------------------------------------------------------------
# Tests — POST /api/upload
# ---------------------------------------------------------------------------


class TestUploadEndpoint:
    def test_upload_single_pdf(self, client: TestClient):
        pdf_bytes = _make_minimal_pdf()
        resp = client.post(
            "/api/upload",
            files=[("files", ("TestCorp_MSA_2024.pdf", io.BytesIO(pdf_bytes), "application/pdf"))],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["total_uploaded"] == 1
        assert "elapsed_seconds" in data
        assert data["elapsed_seconds"] >= 0

    def test_upload_multiple_files(self, client: TestClient):
        pdf_bytes = _make_minimal_pdf()
        files = [
            ("files", ("Acme_MSA_2024.pdf", io.BytesIO(pdf_bytes), "application/pdf")),
            ("files", ("Acme_Amendment1.pdf", io.BytesIO(pdf_bytes), "application/pdf")),
        ]
        resp = client.post("/api/upload", files=files)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["total_uploaded"] == 2

    def test_upload_returns_elapsed_time(self, client: TestClient):
        pdf_bytes = _make_minimal_pdf()
        resp = client.post(
            "/api/upload",
            files=[("files", ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf"))],
        )
        data = resp.json()
        assert isinstance(data["elapsed_seconds"], (int, float))


# ---------------------------------------------------------------------------
# Tests — GET /api/pipeline/status
# ---------------------------------------------------------------------------


class TestPipelineStatus:
    def test_status_endpoint_returns_state(self, client: TestClient):
        resp = client.get("/api/pipeline/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "state" in data
        assert data["state"] in ("idle", "processing", "done", "error", "cancelled")


# ---------------------------------------------------------------------------
# Tests — DELETE /api/database
# ---------------------------------------------------------------------------


class TestClearDatabase:
    def test_clear_empty_db(self, client: TestClient):
        resp = client.delete("/api/database")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "deleted" in data

    def test_clear_removes_all_records(self, client: TestClient, db_session: Session):
        # Seed some data
        org = get_or_create_organization(db_session, name="ClearTest Corp")
        upsert_document(
            db_session,
            org_id=org.id,
            file_path="/tmp/cleartest/doc.pdf",
            filename="doc.pdf",
        )
        upsert_obligation(
            db_session,
            doc_id=upsert_document(
                db_session,
                org_id=org.id,
                file_path="/tmp/cleartest/doc2.pdf",
                filename="doc2.pdf",
            ).id,
            source_clause="Section 1",
            obligation_text="Must pay on time.",
        )
        db_session.flush()

        # Verify data exists
        assert db_session.query(func.count(Organization.id)).scalar() >= 1
        assert db_session.query(func.count(Document.id)).scalar() >= 1

        # Clear
        resp = client.delete("/api/database")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["deleted"]["organizations"] >= 1

    def test_clear_then_orgs_endpoint_returns_empty(self, client: TestClient, db_session: Session):
        # Seed
        get_or_create_organization(db_session, name="WipeMe Corp")
        db_session.flush()

        # Clear
        client.delete("/api/database")

        # Verify
        resp = client.get("/api/organizations")
        assert resp.status_code == 200
        orgs = resp.json()
        # After clear, no orgs should remain in this transaction
        assert not any(o["name"] == "WipeMe Corp" for o in orgs)
