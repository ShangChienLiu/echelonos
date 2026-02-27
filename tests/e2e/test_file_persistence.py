"""E2E tests verifying uploaded files persist on disk after upload completes.

The bug: upload saves files to a temp dir, persists DB records with those temp
paths, then deletes the temp dir in its ``finally`` block.  When the pipeline
later tries to OCR those files, every read fails (0 pages, 0 obligations).

These tests verify that:
1. Document.file_path points to an *existing* file after upload
2. Paths are under the configured ``upload_dir``, not a temp directory
3. Organization.folder_path is persistent (not a temp dir)
"""

from __future__ import annotations

import io
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from echelonos.config import settings
from echelonos.db.models import Base, Document, Organization
from echelonos.api.app import app
from echelonos.db.session import get_db

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
def test_upload_dir(tmp_path):
    """Provide a test-specific upload directory and patch settings."""
    upload_dir = str(tmp_path / "uploads")
    original = settings.upload_dir
    settings.upload_dir = upload_dir
    yield upload_dir
    settings.upload_dir = original


@pytest.fixture()
def client(db_session: Session, test_upload_dir: str):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_pdf(label: str = "Hello World") -> bytes:
    """Create a minimal valid PDF file with a unique text label."""
    content = f"BT /F1 12 Tf 100 700 Td ({label}) Tj ET"
    length = len(content)
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        + f"4 0 obj<</Length {length}>>stream\n{content}\nendstream\nendobj\n".encode()
        + b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
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


def _get_docs_for_org(db: Session, org_name: str) -> list[Document]:
    """Query documents belonging to a specific org (avoids pre-existing data)."""
    org = db.query(Organization).filter(Organization.name == org_name).first()
    if org is None:
        return []
    return db.query(Document).filter(Document.org_id == org.id).all()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFilePersistence:
    """Verify uploaded files survive the upload endpoint's temp cleanup."""

    def test_document_file_path_exists_after_upload(
        self, client: TestClient, db_session: Session
    ):
        """After upload, every Document.file_path must point to a real file."""
        pdf_bytes = _make_minimal_pdf()
        resp = client.post(
            "/api/upload",
            files=[("files", ("PersistTest_Doc.pdf", io.BytesIO(pdf_bytes), "application/pdf"))],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        org_name = data["org_name"]

        docs = _get_docs_for_org(db_session, org_name)
        assert len(docs) >= 1

        for doc in docs:
            assert os.path.isfile(doc.file_path), (
                f"Document file_path does not exist on disk: {doc.file_path}"
            )

    def test_document_file_path_under_upload_dir(
        self, client: TestClient, db_session: Session, test_upload_dir: str
    ):
        """Document paths must be under the persistent upload_dir, not /tmp."""
        pdf_bytes = _make_minimal_pdf()
        resp = client.post(
            "/api/upload",
            files=[("files", ("PathTest_Doc.pdf", io.BytesIO(pdf_bytes), "application/pdf"))],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        org_name = data["org_name"]

        docs = _get_docs_for_org(db_session, org_name)
        assert len(docs) >= 1

        for doc in docs:
            assert doc.file_path.startswith(test_upload_dir), (
                f"Document file_path is not under upload_dir.\n"
                f"  file_path:  {doc.file_path}\n"
                f"  upload_dir: {test_upload_dir}"
            )
            # Must NOT be under the system temp directory
            assert not doc.file_path.startswith(tempfile.gettempdir()), (
                f"Document file_path is under temp dir: {doc.file_path}"
            )

    def test_organization_folder_path_is_persistent(
        self, client: TestClient, db_session: Session, test_upload_dir: str
    ):
        """Organization.folder_path must be persistent, not a temp path."""
        pdf_bytes = _make_minimal_pdf()
        resp = client.post(
            "/api/upload",
            files=[("files", ("OrgTest_Doc.pdf", io.BytesIO(pdf_bytes), "application/pdf"))],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        org_name = data["org_name"]

        org = (
            db_session.query(Organization)
            .filter(Organization.name == org_name)
            .first()
        )
        assert org is not None
        assert org.folder_path is not None
        assert org.folder_path.startswith(test_upload_dir), (
            f"Organization folder_path is not under upload_dir.\n"
            f"  folder_path: {org.folder_path}\n"
            f"  upload_dir:  {test_upload_dir}"
        )

    def test_zip_upload_files_persist(
        self, client: TestClient, db_session: Session, test_upload_dir: str
    ):
        """Files from a zip upload must also be persisted to upload_dir."""
        import zipfile as _zf

        # Use different content so dedup doesn't collapse them.
        pdf_a = _make_minimal_pdf("Contract Alpha")
        pdf_b = _make_minimal_pdf("Contract Beta")
        buf = io.BytesIO()
        with _zf.ZipFile(buf, "w") as zf:
            zf.writestr("contract_a.pdf", pdf_a)
            zf.writestr("contract_b.pdf", pdf_b)
        buf.seek(0)

        resp = client.post(
            "/api/upload",
            files=[("files", ("ZipOrg.zip", buf, "application/zip"))],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        org_name = data["org_name"]

        docs = _get_docs_for_org(db_session, org_name)
        assert len(docs) >= 2

        for doc in docs:
            assert os.path.isfile(doc.file_path), (
                f"Zip-extracted file does not exist: {doc.file_path}"
            )
            assert doc.file_path.startswith(test_upload_dir), (
                f"Zip-extracted file not under upload_dir: {doc.file_path}"
            )
