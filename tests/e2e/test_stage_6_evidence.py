"""E2E tests for Stage 6: Evidence Packaging (Immutable Audit Trail).

All functions under test are pure -- no mocking or external services required.
Test data is defined inline for clarity and self-containment.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from echelonos.stages.stage_6_evidence import (
    EvidenceRecord,
    VerificationResult,
    create_evidence_record,
    create_status_change_record,
    package_evidence,
    validate_evidence_chain,
    validate_evidence_chain_against_obligations,
)

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

SAMPLE_OBLIGATION = {
    "obligation_id": "ob-001",
    "doc_id": "doc-aaa",
    "source_clause": (
        "The Vendor shall deliver all hardware components to the Client's "
        "designated facility within 30 calendar days of the purchase order date."
    ),
    "extraction_model": "gpt-4o-2025-04-01",
    "source_page": 3,
    "section_reference": "Article 1.1",
    "confidence": 0.95,
}

SAMPLE_DOCUMENT = {
    "doc_id": "doc-aaa",
    "filename": "services_agreement_v2.pdf",
}

SAMPLE_VERIFICATION_CONFIRMED = {
    "verification_model": "claude-sonnet-4-20250514",
    "verified": True,
    "confidence": 0.92,
    "reason": "Source clause exists verbatim in the document.",
}

SAMPLE_VERIFICATION_DISPUTED = {
    "verification_model": "claude-sonnet-4-20250514",
    "verified": False,
    "confidence": 0.30,
    "reason": "The source clause does not exist in the document.",
}

SAMPLE_VERIFICATION_UNVERIFIED = {
    "verification_model": "claude-sonnet-4-20250514",
    # No 'verified' key -- triggers UNVERIFIED.
    "confidence": 0.50,
}

SAMPLE_AMENDMENT_HISTORY = [
    {
        "doc_id": "doc-aaa",
        "clause": "Article 1.1 - Original 30-day delivery term",
        "status": "ACTIVE",
    },
    {
        "doc_id": "doc-bbb",
        "clause": "Amendment 1, Section 2 - Extended to 45 days",
        "status": "SUPERSEDED",
    },
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateEvidenceRecord:
    """Tests for create_evidence_record()."""

    def test_create_evidence_record(self) -> None:
        """Correct fields are populated from obligation, document, and verification."""
        record = create_evidence_record(
            obligation=SAMPLE_OBLIGATION,
            document=SAMPLE_DOCUMENT,
            verification=SAMPLE_VERIFICATION_CONFIRMED,
        )

        assert isinstance(record, EvidenceRecord)
        assert record.obligation_id == "ob-001"
        assert record.doc_id == "doc-aaa"
        assert record.doc_filename == "services_agreement_v2.pdf"
        assert record.page_number == 3
        assert record.section_reference == "Article 1.1"
        assert "30 calendar days" in record.source_clause
        assert record.extraction_model == "gpt-4o-2025-04-01"
        assert record.verification_model == "claude-sonnet-4-20250514"
        assert record.verification_result == "CONFIRMED"
        assert record.confidence == 0.92
        assert record.amendment_history is None

    def test_create_evidence_with_amendment_history(self) -> None:
        """Amendment history is correctly included in the evidence record."""
        record = create_evidence_record(
            obligation=SAMPLE_OBLIGATION,
            document=SAMPLE_DOCUMENT,
            verification=SAMPLE_VERIFICATION_CONFIRMED,
            amendment_history=SAMPLE_AMENDMENT_HISTORY,
        )

        assert record.amendment_history is not None
        assert len(record.amendment_history) == 2

        # Oldest entry first.
        assert record.amendment_history[0]["doc_id"] == "doc-aaa"
        assert record.amendment_history[0]["status"] == "ACTIVE"

        # Newest entry last.
        assert record.amendment_history[1]["doc_id"] == "doc-bbb"
        assert record.amendment_history[1]["status"] == "SUPERSEDED"


class TestPackageEvidence:
    """Tests for package_evidence()."""

    def test_package_evidence_batch(self) -> None:
        """Multiple obligations are packaged into corresponding evidence records."""
        obligation_2 = {
            "obligation_id": "ob-002",
            "doc_id": "doc-aaa",
            "source_clause": "Client shall pay within 45 days of receipt of invoice.",
            "extraction_model": "gpt-4o-2025-04-01",
            "source_page": 5,
            "confidence": 0.88,
        }
        verification_2 = {
            "verification_model": "claude-sonnet-4-20250514",
            "verified": True,
            "confidence": 0.90,
        }

        obligations = [SAMPLE_OBLIGATION, obligation_2]
        documents = {"doc-aaa": SAMPLE_DOCUMENT}
        verifications = {
            "ob-001": SAMPLE_VERIFICATION_CONFIRMED,
            "ob-002": verification_2,
        }

        records = package_evidence(obligations, documents, verifications)

        assert len(records) == 2
        assert records[0].obligation_id == "ob-001"
        assert records[1].obligation_id == "ob-002"
        assert records[0].verification_result == "CONFIRMED"
        assert records[1].verification_result == "CONFIRMED"
        assert records[1].page_number == 5

    def test_package_evidence_skips_missing_document(self) -> None:
        """Obligations referencing unknown doc_ids are skipped gracefully."""
        obligation_orphan = {
            "obligation_id": "ob-orphan",
            "doc_id": "doc-missing",
            "source_clause": "This will be skipped.",
            "extraction_model": "gpt-4o-2025-04-01",
            "confidence": 0.80,
        }
        verifications = {
            "ob-orphan": SAMPLE_VERIFICATION_CONFIRMED,
        }

        records = package_evidence(
            obligations=[obligation_orphan],
            documents={},
            verifications=verifications,
        )

        assert len(records) == 0

    def test_package_evidence_skips_missing_verification(self) -> None:
        """Obligations with no matching verification are skipped gracefully."""
        records = package_evidence(
            obligations=[SAMPLE_OBLIGATION],
            documents={"doc-aaa": SAMPLE_DOCUMENT},
            verifications={},  # No verification for ob-001.
        )

        assert len(records) == 0

    def test_package_evidence_with_amendment_chains(self) -> None:
        """Amendment chains are attached to the correct evidence records."""
        amendment_chains = {
            "ob-001": SAMPLE_AMENDMENT_HISTORY,
        }

        records = package_evidence(
            obligations=[SAMPLE_OBLIGATION],
            documents={"doc-aaa": SAMPLE_DOCUMENT},
            verifications={"ob-001": SAMPLE_VERIFICATION_CONFIRMED},
            amendment_chains=amendment_chains,
        )

        assert len(records) == 1
        assert records[0].amendment_history is not None
        assert len(records[0].amendment_history) == 2


class TestStatusChangeRecord:
    """Tests for create_status_change_record()."""

    def test_status_change_record(self) -> None:
        """Status transition creates a new record with old/new status captured."""
        record = create_status_change_record(
            obligation_id="ob-001",
            old_status="ACTIVE",
            new_status="SUPERSEDED",
            reason="Amendment doc-bbb extends delivery to 45 days.",
            changed_by_doc_id="doc-bbb",
        )

        assert isinstance(record, EvidenceRecord)
        assert record.obligation_id == "ob-001"
        assert record.doc_id == "doc-bbb"
        assert record.doc_filename == "status_change"
        assert "ACTIVE" in record.source_clause
        assert "SUPERSEDED" in record.source_clause
        assert record.extraction_model == "SYSTEM"
        assert record.verification_model == "SYSTEM"
        assert record.verification_result == "UNVERIFIED"
        assert record.confidence == 1.0

        # Amendment history captures the transition details.
        assert record.amendment_history is not None
        assert len(record.amendment_history) == 1
        entry = record.amendment_history[0]
        assert entry["old_status"] == "ACTIVE"
        assert entry["new_status"] == "SUPERSEDED"
        assert entry["reason"] == "Amendment doc-bbb extends delivery to 45 days."
        assert entry["changed_by_doc_id"] == "doc-bbb"

    def test_status_change_without_doc_id(self) -> None:
        """Status change triggered by the system (no specific document)."""
        record = create_status_change_record(
            obligation_id="ob-003",
            old_status="ACTIVE",
            new_status="TERMINATED",
            reason="Contract expired on 2025-12-31.",
        )

        assert record.doc_id == "SYSTEM"
        assert record.amendment_history[0]["changed_by_doc_id"] is None


class TestEvidenceImmutability:
    """Tests verifying that EvidenceRecord is immutable (frozen)."""

    def test_evidence_immutability(self) -> None:
        """EvidenceRecord is frozen -- field assignment raises an error."""
        record = create_evidence_record(
            obligation=SAMPLE_OBLIGATION,
            document=SAMPLE_DOCUMENT,
            verification=SAMPLE_VERIFICATION_CONFIRMED,
        )

        with pytest.raises(ValidationError):
            record.confidence = 0.50  # type: ignore[misc]

        with pytest.raises(ValidationError):
            record.verification_result = "DISPUTED"  # type: ignore[misc]

        with pytest.raises(ValidationError):
            record.source_clause = "tampered"  # type: ignore[misc]

    def test_status_change_produces_new_record(self) -> None:
        """Demonstrate append-only pattern: status changes yield distinct records."""
        original = create_evidence_record(
            obligation=SAMPLE_OBLIGATION,
            document=SAMPLE_DOCUMENT,
            verification=SAMPLE_VERIFICATION_CONFIRMED,
        )

        transition = create_status_change_record(
            obligation_id="ob-001",
            old_status="ACTIVE",
            new_status="SUPERSEDED",
            reason="Replaced by amendment.",
        )

        # Two separate record objects -- the original is not mutated.
        assert original is not transition
        assert original.verification_result == "CONFIRMED"
        assert transition.verification_result == "UNVERIFIED"
        assert original.source_clause != transition.source_clause


class TestValidateEvidenceChain:
    """Tests for validate_evidence_chain() and the obligation-aware variant."""

    def test_validate_evidence_chain_valid(self) -> None:
        """A complete chain with proper amendment histories passes validation."""
        record_1 = create_evidence_record(
            obligation=SAMPLE_OBLIGATION,
            document=SAMPLE_DOCUMENT,
            verification=SAMPLE_VERIFICATION_CONFIRMED,
            amendment_history=SAMPLE_AMENDMENT_HISTORY,
        )

        obligation_2 = {
            **SAMPLE_OBLIGATION,
            "obligation_id": "ob-002",
        }
        record_2 = create_evidence_record(
            obligation=obligation_2,
            document=SAMPLE_DOCUMENT,
            verification=SAMPLE_VERIFICATION_CONFIRMED,
        )

        result = validate_evidence_chain([record_1, record_2])

        assert result["valid"] is True
        assert result["missing_evidence"] == []
        assert result["gaps"] == []

    def test_validate_evidence_chain_missing(self) -> None:
        """Missing evidence for an expected obligation is detected."""
        record = create_evidence_record(
            obligation=SAMPLE_OBLIGATION,
            document=SAMPLE_DOCUMENT,
            verification=SAMPLE_VERIFICATION_CONFIRMED,
        )

        # We only have evidence for ob-001, but expect ob-001 AND ob-002.
        result = validate_evidence_chain_against_obligations(
            records=[record],
            expected_obligation_ids=["ob-001", "ob-002"],
        )

        assert result["valid"] is False
        assert "ob-002" in result["missing_evidence"]
        assert "ob-001" not in result["missing_evidence"]

    def test_validate_evidence_chain_amendment_gaps(self) -> None:
        """Amendment history entries with missing keys are reported as gaps."""
        # Build a record with a malformed amendment history entry.
        bad_amendment_history = [
            {"doc_id": "doc-aaa", "clause": "Original clause", "status": "ACTIVE"},
            {"doc_id": "doc-bbb"},  # Missing 'clause' and 'status'.
        ]

        record = create_evidence_record(
            obligation=SAMPLE_OBLIGATION,
            document=SAMPLE_DOCUMENT,
            verification=SAMPLE_VERIFICATION_CONFIRMED,
            amendment_history=bad_amendment_history,
        )

        result = validate_evidence_chain([record])

        assert result["valid"] is False
        assert len(result["gaps"]) == 1
        assert "ob-001" in result["gaps"][0]
        assert "amendment_history[1]" in result["gaps"][0]

    def test_validate_empty_records(self) -> None:
        """An empty record list is trivially valid."""
        result = validate_evidence_chain([])

        assert result["valid"] is True
        assert result["missing_evidence"] == []
        assert result["gaps"] == []


class TestVerificationResultTypes:
    """Tests that CONFIRMED, DISPUTED, and UNVERIFIED are all handled correctly."""

    def test_confirmed_result(self) -> None:
        """verified=True maps to CONFIRMED."""
        record = create_evidence_record(
            obligation=SAMPLE_OBLIGATION,
            document=SAMPLE_DOCUMENT,
            verification=SAMPLE_VERIFICATION_CONFIRMED,
        )
        assert record.verification_result == "CONFIRMED"

    def test_disputed_result(self) -> None:
        """verified=False maps to DISPUTED."""
        record = create_evidence_record(
            obligation=SAMPLE_OBLIGATION,
            document=SAMPLE_DOCUMENT,
            verification=SAMPLE_VERIFICATION_DISPUTED,
        )
        assert record.verification_result == "DISPUTED"
        assert record.confidence == 0.30

    def test_unverified_result(self) -> None:
        """Missing 'verified' key maps to UNVERIFIED."""
        record = create_evidence_record(
            obligation=SAMPLE_OBLIGATION,
            document=SAMPLE_DOCUMENT,
            verification=SAMPLE_VERIFICATION_UNVERIFIED,
        )
        assert record.verification_result == "UNVERIFIED"
        assert record.confidence == 0.50

    def test_explicit_result_string(self) -> None:
        """An explicit 'result' key in the verification dict is preferred."""
        verification = {
            "verification_model": "claude-sonnet-4-20250514",
            "result": "DISPUTED",
            "verified": True,  # This is contradictory but 'result' takes precedence.
            "confidence": 0.40,
        }

        record = create_evidence_record(
            obligation=SAMPLE_OBLIGATION,
            document=SAMPLE_DOCUMENT,
            verification=verification,
        )
        assert record.verification_result == "DISPUTED"

    def test_invalid_verification_result_rejected(self) -> None:
        """An invalid verification_result string raises a ValidationError."""
        with pytest.raises(ValidationError, match="verification_result"):
            EvidenceRecord(
                obligation_id="ob-bad",
                doc_id="doc-bad",
                doc_filename="bad.pdf",
                source_clause="Some clause.",
                extraction_model="gpt-4o",
                verification_model="claude",
                verification_result="MAYBE",  # Not a valid value.
                confidence=0.5,
            )

    def test_confidence_bounds(self) -> None:
        """Confidence must be between 0.0 and 1.0 inclusive."""
        with pytest.raises(ValidationError):
            EvidenceRecord(
                obligation_id="ob-x",
                doc_id="doc-x",
                doc_filename="x.pdf",
                source_clause="Clause.",
                extraction_model="gpt-4o",
                verification_model="claude",
                verification_result="CONFIRMED",
                confidence=1.5,
            )

        with pytest.raises(ValidationError):
            EvidenceRecord(
                obligation_id="ob-x",
                doc_id="doc-x",
                doc_filename="x.pdf",
                source_clause="Clause.",
                extraction_model="gpt-4o",
                verification_model="claude",
                verification_result="CONFIRMED",
                confidence=-0.1,
            )
