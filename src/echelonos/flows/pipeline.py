"""Main pipeline flow orchestrating all 8 stages."""

import os

from prefect import flow, task
import structlog

from echelonos.db.session import SessionLocal
from echelonos.db.persist import get_or_create_organization, upsert_document

logger = structlog.get_logger()


@task(retries=2, retry_delay_seconds=30)
def stage_0a_validate(org_folder: str) -> list[dict]:
    """Stage 0a: Validate all files in an organization folder."""
    from echelonos.stages.stage_0a_validation import validate_folder

    return validate_folder(org_folder)


@task(retries=1)
def stage_0b_dedup(valid_files: list[dict]) -> list[dict]:
    """Stage 0b: Deduplicate files using 4-layer hash pipeline."""
    from echelonos.stages.stage_0b_dedup import deduplicate_files

    return deduplicate_files(valid_files)


@task
def persist_stage_0(org_folder: str, unique_files: list[dict]) -> dict:
    """Persist organization and documents after stages 0a/0b."""
    org_name = os.path.basename(org_folder.rstrip("/"))
    db = SessionLocal()
    try:
        org = get_or_create_organization(
            db, name=org_name, folder_path=org_folder,
        )
        doc_ids = []
        for f in unique_files:
            doc = upsert_document(
                db,
                org_id=org.id,
                file_path=f["file_path"],
                filename=os.path.basename(f["file_path"]),
                status=f.get("status", "VALID"),
            )
            doc_ids.append(str(doc.id))
        db.commit()
        logger.info(
            "pipeline.persist_stage_0",
            org_id=str(org.id),
            documents=len(doc_ids),
        )
        return {"org_id": str(org.id), "doc_ids": doc_ids}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@flow(name="echelonos-pipeline", log_prints=True)
def run_pipeline(org_folder: str) -> dict:
    """Run the full 8-stage obligation extraction pipeline for an organization."""
    logger.info("pipeline.start", org_folder=org_folder)

    # Stage 0a: File Validation
    validated = stage_0a_validate(org_folder)
    valid_files = [f for f in validated if f["status"] == "VALID"]
    logger.info("pipeline.stage_0a_complete", total=len(validated), valid=len(valid_files))

    # Stage 0b: Deduplication
    unique_files = stage_0b_dedup(valid_files)
    logger.info("pipeline.stage_0b_complete", unique=len(unique_files))

    # Persist organizations & documents
    persisted = persist_stage_0(org_folder, unique_files)

    # Stages 1-7 will be added as they're implemented
    return {
        "org_folder": org_folder,
        "total_files": len(validated),
        "valid_files": len(valid_files),
        "unique_files": len(unique_files),
        "org_id": persisted["org_id"],
        "doc_ids": persisted["doc_ids"],
    }
