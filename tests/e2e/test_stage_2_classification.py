"""E2E tests for Stage 2: Document Classification.

Each test exercises the public API of stage_2_classification with a mocked
Claude client so that no real API calls are made.  The mock returns realistic
structured responses matching the ClassificationResult schema.
"""

from unittest.mock import MagicMock, patch

import pytest

from echelonos.stages.stage_2_classification import (
    ClassificationResult,
    classify_document,
    classify_with_cross_check,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_extract(result: ClassificationResult):
    """Return a context-manager that patches ``extract_with_structured_output``
    in the classification module to return *result*.
    """
    return patch(
        "echelonos.stages.stage_2_classification.extract_with_structured_output",
        return_value=result,
    )


# ---------------------------------------------------------------------------
# Sample texts
# ---------------------------------------------------------------------------

MSA_TEXT = """\
MASTER SERVICE AGREEMENT

This Master Service Agreement ("Agreement") is entered into as of January 15,
2025, by and between Acme Corp ("Client") and Globex Inc ("Provider").

1. SCOPE OF SERVICES
The Provider shall provide the services described in each Statement of Work
executed under this Agreement.

2. TERM
This Agreement shall commence on the Effective Date and continue for a period
of three (3) years unless earlier terminated.
"""

AMENDMENT_TEXT = """\
FIRST AMENDMENT TO MASTER SERVICE AGREEMENT

This First Amendment ("Amendment") hereby amends the Master Service Agreement
dated January 15, 2025 ("Original Agreement") between Acme Corp and Globex Inc.

This Amendment modifies Section 5.2 of the Original Agreement as follows:
The payment terms shall be Net 45 instead of Net 30.
"""

SOW_TEXT = """\
STATEMENT OF WORK #1

Under the Master Service Agreement dated January 15, 2025 between Acme Corp
and Globex Inc, the following work shall be performed:

Deliverables:
- Data migration from legacy system
- API integration with client platform
- User acceptance testing

Timeline: February 1, 2025 through April 30, 2025
Total Fee: $150,000
"""

NDA_TEXT = """\
NON-DISCLOSURE AGREEMENT

This Non-Disclosure Agreement is entered into as of March 1, 2025, by and
between TechStart LLC ("Disclosing Party") and DataSafe Corp ("Receiving Party").

The Receiving Party agrees to hold in confidence all proprietary information
disclosed by the Disclosing Party.
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestClassifyDocument:
    """Tests targeting classify_document()."""

    def test_classify_msa(self) -> None:
        """Text clearly describing an MSA should return doc_type='MSA'."""
        expected = ClassificationResult(
            doc_type="MSA",
            parties=["Acme Corp", "Globex Inc"],
            effective_date="2025-01-15",
            parent_reference_raw=None,
            confidence=0.95,
        )

        with _patch_extract(expected):
            result = classify_document(MSA_TEXT, claude_client=MagicMock())

        assert result.doc_type == "MSA"
        assert result.confidence >= 0.7
        assert "Acme Corp" in result.parties
        assert "Globex Inc" in result.parties

    def test_classify_amendment(self) -> None:
        """Text with 'hereby amends' should return doc_type='Amendment'."""
        expected = ClassificationResult(
            doc_type="Amendment",
            parties=["Acme Corp", "Globex Inc"],
            effective_date=None,
            parent_reference_raw="Master Service Agreement dated January 15, 2025",
            confidence=0.92,
        )

        with _patch_extract(expected):
            result = classify_document(AMENDMENT_TEXT, claude_client=MagicMock())

        assert result.doc_type == "Amendment"
        assert result.parent_reference_raw is not None
        assert "January 15, 2025" in result.parent_reference_raw

    def test_low_confidence_becomes_unknown(self) -> None:
        """When the model's confidence is below 0.7 the doc_type must
        be overridden to 'UNKNOWN'."""
        expected = ClassificationResult(
            doc_type="SOW",
            parties=["Acme Corp"],
            effective_date=None,
            parent_reference_raw=None,
            confidence=0.5,
        )

        with _patch_extract(expected):
            result = classify_document(SOW_TEXT, claude_client=MagicMock())

        assert result.doc_type == "UNKNOWN"
        assert result.confidence == 0.5

    def test_parties_extraction(self) -> None:
        """Verify the parties list is correctly passed through."""
        expected = ClassificationResult(
            doc_type="NDA",
            parties=["TechStart LLC", "DataSafe Corp"],
            effective_date="2025-03-01",
            parent_reference_raw=None,
            confidence=0.98,
        )

        with _patch_extract(expected):
            result = classify_document(NDA_TEXT, claude_client=MagicMock())

        assert len(result.parties) == 2
        assert "TechStart LLC" in result.parties
        assert "DataSafe Corp" in result.parties

    def test_effective_date_extraction(self) -> None:
        """Verify the effective_date field is correctly extracted."""
        expected = ClassificationResult(
            doc_type="MSA",
            parties=["Acme Corp", "Globex Inc"],
            effective_date="2025-01-15",
            parent_reference_raw=None,
            confidence=0.95,
        )

        with _patch_extract(expected):
            result = classify_document(MSA_TEXT, claude_client=MagicMock())

        assert result.effective_date == "2025-01-15"

    def test_empty_text_handling(self) -> None:
        """An empty string input should return UNKNOWN with zero confidence
        without making an API call."""
        result = classify_document("", claude_client=MagicMock())

        assert result.doc_type == "UNKNOWN"
        assert result.confidence == 0.0
        assert result.parties == []
        assert result.effective_date is None
        assert result.parent_reference_raw is None

    def test_whitespace_only_text_handling(self) -> None:
        """Whitespace-only input should be treated the same as empty."""
        result = classify_document("   \n\t  ", claude_client=MagicMock())

        assert result.doc_type == "UNKNOWN"
        assert result.confidence == 0.0


class TestClassifyWithCrossCheck:
    """Tests targeting classify_with_cross_check()."""

    def test_cross_check_reclassifies_amendment(self) -> None:
        """An MSA classification on text containing 'hereby amends' should
        be reclassified as Amendment."""
        initial = ClassificationResult(
            doc_type="MSA",
            parties=["Acme Corp", "Globex Inc"],
            effective_date=None,
            parent_reference_raw="Master Service Agreement dated January 15, 2025",
            confidence=0.85,
        )

        result = classify_with_cross_check(AMENDMENT_TEXT, initial)

        assert result.doc_type == "Amendment"
        # Parties and other fields should be preserved.
        assert result.parties == ["Acme Corp", "Globex Inc"]
        assert result.confidence == 0.85

    def test_cross_check_flags_amendment_without_parent(self) -> None:
        """An Amendment with no parent_reference_raw should be flagged
        as suspicious."""
        initial = ClassificationResult(
            doc_type="Amendment",
            parties=["Acme Corp", "Globex Inc"],
            effective_date=None,
            parent_reference_raw=None,
            confidence=0.88,
        )

        result = classify_with_cross_check(AMENDMENT_TEXT, initial)

        assert result.doc_type == "Amendment"
        assert result.parent_reference_raw is not None
        assert "SUSPICIOUS" in result.parent_reference_raw

    def test_cross_check_does_not_alter_correct_msa(self) -> None:
        """An MSA classification on genuine MSA text should not be changed."""
        initial = ClassificationResult(
            doc_type="MSA",
            parties=["Acme Corp", "Globex Inc"],
            effective_date="2025-01-15",
            parent_reference_raw=None,
            confidence=0.95,
        )

        result = classify_with_cross_check(MSA_TEXT, initial)

        assert result.doc_type == "MSA"
        assert result.effective_date == "2025-01-15"

    def test_cross_check_does_not_alter_amendment_with_parent(self) -> None:
        """An Amendment with a valid parent_reference_raw should not be
        flagged as suspicious."""
        initial = ClassificationResult(
            doc_type="Amendment",
            parties=["Acme Corp", "Globex Inc"],
            effective_date=None,
            parent_reference_raw="Master Service Agreement dated January 15, 2025",
            confidence=0.92,
        )

        result = classify_with_cross_check(AMENDMENT_TEXT, initial)

        assert result.doc_type == "Amendment"
        assert "SUSPICIOUS" not in result.parent_reference_raw

    def test_cross_check_modifies_section_triggers_reclassification(self) -> None:
        """Text with 'modifies Section' should also trigger reclassification
        from MSA to Amendment."""
        text_with_modifies = (
            "This document modifies Section 3 of the prior agreement "
            "between Alpha LLC and Beta Inc."
        )
        initial = ClassificationResult(
            doc_type="MSA",
            parties=["Alpha LLC", "Beta Inc"],
            effective_date=None,
            parent_reference_raw="prior agreement",
            confidence=0.80,
        )

        result = classify_with_cross_check(text_with_modifies, initial)

        assert result.doc_type == "Amendment"
