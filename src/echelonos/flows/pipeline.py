"""Main pipeline flow orchestrating all 8 stages."""

from prefect import flow, task
import structlog

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

    # Stages 1-7 will be added as they're implemented
    return {
        "org_folder": org_folder,
        "total_files": len(validated),
        "valid_files": len(valid_files),
        "unique_files": len(unique_files),
    }
