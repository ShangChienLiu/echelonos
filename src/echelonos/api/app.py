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
import threading
import time
from typing import Any

from fastapi import Depends, FastAPI, Query, UploadFile, File
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
    "state": "idle",  # idle | processing | done | error | cancelled
    "org_name": None,
    "org_id": None,
    "current_stage": None,
    "current_stage_label": None,
    "total_files": 0,
    "processed_files": 0,
    "total_docs": 0,
    "processed_docs": 0,
    "stages_completed": [],
    "started_at": None,
    "finished_at": None,
    "elapsed_seconds": None,
    "error": None,
}

# Background pipeline thread state.
_pipeline_thread: threading.Thread | None = None
_cancel_event = threading.Event()
_test_session_factory: Any = None  # Override in tests to inject DB session


def _reset_pipeline_status() -> None:
    _pipeline_status.update(
        state="idle",
        org_name=None,
        org_id=None,
        current_stage=None,
        current_stage_label=None,
        total_files=0,
        processed_files=0,
        total_docs=0,
        processed_docs=0,
        stages_completed=[],
        started_at=None,
        finished_at=None,
        elapsed_seconds=None,
        error=None,
    )


def _set_stage(stage_id: str, label: str) -> None:
    """Update status to reflect the current stage."""
    _pipeline_status["current_stage"] = stage_id
    _pipeline_status["current_stage_label"] = label


def _complete_stage(label: str) -> None:
    """Mark a stage as completed."""
    _pipeline_status["stages_completed"] = [
        *_pipeline_status["stages_completed"],
        label,
    ]


@app.get("/api/pipeline/status")
def pipeline_status() -> dict:
    """Return the current pipeline processing status."""
    result = dict(_pipeline_status)
    # Compute live elapsed_seconds when processing.
    if result["state"] == "processing" and result["started_at"] is not None:
        result["elapsed_seconds"] = round(time.time() - result["started_at"], 2)
    return result


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

            from echelonos.stages.stage_0a_validation import _is_macos_junk

            total_uploaded = sum(
                1
                for dirpath, _, fnames in os.walk(org_dir)
                for f in fnames
                if not _is_macos_junk(os.path.join(dirpath, f))
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


# ---------------------------------------------------------------------------
# Pipeline run / stop
# ---------------------------------------------------------------------------


@app.post("/api/pipeline/run")
def run_pipeline(
    org_name: str = Query(..., description="Organization name to process"),
    db: Session = Depends(get_db),
) -> dict:
    """Launch stages 1-7 in a background thread for the given organization."""
    global _pipeline_thread

    # Guard: is a pipeline already running?
    if (
        _pipeline_thread is not None
        and _pipeline_thread.is_alive()
    ):
        return {"status": "error", "error": "Pipeline is already running"}

    # Validate org exists.
    org = (
        db.query(Organization)
        .filter(func.lower(Organization.name) == org_name.lower())
        .first()
    )
    if org is None:
        return {"status": "error", "error": f"Organization '{org_name}' not found"}

    # Validate org has documents.
    doc_count = db.query(Document).filter(Document.org_id == org.id).count()
    if doc_count == 0:
        return {"status": "error", "error": f"No documents found for '{org_name}'"}

    # Reset cancellation flag.
    _cancel_event.clear()

    # Initialize status.
    _pipeline_status.update(
        state="processing",
        org_name=org_name,
        org_id=str(org.id),
        current_stage=None,
        current_stage_label="Initializing...",
        total_files=0,
        processed_files=0,
        total_docs=doc_count,
        processed_docs=0,
        stages_completed=[],
        started_at=time.time(),
        finished_at=None,
        elapsed_seconds=None,
        error=None,
    )

    # Launch background thread.
    _pipeline_thread = threading.Thread(
        target=_run_pipeline_background,
        args=(org_name, str(org.id)),
        daemon=True,
    )
    _pipeline_thread.start()

    return {"status": "ok", "message": f"Pipeline started for '{org_name}'"}


@app.post("/api/pipeline/stop")
def stop_pipeline() -> dict:
    """Request cancellation of the running pipeline."""
    if (
        _pipeline_thread is None
        or not _pipeline_thread.is_alive()
    ):
        return {"status": "error", "error": "No pipeline is currently running"}

    _cancel_event.set()
    _pipeline_status.update(
        state="cancelled",
        current_stage_label="Cancelling...",
    )
    return {"status": "ok", "message": "Pipeline cancellation requested"}


def _run_pipeline_background(org_name: str, org_id: str) -> None:
    """Execute stages 1-7 in a background thread.

    Creates its own DB session (thread-safe) and LLM clients.
    Checks ``_cancel_event`` between stages and between documents.
    """
    import uuid as _uuid

    from echelonos.db.persist import (
        upsert_document,
        upsert_document_link,
        upsert_obligation,
        upsert_page,
    )

    if _test_session_factory is not None:
        db = _test_session_factory()
    else:
        from echelonos.db.session import SessionLocal
        db = SessionLocal()

    try:
        # Load clients once.
        from echelonos.llm.claude_client import get_anthropic_client
        from echelonos.ocr.mistral_client import get_mistral_client

        ocr_client = get_mistral_client()
        claude_client = get_anthropic_client()

        # Load documents for this org.
        org_uuid = _uuid.UUID(org_id)
        docs = db.query(Document).filter(Document.org_id == org_uuid).all()
        total = len(docs)
        _pipeline_status["total_docs"] = total
        _pipeline_status["processed_docs"] = 0

        # ---- Stage 1: OCR ----
        if _cancel_event.is_set():
            return
        _set_stage("stage_1", "Stage 1: OCR — Extracting text")

        from echelonos.stages.stage_1_ocr import ingest_document

        for i, doc in enumerate(docs):
            if _cancel_event.is_set():
                return
            try:
                ocr_result = ingest_document(
                    file_path=doc.file_path,
                    doc_id=str(doc.id),
                    ocr_client=ocr_client,
                )
                for page_data in ocr_result.get("pages", []):
                    upsert_page(
                        db,
                        doc_id=doc.id,
                        page_number=page_data["page_number"],
                        text=page_data.get("text"),
                        tables_markdown=page_data.get("tables_markdown"),
                        ocr_confidence=page_data.get("ocr_confidence"),
                    )
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("OCR failed for doc %s", doc.id)
            _pipeline_status["processed_docs"] = i + 1

        _complete_stage("OCR")

        # ---- Stage 2: Classification ----
        if _cancel_event.is_set():
            return
        _set_stage("stage_2", "Stage 2: Classification")
        _pipeline_status["processed_docs"] = 0

        from echelonos.stages.stage_2_classification import (
            classify_document,
            classify_with_cross_check,
        )

        for i, doc in enumerate(docs):
            if _cancel_event.is_set():
                return
            try:
                # Gather text from pages.
                pages = (
                    db.query(Page)
                    .filter(Page.doc_id == doc.id)
                    .order_by(Page.page_number)
                    .all()
                )
                full_text = "\n\n".join(p.text or "" for p in pages)
                if not full_text.strip():
                    _pipeline_status["processed_docs"] = i + 1
                    continue

                result = classify_document(full_text, claude_client=claude_client)
                result = classify_with_cross_check(full_text, result)

                # Update document fields.
                doc.doc_type = result.doc_type
                doc.parties = result.parties
                doc.effective_date = result.effective_date
                doc.parent_reference_raw = result.parent_reference_raw
                doc.classification_confidence = result.confidence
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Classification failed for doc %s", doc.id)
            _pipeline_status["processed_docs"] = i + 1

        _complete_stage("Classification")

        # ---- Stage 3: Extraction ----
        if _cancel_event.is_set():
            return
        _set_stage("stage_3", "Stage 3: Obligation Extraction")
        _pipeline_status["processed_docs"] = 0

        from echelonos.stages.stage_3_extraction import extract_and_verify

        for i, doc in enumerate(docs):
            if _cancel_event.is_set():
                return
            try:
                pages = (
                    db.query(Page)
                    .filter(Page.doc_id == doc.id)
                    .order_by(Page.page_number)
                    .all()
                )
                full_text = "\n\n".join(p.text or "" for p in pages)
                if not full_text.strip():
                    _pipeline_status["processed_docs"] = i + 1
                    continue

                verified = extract_and_verify(full_text, claude_client=claude_client)
                for item in verified:
                    obl_data = item.get("obligation", {})
                    upsert_obligation(
                        db,
                        doc_id=doc.id,
                        source_clause=obl_data.get("source_clause", ""),
                        obligation_text=obl_data.get("obligation_text", ""),
                        obligation_type=obl_data.get("obligation_type"),
                        responsible_party=obl_data.get("responsible_party"),
                        counterparty=obl_data.get("counterparty"),
                        frequency=obl_data.get("frequency"),
                        deadline=obl_data.get("deadline"),
                        source_page=obl_data.get("source_page"),
                        confidence=item.get("claude_verification", {}).get(
                            "confidence", obl_data.get("confidence")
                        ),
                        status=item.get("status", "ACTIVE"),
                        verification_result=item.get("claude_verification"),
                    )
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Extraction failed for doc %s", doc.id)
            _pipeline_status["processed_docs"] = i + 1

        _complete_stage("Extraction")

        # ---- Stage 4: Linking ----
        if _cancel_event.is_set():
            return
        _set_stage("stage_4", "Stage 4: Document Linking")

        from echelonos.stages.stage_4_linking import link_documents

        try:
            doc_dicts = []
            for doc in docs:
                doc_dicts.append({
                    "id": str(doc.id),
                    "doc_type": doc.doc_type,
                    "filename": doc.filename,
                    "parties": doc.parties,
                    "effective_date": str(doc.effective_date) if doc.effective_date else None,
                    "parent_reference_raw": doc.parent_reference_raw,
                })

            link_results = link_documents(doc_dicts)
            for lr in link_results:
                child_id = _uuid.UUID(lr["child_doc_id"])
                parent_id = (
                    _uuid.UUID(lr["parent_doc_id"])
                    if lr.get("parent_doc_id")
                    else None
                )
                upsert_document_link(
                    db,
                    child_doc_id=child_id,
                    parent_doc_id=parent_id,
                    link_status=lr.get("status", "UNLINKED"),
                    candidates=lr.get("candidates"),
                )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Linking failed for org %s", org_name)

        _complete_stage("Linking")

        # ---- Stage 5: Amendment Resolution ----
        if _cancel_event.is_set():
            return
        _set_stage("stage_5", "Stage 5: Amendment Resolution")

        from echelonos.stages.stage_5_amendment import resolve_all

        try:
            # Re-read docs and links from DB for fresh state.
            docs_refreshed = db.query(Document).filter(Document.org_id == org_uuid).all()
            doc_dicts_for_amend = []
            for doc in docs_refreshed:
                obligations_orm = (
                    db.query(Obligation).filter(Obligation.doc_id == doc.id).all()
                )
                doc_dicts_for_amend.append({
                    "id": str(doc.id),
                    "doc_type": doc.doc_type,
                    "filename": doc.filename,
                    "obligations": [
                        {
                            "id": str(o.id),
                            "doc_id": str(o.doc_id),
                            "obligation_text": o.obligation_text,
                            "obligation_type": o.obligation_type,
                            "source_clause": o.source_clause,
                            "status": o.status,
                        }
                        for o in obligations_orm
                    ],
                })

            links_orm = (
                db.query(DocumentLink)
                .filter(DocumentLink.child_doc_id.in_([d.id for d in docs_refreshed]))
                .all()
            )
            link_dicts = [
                {
                    "child_doc_id": str(l.child_doc_id),
                    "parent_doc_id": str(l.parent_doc_id) if l.parent_doc_id else None,
                    "status": l.link_status,
                }
                for l in links_orm
            ]

            resolved = resolve_all(
                doc_dicts_for_amend, link_dicts, claude_client=claude_client
            )
            for obl_dict in resolved:
                obl_id = _uuid.UUID(obl_dict["id"])
                obl = db.query(Obligation).filter(Obligation.id == obl_id).first()
                if obl:
                    obl.status = obl_dict.get("status", obl.status)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Amendment resolution failed for org %s", org_name)

        _complete_stage("Amendment Resolution")

        # ---- Stage 6: Evidence Packaging ----
        if _cancel_event.is_set():
            return
        _set_stage("stage_6", "Stage 6: Evidence Packaging")

        from echelonos.stages.stage_6_evidence import package_evidence

        try:
            docs_refreshed = db.query(Document).filter(Document.org_id == org_uuid).all()
            doc_ids = [d.id for d in docs_refreshed]

            all_obligations = (
                db.query(Obligation).filter(Obligation.doc_id.in_(doc_ids)).all()
            )

            obl_dicts = [
                {
                    "id": str(o.id),
                    "doc_id": str(o.doc_id),
                    "obligation_text": o.obligation_text,
                    "obligation_type": o.obligation_type,
                    "source_clause": o.source_clause,
                    "source_page": o.source_page,
                    "status": o.status,
                    "confidence": o.confidence,
                    "verification_result": o.verification_result,
                }
                for o in all_obligations
            ]

            doc_map = {
                str(d.id): {
                    "id": str(d.id),
                    "doc_type": d.doc_type,
                    "filename": d.filename,
                }
                for d in docs_refreshed
            }

            verifications = {
                str(o.id): o.verification_result or {}
                for o in all_obligations
            }

            evidence_records = package_evidence(
                obligations=obl_dicts,
                documents=doc_map,
                verifications=verifications,
            )

            for ev in evidence_records:
                existing = (
                    db.query(Evidence)
                    .filter(
                        Evidence.obligation_id == _uuid.UUID(ev.obligation_id),
                        Evidence.doc_id == _uuid.UUID(ev.doc_id),
                        Evidence.source_clause == ev.source_clause,
                    )
                    .first()
                )
                if existing is None:
                    db.add(
                        Evidence(
                            id=_uuid.uuid4(),
                            obligation_id=_uuid.UUID(ev.obligation_id),
                            doc_id=_uuid.UUID(ev.doc_id),
                            page_number=ev.page_number,
                            source_clause=ev.source_clause,
                            extraction_model=ev.extraction_model,
                            verification_model=ev.verification_model,
                            verification_result=ev.verification_result,
                            confidence=ev.confidence,
                            amendment_history=ev.amendment_history,
                        )
                    )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Evidence packaging failed for org %s", org_name)

        _complete_stage("Evidence")

        # ---- Stage 7: Report (no-op) ----
        if _cancel_event.is_set():
            return
        _set_stage("stage_7", "Stage 7: Report Ready")
        _complete_stage("Report")

        # Done.
        _pipeline_status.update(
            state="done",
            current_stage=None,
            current_stage_label=None,
            finished_at=time.time(),
            elapsed_seconds=round(
                time.time() - (_pipeline_status.get("started_at") or time.time()), 2
            ),
        )

    except Exception as exc:
        logger.exception("Pipeline background thread failed for %s", org_name)
        _pipeline_status.update(
            state="error",
            finished_at=time.time(),
            elapsed_seconds=round(
                time.time() - (_pipeline_status.get("started_at") or time.time()), 2
            ),
            error=str(exc),
        )
    finally:
        if _test_session_factory is None:
            db.close()
        if _cancel_event.is_set() and _pipeline_status["state"] != "error":
            _pipeline_status.update(
                state="cancelled",
                current_stage=None,
                current_stage_label=None,
                finished_at=time.time(),
                elapsed_seconds=round(
                    time.time()
                    - (_pipeline_status.get("started_at") or time.time()),
                    2,
                ),
            )
