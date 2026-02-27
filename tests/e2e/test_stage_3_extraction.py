"""E2E tests for Stage 3: Obligation Extraction + Verification.

All LLM calls (Claude structured output and Claude verification) are mocked
to enable deterministic testing without API keys or network access.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from echelonos.stages.stage_3_extraction import (
    Obligation,
    ExtractionResult,
    extract_and_verify,
    extract_obligations,
    extract_party_roles,
    run_cove,
    verify_grounding,
    verify_with_claude,
)

# ---------------------------------------------------------------------------
# Shared fixtures & helpers
# ---------------------------------------------------------------------------

SAMPLE_CONTRACT_TEXT = (
    "SERVICES AGREEMENT\n\n"
    "This Services Agreement (\"Agreement\") is entered into as of January 1, 2025, "
    "by and between CDW Government LLC, hereinafter referred to as the \"Vendor\", "
    "and the State of California, hereinafter referred to as the \"Client\".\n\n"
    "ARTICLE 1 - DELIVERY OBLIGATIONS\n\n"
    "1.1 The Vendor shall deliver all hardware components to the Client's "
    "designated facility within 30 calendar days of the purchase order date.\n\n"
    "1.2 The Vendor shall provide quarterly status reports to the Client "
    "detailing delivery timelines and any anticipated delays.\n\n"
    "ARTICLE 2 - FINANCIAL TERMS\n\n"
    "2.1 The Client shall pay the Vendor within 45 days of receipt of a valid "
    "invoice. Late payments shall accrue interest at 1.5% per month.\n\n"
    "ARTICLE 3 - CONFIDENTIALITY\n\n"
    "3.1 Both parties shall maintain the confidentiality of all proprietary "
    "information exchanged under this Agreement for a period of 5 years "
    "following termination.\n\n"
    "ARTICLE 4 - INDEMNIFICATION\n\n"
    "4.1 The Vendor shall indemnify and hold harmless the Client from any "
    "claims arising from the Vendor's negligence or willful misconduct.\n"
)

SAMPLE_PARTY_ROLES = {
    "Vendor": "CDW Government LLC",
    "Client": "State of California",
}

SAMPLE_OBLIGATION = Obligation(
    obligation_text=(
        "Vendor must deliver all hardware components to Client's facility "
        "within 30 calendar days of the purchase order date."
    ),
    obligation_type="Delivery",
    responsible_party="Vendor",
    counterparty="Client",
    frequency=None,
    deadline="30 calendar days of the purchase order date",
    source_clause=(
        "The Vendor shall deliver all hardware components to the Client's "
        "designated facility within 30 calendar days of the purchase order date."
    ),
    source_page=1,
    confidence=0.95,
)

SAMPLE_LOW_CONFIDENCE_OBLIGATION = Obligation(
    obligation_text="Vendor must provide status reports to the Client.",
    obligation_type="Delivery",
    responsible_party="Vendor",
    counterparty="Client",
    frequency="Quarterly",
    deadline=None,
    source_clause=(
        "The Vendor shall provide quarterly status reports to the Client "
        "detailing delivery timelines and any anticipated delays."
    ),
    source_page=1,
    confidence=0.65,
)


def _patch_structured(return_value):
    """Patch extract_with_structured_output in stage_3_extraction."""
    return patch(
        "echelonos.stages.stage_3_extraction.extract_with_structured_output",
        return_value=return_value,
    )


def _patch_structured_side_effect(side_effect):
    """Patch extract_with_structured_output with multiple return values."""
    return patch(
        "echelonos.stages.stage_3_extraction.extract_with_structured_output",
        side_effect=side_effect,
    )


def _make_anthropic_response(text: str):
    """Build a mock Anthropic messages.create response."""
    content_block = SimpleNamespace(text=text)
    return SimpleNamespace(content=[content_block])


def _mock_anthropic_client():
    """Return a MagicMock that behaves like an Anthropic client."""
    client = MagicMock()
    client.messages.create = MagicMock()
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExtractPartyRoles:
    """Tests for extract_party_roles()."""

    def test_extract_party_roles(self) -> None:
        """Correct role mapping is extracted from contract text."""
        from echelonos.stages.stage_3_extraction import _PartyRolesResponse

        expected = _PartyRolesResponse(party_roles=SAMPLE_PARTY_ROLES)

        with _patch_structured(expected):
            roles = extract_party_roles(SAMPLE_CONTRACT_TEXT, claude_client=MagicMock())

        assert roles == SAMPLE_PARTY_ROLES
        assert roles["Vendor"] == "CDW Government LLC"
        assert roles["Client"] == "State of California"


class TestExtractObligations:
    """Tests for extract_obligations()."""

    def test_extract_obligations(self) -> None:
        """Obligations are extracted with correct Pydantic schema."""
        from echelonos.stages.stage_3_extraction import _ExtractionResponse

        obligations = [SAMPLE_OBLIGATION, SAMPLE_LOW_CONFIDENCE_OBLIGATION]
        expected = _ExtractionResponse(obligations=obligations)

        with _patch_structured(expected):
            result = extract_obligations(
                SAMPLE_CONTRACT_TEXT,
                SAMPLE_PARTY_ROLES,
                claude_client=MagicMock(),
            )

        assert isinstance(result, ExtractionResult)
        assert len(result.obligations) == 2
        assert result.party_roles == SAMPLE_PARTY_ROLES

        first = result.obligations[0]
        assert first.obligation_type == "Delivery"
        assert first.responsible_party == "Vendor"
        assert first.counterparty == "Client"
        assert first.confidence == 0.95
        assert first.source_page == 1


class TestGroundingCheck:
    """Tests for verify_grounding()."""

    def test_grounding_check_passes(self) -> None:
        """Source clause found in raw text returns True."""
        assert verify_grounding(SAMPLE_OBLIGATION, SAMPLE_CONTRACT_TEXT) is True

    def test_grounding_check_fails(self) -> None:
        """Fabricated source clause not in raw text returns False."""
        fabricated = Obligation(
            obligation_text="Vendor must fly to the moon.",
            obligation_type="Delivery",
            responsible_party="Vendor",
            counterparty="Client",
            frequency=None,
            deadline=None,
            source_clause="The Vendor shall fly to the moon by end of Q3.",
            source_page=1,
            confidence=0.90,
        )
        assert verify_grounding(fabricated, SAMPLE_CONTRACT_TEXT) is False


class TestClaudeVerification:
    """Tests for verify_with_claude()."""

    def test_claude_verification_agrees(self) -> None:
        """Claude confirms the obligation is verified."""
        mock_client = _mock_anthropic_client()
        mock_client.messages.create.return_value = _make_anthropic_response(
            '{"verified": true, "confidence": 0.95, '
            '"reason": "The source clause exists verbatim in the document and '
            'the obligation accurately reflects the contractual requirement."}'
        )

        result = verify_with_claude(
            SAMPLE_OBLIGATION,
            SAMPLE_CONTRACT_TEXT,
            anthropic_client=mock_client,
        )

        assert result["verified"] is True
        assert result["confidence"] == 0.95
        assert isinstance(result["reason"], str)
        assert len(result["reason"]) > 0

        mock_client.messages.create.assert_called_once()

    def test_claude_verification_disagrees(self) -> None:
        """Claude disputes the obligation -- verified=False."""
        fabricated = Obligation(
            obligation_text="Client must pay within 10 days.",
            obligation_type="Financial",
            responsible_party="Client",
            counterparty="Vendor",
            frequency=None,
            deadline="10 days",
            source_clause="The Client shall pay the Vendor within 10 days.",
            source_page=1,
            confidence=0.70,
        )

        mock_client = _mock_anthropic_client()
        mock_client.messages.create.return_value = _make_anthropic_response(
            '{"verified": false, "confidence": 0.30, '
            '"reason": "The source clause does not exist in the document. '
            'The actual payment term is 45 days, not 10 days."}'
        )

        result = verify_with_claude(
            fabricated,
            SAMPLE_CONTRACT_TEXT,
            anthropic_client=mock_client,
        )

        assert result["verified"] is False
        assert result["confidence"] == 0.30
        assert "10 days" in result["reason"] or "45 days" in result["reason"]


class TestCoVe:
    """Tests for run_cove() Chain-of-Verification."""

    def test_cove_runs_for_low_confidence(self) -> None:
        """CoVe runs and passes when all answers are found in the document."""
        from echelonos.stages.stage_3_extraction import (
            _CoVeAnswersResponse,
            _CoVeQuestionsResponse,
        )

        questions = [
            "What is the frequency of the status reports?",
            "Who is responsible for providing the status reports?",
            "What details should the reports contain?",
        ]
        answers = [
            "Quarterly",
            "The Vendor",
            "Delivery timelines and any anticipated delays",
        ]

        with _patch_structured_side_effect([
            _CoVeQuestionsResponse(questions=questions),
            _CoVeAnswersResponse(answers=answers),
        ]):
            result = run_cove(
                SAMPLE_LOW_CONFIDENCE_OBLIGATION,
                SAMPLE_CONTRACT_TEXT,
                claude_client=MagicMock(),
            )

        assert result["cove_passed"] is True
        assert len(result["questions"]) == 3
        assert len(result["answers"]) == 3

    def test_cove_skipped_for_high_confidence(self) -> None:
        """CoVe is not triggered when confidence >= 0.80.

        This test verifies the orchestrator behaviour rather than run_cove()
        itself -- the orchestrator should skip CoVe for high-confidence
        obligations.
        """
        from echelonos.stages.stage_3_extraction import (
            _ExtractionResponse,
            _PartyRolesResponse,
        )

        mock_claude = MagicMock()
        mock_claude.messages.create.return_value = _make_anthropic_response(
            '{"verified": true, "confidence": 0.95, "reason": "Verified."}'
        )

        with _patch_structured_side_effect([
            _PartyRolesResponse(party_roles=SAMPLE_PARTY_ROLES),
            _ExtractionResponse(obligations=[SAMPLE_OBLIGATION]),
            # No CoVe calls expected.
        ]):
            results = extract_and_verify(
                SAMPLE_CONTRACT_TEXT,
                claude_client=mock_claude,
            )

        assert len(results) == 1
        # CoVe should be None because confidence (0.95) >= 0.80.
        assert results[0]["cove"] is None


class TestFullPipeline:
    """End-to-end pipeline tests for extract_and_verify()."""

    def test_full_pipeline(self) -> None:
        """Full pipeline: extract -> ground -> verify -> result."""
        from echelonos.stages.stage_3_extraction import (
            _ExtractionResponse,
            _PartyRolesResponse,
        )

        mock_claude = MagicMock()
        # Claude verification agrees.
        mock_claude.messages.create.return_value = _make_anthropic_response(
            '{"verified": true, "confidence": 0.92, '
            '"reason": "The obligation is accurately extracted."}'
        )

        with _patch_structured_side_effect([
            _PartyRolesResponse(party_roles=SAMPLE_PARTY_ROLES),
            _ExtractionResponse(obligations=[SAMPLE_OBLIGATION]),
        ]):
            results = extract_and_verify(
                SAMPLE_CONTRACT_TEXT,
                claude_client=mock_claude,
            )

        assert len(results) == 1
        entry = results[0]

        # Obligation data is present.
        assert entry["obligation"]["obligation_type"] == "Delivery"
        assert entry["obligation"]["responsible_party"] == "Vendor"

        # Grounding passes (source_clause is in the contract text).
        assert entry["grounding"] is True

        # Claude verification passes.
        assert entry["claude_verification"]["verified"] is True

        # No CoVe needed (high confidence).
        assert entry["cove"] is None

        # Final status.
        assert entry["status"] == "VERIFIED"

    def test_unverified_marking(self) -> None:
        """Obligation with failed grounding + Claude disagreement -> UNVERIFIED."""
        from echelonos.stages.stage_3_extraction import (
            _ExtractionResponse,
            _PartyRolesResponse,
        )

        # Create an obligation whose source_clause does NOT appear in the text.
        bad_obligation = Obligation(
            obligation_text="Vendor must deliver within 7 business days.",
            obligation_type="Delivery",
            responsible_party="Vendor",
            counterparty="Client",
            frequency=None,
            deadline="7 business days",
            source_clause="The Vendor shall deliver within 7 business days.",
            source_page=1,
            confidence=0.85,
        )

        mock_claude = MagicMock()
        mock_claude.messages.create.return_value = _make_anthropic_response(
            '{"verified": false, "confidence": 0.20, '
            '"reason": "The source clause does not exist in the document. '
            'The actual delivery term is 30 calendar days, not 7 business days."}'
        )

        with _patch_structured_side_effect([
            _PartyRolesResponse(party_roles=SAMPLE_PARTY_ROLES),
            _ExtractionResponse(obligations=[bad_obligation]),
        ]):
            results = extract_and_verify(
                SAMPLE_CONTRACT_TEXT,
                claude_client=mock_claude,
            )

        assert len(results) == 1
        entry = results[0]

        # Grounding fails (fabricated clause).
        assert entry["grounding"] is False

        # Claude also disagrees.
        assert entry["claude_verification"]["verified"] is False

        # Final status is UNVERIFIED.
        assert entry["status"] == "UNVERIFIED"

    def test_pipeline_with_cove_triggered(self) -> None:
        """Low-confidence obligation triggers CoVe in the full pipeline."""
        from echelonos.stages.stage_3_extraction import (
            _CoVeAnswersResponse,
            _CoVeQuestionsResponse,
            _ExtractionResponse,
            _PartyRolesResponse,
        )

        mock_claude = MagicMock()
        mock_claude.messages.create.return_value = _make_anthropic_response(
            '{"verified": true, "confidence": 0.88, '
            '"reason": "Obligation matches the source clause."}'
        )

        with _patch_structured_side_effect([
            _PartyRolesResponse(party_roles=SAMPLE_PARTY_ROLES),
            _ExtractionResponse(obligations=[SAMPLE_LOW_CONFIDENCE_OBLIGATION]),
            _CoVeQuestionsResponse(
                questions=[
                    "What is the frequency of status reports?",
                    "Who must provide the reports?",
                ]
            ),
            _CoVeAnswersResponse(
                answers=[
                    "Quarterly",
                    "The Vendor",
                ]
            ),
        ]):
            results = extract_and_verify(
                SAMPLE_CONTRACT_TEXT,
                claude_client=mock_claude,
            )

        assert len(results) == 1
        entry = results[0]

        # Grounding passes.
        assert entry["grounding"] is True

        # Claude verifies.
        assert entry["claude_verification"]["verified"] is True

        # CoVe was triggered (confidence 0.65 < 0.80) and passed.
        assert entry["cove"] is not None
        assert entry["cove"]["cove_passed"] is True
        assert len(entry["cove"]["questions"]) == 2
        assert len(entry["cove"]["answers"]) == 2

        # All checks passed.
        assert entry["status"] == "VERIFIED"
