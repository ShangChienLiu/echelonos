"""Stage 6: Evidence Packaging (Immutable Audit Trail).

Creates append-only evidence records that trace every obligation back to its
source clause, extraction model, verification result, and amendment history.
Status changes produce NEW evidence records rather than updating existing ones,
preserving a complete audit trail.
"""

from __future__ import annotations

from enum import Enum

import structlog
from pydantic import BaseModel, Field, model_validator

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class VerificationResult(str, Enum):
    """Possible verification outcomes for an obligation."""

    CONFIRMED = "CONFIRMED"
    DISPUTED = "DISPUTED"
    UNVERIFIED = "UNVERIFIED"


class EvidenceRecord(BaseModel, frozen=True):
    """An immutable evidence record linking an obligation to its provenance.

    Records are append-only: once created they are never modified.  Status
    transitions create new records rather than updating existing ones.
    """

    obligation_id: str
    doc_id: str
    doc_filename: str
    page_number: int | None = None
    section_reference: str | None = None
    source_clause: str
    extraction_model: str
    verification_model: str
    verification_result: str = Field(
        description="CONFIRMED | DISPUTED | UNVERIFIED",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    amendment_history: list[dict] | None = None

    @model_validator(mode="after")
    def _validate_verification_result(self) -> "EvidenceRecord":
        allowed = {v.value for v in VerificationResult}
        if self.verification_result not in allowed:
            raise ValueError(
                f"verification_result must be one of {allowed}, "
                f"got {self.verification_result!r}"
            )
        return self


# ---------------------------------------------------------------------------
# Helper: map verification dict to a VerificationResult string
# ---------------------------------------------------------------------------


def _resolve_verification_result(verification: dict) -> str:
    """Derive a VerificationResult string from a verification dict.

    The verification dict is expected to come from Stage 3 and may contain
    a ``verified`` boolean and/or a ``result`` string.  This function
    normalises these into one of the three canonical values.
    """
    # If the dict already carries an explicit result string, prefer it.
    explicit = verification.get("result")
    if explicit and explicit in {v.value for v in VerificationResult}:
        return explicit

    verified = verification.get("verified")
    if verified is True:
        return VerificationResult.CONFIRMED.value
    if verified is False:
        return VerificationResult.DISPUTED.value
    return VerificationResult.UNVERIFIED.value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_evidence_record(
    obligation: dict,
    document: dict,
    verification: dict,
    amendment_history: list[dict] | None = None,
) -> EvidenceRecord:
    """Build a single evidence record from extraction + verification results.

    Parameters
    ----------
    obligation:
        Dict with at least ``obligation_id``, ``source_clause``,
        ``extraction_model``, ``source_page``, and ``confidence``.
    document:
        Dict with at least ``doc_id`` and ``filename``.
    verification:
        Dict with ``verification_model`` and either ``verified`` (bool)
        or ``result`` (str).  May also include ``confidence``.
    amendment_history:
        Optional list of amendment dicts, oldest-first.  Each dict should
        contain ``doc_id``, ``clause``, and ``status``.

    Returns
    -------
    EvidenceRecord
    """
    verification_result = _resolve_verification_result(verification)

    record = EvidenceRecord(
        obligation_id=obligation["obligation_id"],
        doc_id=document["doc_id"],
        doc_filename=document["filename"],
        page_number=obligation.get("source_page"),
        section_reference=obligation.get("section_reference"),
        source_clause=obligation["source_clause"],
        extraction_model=obligation["extraction_model"],
        verification_model=verification["verification_model"],
        verification_result=verification_result,
        confidence=verification.get("confidence", obligation.get("confidence", 0.0)),
        amendment_history=amendment_history,
    )

    log.info(
        "evidence_record_created",
        obligation_id=record.obligation_id,
        doc_id=record.doc_id,
        verification_result=record.verification_result,
    )
    return record


def package_evidence(
    obligations: list[dict],
    documents: dict[str, dict],
    verifications: dict[str, dict],
    amendment_chains: dict[str, list[dict]] | None = None,
) -> list[EvidenceRecord]:
    """Create evidence records for a batch of obligations.

    Parameters
    ----------
    obligations:
        List of obligation dicts.  Each must contain ``obligation_id``
        and ``doc_id`` (used to look up the corresponding document).
    documents:
        Lookup dict keyed by ``doc_id``.
    verifications:
        Lookup dict keyed by ``obligation_id``.
    amendment_chains:
        Optional lookup dict keyed by ``obligation_id`` mapping to an
        ordered list of amendment dicts.

    Returns
    -------
    list[EvidenceRecord]
    """
    amendment_chains = amendment_chains or {}
    records: list[EvidenceRecord] = []

    for obligation in obligations:
        ob_id = obligation["obligation_id"]
        doc_id = obligation["doc_id"]

        document = documents.get(doc_id)
        if document is None:
            log.warning(
                "evidence_missing_document",
                obligation_id=ob_id,
                doc_id=doc_id,
            )
            continue

        verification = verifications.get(ob_id)
        if verification is None:
            log.warning(
                "evidence_missing_verification",
                obligation_id=ob_id,
            )
            continue

        amendment_history = amendment_chains.get(ob_id)

        record = create_evidence_record(
            obligation=obligation,
            document=document,
            verification=verification,
            amendment_history=amendment_history,
        )
        records.append(record)

    log.info(
        "evidence_packaging_complete",
        total_obligations=len(obligations),
        total_records=len(records),
        skipped=len(obligations) - len(records),
    )
    return records


def create_status_change_record(
    obligation_id: str,
    old_status: str,
    new_status: str,
    reason: str,
    changed_by_doc_id: str | None = None,
) -> EvidenceRecord:
    """Create an append-only evidence record for a status transition.

    Rather than mutating existing evidence, a NEW record is produced that
    captures the old status, the new status, and the reason for the change.
    This preserves the full audit trail.

    Parameters
    ----------
    obligation_id:
        The obligation whose status changed.
    old_status:
        Previous status value (e.g. ``ACTIVE``).
    new_status:
        New status value (e.g. ``SUPERSEDED``).
    reason:
        Human-readable explanation of the change.
    changed_by_doc_id:
        Optional document ID that triggered the change (e.g. an amendment).

    Returns
    -------
    EvidenceRecord
    """
    record = EvidenceRecord(
        obligation_id=obligation_id,
        doc_id=changed_by_doc_id or "SYSTEM",
        doc_filename="status_change",
        page_number=None,
        section_reference=None,
        source_clause=f"Status changed from {old_status} to {new_status}: {reason}",
        extraction_model="SYSTEM",
        verification_model="SYSTEM",
        verification_result=VerificationResult.UNVERIFIED.value,
        confidence=1.0,
        amendment_history=[
            {
                "old_status": old_status,
                "new_status": new_status,
                "reason": reason,
                "changed_by_doc_id": changed_by_doc_id,
            }
        ],
    )

    log.info(
        "status_change_recorded",
        obligation_id=obligation_id,
        old_status=old_status,
        new_status=new_status,
        reason=reason,
    )
    return record


def validate_evidence_chain(records: list[EvidenceRecord]) -> dict:
    """Validate that the evidence chain is complete and consistent.

    Checks performed:
    1. Every unique obligation ID in the records has at least one evidence
       record.
    2. Amendment histories (when present) are checked for continuity -- each
       entry must have ``doc_id``, ``clause``, and ``status`` keys.

    Parameters
    ----------
    records:
        All evidence records to validate.

    Returns
    -------
    dict with keys:
        - valid       (bool)
        - missing_evidence  (list[str])  obligation IDs with no records
        - gaps        (list[str])  descriptions of amendment-history gaps
    """
    # Collect all obligation IDs that have at least one record.
    covered_obligation_ids: set[str] = set()
    gaps: list[str] = []

    for record in records:
        covered_obligation_ids.add(record.obligation_id)

        # Check amendment history integrity when present.
        if record.amendment_history:
            for idx, entry in enumerate(record.amendment_history):
                required_keys = {"doc_id", "clause", "status"}
                missing_keys = required_keys - set(entry.keys())
                if missing_keys:
                    gaps.append(
                        f"obligation {record.obligation_id}: amendment_history[{idx}] "
                        f"missing keys {missing_keys}"
                    )

    # Identify obligations referenced in records but with no evidence.
    # (In practice the caller would also supply a list of *expected*
    # obligation IDs; here we can only detect self-consistency.)
    missing_evidence: list[str] = []
    # We derive the full set from the records themselves -- if a record
    # references an obligation_id that has no *other* evidence record,
    # it still counts as covered.  The more useful check is done by the
    # caller comparing against the full obligation list.

    result = {
        "valid": len(gaps) == 0 and len(missing_evidence) == 0,
        "missing_evidence": missing_evidence,
        "gaps": gaps,
    }

    log.info(
        "evidence_chain_validated",
        valid=result["valid"],
        total_records=len(records),
        gaps=len(gaps),
    )
    return result


def validate_evidence_chain_against_obligations(
    records: list[EvidenceRecord],
    expected_obligation_ids: list[str],
) -> dict:
    """Validate the evidence chain against a known set of obligation IDs.

    This is the stronger variant of :func:`validate_evidence_chain` that
    also checks that every expected obligation ID has at least one evidence
    record.

    Parameters
    ----------
    records:
        All evidence records to validate.
    expected_obligation_ids:
        The full list of obligation IDs that should be covered.

    Returns
    -------
    dict with keys:
        - valid              (bool)
        - missing_evidence   (list[str])
        - gaps               (list[str])
    """
    base_result = validate_evidence_chain(records)

    covered = {r.obligation_id for r in records}
    missing = [oid for oid in expected_obligation_ids if oid not in covered]

    valid = base_result["valid"] and len(missing) == 0

    result = {
        "valid": valid,
        "missing_evidence": missing,
        "gaps": base_result["gaps"],
    }

    log.info(
        "evidence_chain_validated_against_obligations",
        valid=result["valid"],
        expected=len(expected_obligation_ids),
        missing=len(missing),
    )
    return result
