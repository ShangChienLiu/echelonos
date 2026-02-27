"""FastAPI application for the Echelonos obligation report API.

Serves obligation report data from the PostgreSQL database, with a
fallback to demo data when the requested organization is not found.

Run standalone with::

    uvicorn echelonos.api.app:app --reload
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from typing import Any

from fastapi import Depends, FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session

from echelonos.api.demo_data import DEMO_DOCUMENTS, DEMO_LINKS, DEMO_OBLIGATIONS
from echelonos.db.models import (
    Base,
    DanglingReference,
    Document,
    DocumentLink,
    Evidence,
    Fingerprint,
    Flag,
    Obligation,
    Organization,
    Page,
)
from echelonos.db.session import get_db
from echelonos.stages.stage_7_report import (
    FlagItem,
    ObligationReport,
    ObligationRow,
    generate_report,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Echelonos API",
    description="Contract obligation extraction and report API",
    version="0.1.0",
)

# CORS -- allow the React dev server and common local origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_demo_report(org_name: str) -> ObligationReport:
    """Generate a demo report for the given org name."""
    return generate_report(
        org_name=org_name,
        obligations=DEMO_OBLIGATIONS,
        documents=DEMO_DOCUMENTS,
        links=DEMO_LINKS,
    )


def _get_real_report(org_name: str, db: Session) -> ObligationReport | None:
    """Query the database and build a report for *org_name*.

    Returns ``None`` if the organization is not found, which signals the
    caller to fall back to demo data.
    """
    org = (
        db.query(Organization)
        .filter(func.lower(Organization.name) == org_name.lower())
        .first()
    )
    if org is None:
        return None

    docs = db.query(Document).filter(Document.org_id == org.id).all()
    if not docs:
        return None

    doc_ids = [doc.id for doc in docs]

    obligations_orm = (
        db.query(Obligation).filter(Obligation.doc_id.in_(doc_ids)).all()
    )
    links_orm = (
        db.query(DocumentLink).filter(DocumentLink.child_doc_id.in_(doc_ids)).all()
    )

    # Convert ORM objects to dicts matching generate_report() signatures.
    documents: dict[str, dict[str, Any]] = {}
    for doc in docs:
        documents[str(doc.id)] = {
            "id": str(doc.id),
            "doc_type": doc.doc_type,
            "filename": doc.filename,
            "org_name": org.name,
            "upload_date": str(doc.created_at),
            "classification_confidence": doc.classification_confidence or 0.0,
        }

    obligations: list[dict[str, Any]] = []
    for obl in obligations_orm:
        obligations.append({
            "id": str(obl.id),
            "doc_id": str(obl.doc_id),
            "obligation_text": obl.obligation_text,
            "obligation_type": obl.obligation_type,
            "responsible_party": obl.responsible_party,
            "counterparty": obl.counterparty,
            "source_clause": obl.source_clause,
            "status": obl.status,
            "frequency": obl.frequency,
            "deadline": obl.deadline,
            "confidence": obl.confidence or 0.0,
            "verification_result": obl.verification_result,
        })

    links: list[dict[str, Any]] = []
    for link in links_orm:
        links.append({
            "child_doc_id": str(link.child_doc_id),
            "parent_doc_id": str(link.parent_doc_id) if link.parent_doc_id else None,
            "status": link.link_status,
            "confidence": (link.candidates or {}).get("confidence", 0.0),
        })

    return generate_report(
        org_name=org_name,
        obligations=obligations,
        documents=documents,
        links=links,
    )


def _get_report(org_name: str, db: Session) -> ObligationReport:
    """Try real DB first, fall back to demo data."""
    try:
        report = _get_real_report(org_name, db)
        if report is not None:
            return report
    except Exception:
        logger.exception("Failed to query DB for org %s, falling back to demo data", org_name)
    return _get_demo_report(org_name)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health_check() -> dict:
    """Health check endpoint."""
    return {"status": "ok", "service": "echelonos-api"}


@app.get("/api/organizations")
def list_organizations(db: Session = Depends(get_db)) -> list[dict]:
    """List all organizations available in the database."""
    orgs = db.query(Organization).order_by(Organization.name).all()
    return [
        {"id": str(org.id), "name": org.name}
        for org in orgs
    ]


@app.get("/api/report/{org_name}", response_model=ObligationReport)
def get_report(org_name: str, db: Session = Depends(get_db)) -> ObligationReport:
    """Return the full obligation report for an organization."""
    return _get_report(org_name, db)


@app.get("/api/report/{org_name}/obligations", response_model=list[ObligationRow])
def get_obligations(org_name: str, db: Session = Depends(get_db)) -> list[ObligationRow]:
    """Return just the obligation matrix rows for an organization."""
    report = _get_report(org_name, db)
    return report.obligations


@app.get("/api/report/{org_name}/flags", response_model=list[FlagItem])
def get_flags(org_name: str, db: Session = Depends(get_db)) -> list[FlagItem]:
    """Return just the flag report for an organization."""
    report = _get_report(org_name, db)
    return report.flags


@app.get("/api/report/{org_name}/summary")
def get_summary(org_name: str, db: Session = Depends(get_db)) -> dict:
    """Return just the summary statistics for an organization."""
    report = _get_report(org_name, db)
    return report.summary


# ---------------------------------------------------------------------------
# Upload & pipeline
# ---------------------------------------------------------------------------

# Module-level pipeline status store (simple in-process tracking).
_pipeline_status: dict[str, Any] = {
    "state": "idle",  # idle | processing | done | error
    "org_name": None,
    "total_files": 0,
    "processed_files": 0,
    "started_at": None,
    "finished_at": None,
    "elapsed_seconds": None,
    "error": None,
}


def _reset_pipeline_status() -> None:
    _pipeline_status.update(
        state="idle",
        org_name=None,
        total_files=0,
        processed_files=0,
        started_at=None,
        finished_at=None,
        elapsed_seconds=None,
        error=None,
    )


@app.get("/api/pipeline/status")
def pipeline_status() -> dict:
    """Return the current pipeline processing status."""
    return dict(_pipeline_status)


@app.post("/api/upload")
async def upload_documents(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
) -> dict:
    """Upload documents (or a single zip), persist them, and run the pipeline.

    If a single .zip file is uploaded, it is extracted and the zip filename
    (without extension) is used as the organization name.  Otherwise the org
    name is derived from the common filename prefix.
    """
    import zipfile
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from echelonos.db.persist import get_or_create_organization, upsert_document

    start = time.time()

    # Create temp directory.
    tmp_dir = tempfile.mkdtemp(prefix="echelonos_upload_")

    try:
        # ------------------------------------------------------------------
        # Handle zip upload: extract into org_dir
        # ------------------------------------------------------------------
        single_zip = (
            len(files) == 1
            and (files[0].filename or "").lower().endswith(".zip")
        )

        if single_zip:
            zip_upload = files[0]
            org_name = os.path.splitext(zip_upload.filename or "upload")[0]
            org_dir = os.path.join(tmp_dir, org_name)
            os.makedirs(org_dir, exist_ok=True)

            # Save the zip to disk first.
            zip_path = os.path.join(tmp_dir, zip_upload.filename or "upload.zip")
            with open(zip_path, "wb") as out:
                shutil.copyfileobj(zip_upload.file, out)

            # Extract.
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(org_dir)

            total_uploaded = len(
                [n for n in os.listdir(org_dir) if os.path.isfile(os.path.join(org_dir, n))]
            )

        else:
            # ------------------------------------------------------------------
            # Multiple files: save in parallel
            # ------------------------------------------------------------------
            org_name = _derive_org_name([f.filename or "unknown" for f in files])
            org_dir = os.path.join(tmp_dir, org_name)
            os.makedirs(org_dir, exist_ok=True)
            total_uploaded = len(files)

            def _save_file(upload: UploadFile) -> str:
                fname = upload.filename or f"file_{id(upload)}"
                dest = os.path.join(org_dir, fname)
                with open(dest, "wb") as out:
                    shutil.copyfileobj(upload.file, out)
                return dest

            with ThreadPoolExecutor(max_workers=min(8, len(files))) as pool:
                futures = {pool.submit(_save_file, f): f for f in files}
                for future in as_completed(futures):
                    future.result()  # raise on error

        _pipeline_status.update(
            state="processing",
            org_name=org_name,
            total_files=total_uploaded,
            processed_files=0,
            started_at=time.time(),
            finished_at=None,
            elapsed_seconds=None,
            error=None,
        )

        # ------------------------------------------------------------------
        # Run pipeline stages 0a + 0b
        # ------------------------------------------------------------------
        from echelonos.stages.stage_0a_validation import validate_folder
        from echelonos.stages.stage_0b_dedup import deduplicate_files

        validated = validate_folder(org_dir)
        valid_files = [f for f in validated if f["status"] == "VALID"]
        unique_files = deduplicate_files(valid_files)

        # Persist to DB.
        org = get_or_create_organization(db, name=org_name, folder_path=org_dir)
        doc_ids: list[str] = []
        for f in unique_files:
            doc = upsert_document(
                db,
                org_id=org.id,
                file_path=f["file_path"],
                filename=os.path.basename(f["file_path"]),
                status=f.get("status", "VALID"),
            )
            doc_ids.append(str(doc.id))
            _pipeline_status["processed_files"] = len(doc_ids)

        db.commit()

        elapsed = time.time() - start
        _pipeline_status.update(
            state="done",
            processed_files=len(doc_ids),
            finished_at=time.time(),
            elapsed_seconds=round(elapsed, 2),
        )

        return {
            "status": "ok",
            "org_name": org_name,
            "org_id": str(org.id),
            "total_uploaded": total_uploaded,
            "valid_files": len(valid_files),
            "unique_files": len(unique_files),
            "documents_persisted": len(doc_ids),
            "elapsed_seconds": round(elapsed, 2),
        }

    except Exception as exc:
        db.rollback()
        elapsed = time.time() - start
        _pipeline_status.update(
            state="error",
            finished_at=time.time(),
            elapsed_seconds=round(elapsed, 2),
            error=str(exc),
        )
        logger.exception("Pipeline failed for %s", org_name if "org_name" in dir() else "unknown")
        return {
            "status": "error",
            "error": str(exc),
            "elapsed_seconds": round(elapsed, 2),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _derive_org_name(filenames: list[str]) -> str:
    """Derive an organization name from uploaded filenames.

    Uses the longest common prefix of filenames (stripped of separators),
    falling back to a timestamp.
    """
    if not filenames:
        return f"upload-{int(time.time())}"

    basenames = [os.path.splitext(os.path.basename(f))[0] for f in filenames]
    if len(basenames) == 1:
        # Single file — use its name without extension.
        return basenames[0].rstrip("_- ")

    prefix = os.path.commonprefix(basenames).rstrip("_- .")
    if len(prefix) >= 3:
        return prefix
    return f"upload-{int(time.time())}"


# ---------------------------------------------------------------------------
# Database management
# ---------------------------------------------------------------------------


@app.delete("/api/database")
def clear_database(db: Session = Depends(get_db)) -> dict:
    """Delete ALL data from every table. Use with extreme caution.

    Deletes in dependency order to avoid FK constraint violations.
    """
    counts: dict[str, int] = {}
    # Order matters: children before parents.
    tables = [
        ("dangling_references", DanglingReference),
        ("flags", Flag),
        ("evidence", Evidence),
        ("obligations", Obligation),
        ("pages", Page),
        ("fingerprints", Fingerprint),
        ("document_links", DocumentLink),
        ("documents", Document),
        ("organizations", Organization),
    ]
    for name, model in tables:
        n = db.query(model).delete()
        counts[name] = n

    db.commit()
    _reset_pipeline_status()
    logger.info("Database cleared: %s", counts)
    return {"status": "ok", "deleted": counts}
