"""FastAPI application for the Echelonos obligation report API.

Serves obligation report data from the PostgreSQL database, with a
fallback to demo data when the requested organization is not found.

Run standalone with::

    uvicorn echelonos.api.app:app --reload
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session

from echelonos.api.demo_data import DEMO_DOCUMENTS, DEMO_LINKS, DEMO_OBLIGATIONS
from echelonos.db.models import Document, DocumentLink, Obligation, Organization
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
