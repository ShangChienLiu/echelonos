"""E2E tests for POST /api/pipeline/run and POST /api/pipeline/stop.

Uses the Docker PostgreSQL container defined in docker-compose.yml.
Tests are automatically skipped when the database is not reachable.

Each test runs inside a transaction that is rolled back after the test.
Pipeline stage functions are mocked so the tests run fast and don't
require real LLM / OCR credentials.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from echelonos.api import app as app_module
from echelonos.api.app import app, _pipeline_status, _reset_pipeline_status
from echelonos.config import settings
from echelonos.db.models import Base
from echelonos.db.persist import get_or_create_organization, upsert_document
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
def client(db_session: Session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_pipeline_state(db_session: Session):
    """Reset pipeline state and inject test session factory."""
    _reset_pipeline_status()
    app_module._pipeline_thread = None
    app_module._cancel_event.clear()
    app_module._test_session_factory = lambda: db_session
    yield
    _reset_pipeline_status()
    app_module._pipeline_thread = None
    app_module._cancel_event.clear()
    app_module._test_session_factory = None


def _seed_org_with_docs(db_session: Session, org_name: str = "TestCorp", n_docs: int = 2):
    """Seed an org with n dummy documents and return (org, docs)."""
    org = get_or_create_organization(db_session, name=org_name, folder_path="/tmp/test")
    docs = []
    for i in range(n_docs):
        doc = upsert_document(
            db_session,
            org_id=org.id,
            file_path=f"/tmp/test/doc_{i}.pdf",
            filename=f"doc_{i}.pdf",
            status="VALID",
        )
        docs.append(doc)
    db_session.flush()
    return org, docs


# Patches target the *source* modules so inline imports inside
# _run_pipeline_background pick up the mocks.
_STAGE_PATCHES = {
    "mistral": "echelonos.ocr.mistral_client.get_mistral_client",
    "anthropic": "echelonos.llm.claude_client.get_anthropic_client",
    "ocr": "echelonos.stages.stage_1_ocr.ingest_document",
    "classify": "echelonos.stages.stage_2_classification.classify_document",
    "cross_check": "echelonos.stages.stage_2_classification.classify_with_cross_check",
    "extract": "echelonos.stages.stage_3_extraction.extract_and_verify",
    "link": "echelonos.stages.stage_4_linking.link_documents",
    "resolve": "echelonos.stages.stage_5_amendment.resolve_all",
    "evidence": "echelonos.stages.stage_6_evidence.package_evidence",
}


# ---------------------------------------------------------------------------
# Tests — POST /api/pipeline/run — error cases
# ---------------------------------------------------------------------------


class TestRunPipelineErrors:
    def test_run_unknown_org_returns_error(self, client: TestClient):
        resp = client.post("/api/pipeline/run?org_name=NonExistentCorp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "not found" in data["error"]

    def test_run_org_with_no_documents(self, client: TestClient, db_session: Session):
        get_or_create_organization(db_session, name="EmptyCorp")
        db_session.flush()

        resp = client.post("/api/pipeline/run?org_name=EmptyCorp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "No documents" in data["error"]


# ---------------------------------------------------------------------------
# Tests — POST /api/pipeline/stop — error cases
# ---------------------------------------------------------------------------


class TestStopPipelineErrors:
    def test_stop_when_not_running_returns_error(self, client: TestClient):
        resp = client.post("/api/pipeline/stop")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "No pipeline" in data["error"]


# ---------------------------------------------------------------------------
# Tests — Status transitions: idle → processing → done
# ---------------------------------------------------------------------------


class TestPipelineStatusTransitions:
    def test_idle_to_processing_to_done(self, client: TestClient, db_session: Session):
        """Pipeline starts idle, transitions to processing, then done."""
        _seed_org_with_docs(db_session, "TransitionCorp", n_docs=1)

        # Verify starts idle.
        resp = client.get("/api/pipeline/status")
        assert resp.json()["state"] == "idle"

        # Mock all stage functions to be fast no-ops.
        with (
            patch(_STAGE_PATCHES["mistral"], return_value=MagicMock()),
            patch(_STAGE_PATCHES["anthropic"], return_value=MagicMock()),
            patch(_STAGE_PATCHES["ocr"], return_value={"doc_id": "x", "pages": [], "total_pages": 0, "flags": []}),
            patch(_STAGE_PATCHES["classify"], return_value=MagicMock(
                doc_type="MSA", parties=[], effective_date=None,
                parent_reference_raw=None, confidence=0.95,
            )),
            patch(_STAGE_PATCHES["cross_check"], side_effect=lambda text, result: result),
            patch(_STAGE_PATCHES["extract"], return_value=[]),
            patch(_STAGE_PATCHES["link"], return_value=[]),
            patch(_STAGE_PATCHES["resolve"], return_value=[]),
            patch(_STAGE_PATCHES["evidence"], return_value=[]),
        ):
            # Start pipeline.
            resp = client.post("/api/pipeline/run?org_name=TransitionCorp")
            assert resp.json()["status"] == "ok"

            # Wait for completion (with timeout).
            deadline = time.time() + 10
            final_state = None
            while time.time() < deadline:
                resp = client.get("/api/pipeline/status")
                data = resp.json()
                final_state = data["state"]
                if final_state in ("done", "error"):
                    break
                time.sleep(0.2)

            assert final_state == "done", f"Expected done, got {final_state}: {data}"
            assert "stages_completed" in data
            assert len(data["stages_completed"]) > 0
            assert data["elapsed_seconds"] is not None

    def test_concurrent_run_rejected(self, client: TestClient, db_session: Session):
        """A second run while one is processing should be rejected."""
        _seed_org_with_docs(db_session, "ConcurrentCorp", n_docs=1)

        # Simulate a running thread.
        barrier = threading.Event()

        def _fake_runner(*args):
            barrier.wait(timeout=5)

        app_module._pipeline_thread = threading.Thread(target=_fake_runner, daemon=True)
        app_module._pipeline_thread.start()

        try:
            resp = client.post("/api/pipeline/run?org_name=ConcurrentCorp")
            data = resp.json()
            assert data["status"] == "error"
            assert "already running" in data["error"]
        finally:
            barrier.set()
            app_module._pipeline_thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Tests — Cancellation: idle → processing → cancelled
# ---------------------------------------------------------------------------


class TestPipelineCancellation:
    def test_idle_to_processing_to_cancelled(self, client: TestClient, db_session: Session):
        """Pipeline can be stopped mid-run and transitions to cancelled."""
        _seed_org_with_docs(db_session, "CancelCorp", n_docs=3)

        # Use a barrier so OCR blocks until we signal cancellation.
        ocr_entered = threading.Event()
        ocr_release = threading.Event()

        def _slow_ocr(file_path, doc_id, ocr_client=None):
            ocr_entered.set()
            ocr_release.wait(timeout=5)
            return {"doc_id": doc_id, "pages": [], "total_pages": 0, "flags": []}

        with (
            patch(_STAGE_PATCHES["mistral"], return_value=MagicMock()),
            patch(_STAGE_PATCHES["anthropic"], return_value=MagicMock()),
            patch(_STAGE_PATCHES["ocr"], side_effect=_slow_ocr),
            patch(_STAGE_PATCHES["classify"]),
            patch(_STAGE_PATCHES["cross_check"]),
            patch(_STAGE_PATCHES["extract"], return_value=[]),
            patch(_STAGE_PATCHES["link"], return_value=[]),
            patch(_STAGE_PATCHES["resolve"], return_value=[]),
            patch(_STAGE_PATCHES["evidence"], return_value=[]),
        ):
            # Start pipeline.
            resp = client.post("/api/pipeline/run?org_name=CancelCorp")
            assert resp.json()["status"] == "ok"

            # Wait until OCR is entered (pipeline is running).
            assert ocr_entered.wait(timeout=5), "Pipeline did not start OCR"

            # Verify it's processing.
            resp = client.get("/api/pipeline/status")
            assert resp.json()["state"] == "processing"

            # Stop pipeline.
            resp = client.post("/api/pipeline/stop")
            assert resp.json()["status"] == "ok"

            # Release the blocked OCR call.
            ocr_release.set()

            # Wait for the thread to finish.
            deadline = time.time() + 5
            final_state = None
            while time.time() < deadline:
                resp = client.get("/api/pipeline/status")
                data = resp.json()
                final_state = data["state"]
                if final_state == "cancelled":
                    break
                time.sleep(0.2)

            assert final_state == "cancelled", f"Expected cancelled, got {final_state}"
