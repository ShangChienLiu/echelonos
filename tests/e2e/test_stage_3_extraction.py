"""E2E tests for Stage 3: Obligation Extraction + Dual Ensemble Verification.

All LLM calls (Claude structured output) are mocked to enable deterministic
testing without API keys or network access.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from echelonos.stages.stage_3_extraction import (
    Obligation,
    ExtractionResult,
    check_agreement,
    extract_and_verify,
    extract_obligations,
    extract_obligations_independent,
    extract_party_roles,
    match_extractions,
    run_cove,
    verify_grounding,
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

# Independent extraction of the same obligation -- slightly different wording.
SAMPLE_OBLIGATION_INDEPENDENT = Obligation(
    obligation_text=(
        "Vendor is required to deliver hardware components to Client's "
        "designated facility within 30 calendar days of purchase order."
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
    confidence=0.92,
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

    def test_extract_obligations_independent(self) -> None:
        """Independent extraction uses different prompt but same schema."""
        from echelonos.stages.stage_3_extraction import _ExtractionResponse

        obligations = [SAMPLE_OBLIGATION_INDEPENDENT]
        expected = _ExtractionResponse(obligations=obligations)

        with _patch_structured(expected):
            result = extract_obligations_independent(
                SAMPLE_CONTRACT_TEXT,
                SAMPLE_PARTY_ROLES,
                claude_client=MagicMock(),
            )

        assert isinstance(result, ExtractionResult)
        assert len(result.obligations) == 1
        assert result.obligations[0].obligation_type == "Delivery"


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


class TestMatchExtractions:
    """Tests for match_extractions()."""

    def test_matching_pairs_by_source_clause(self) -> None:
        """Obligations with similar source_clause are paired together."""
        primary = [SAMPLE_OBLIGATION]
        independent = [SAMPLE_OBLIGATION_INDEPENDENT]

        pairs = match_extractions(primary, independent)

        assert len(pairs) == 1
        p_obl, ind_obl = pairs[0]
        assert p_obl == SAMPLE_OBLIGATION
        assert ind_obl == SAMPLE_OBLIGATION_INDEPENDENT

    def test_unmatched_primary_becomes_solo(self) -> None:
        """Primary obligation with no match produces (primary, None)."""
        unique_obligation = Obligation(
            obligation_text="Vendor must provide training.",
            obligation_type="Delivery",
            responsible_party="Vendor",
            counterparty="Client",
            frequency=None,
            deadline=None,
            source_clause="The Vendor shall provide on-site training sessions.",
            source_page=2,
            confidence=0.80,
        )

        pairs = match_extractions([unique_obligation], [])

        assert len(pairs) == 1
        assert pairs[0] == (unique_obligation, None)

    def test_unmatched_independent_appended(self) -> None:
        """Independent-only obligations are appended as SOLO entries."""
        unique_ind = Obligation(
            obligation_text="Client must provide access to facilities.",
            obligation_type="Delivery",
            responsible_party="Client",
            counterparty="Vendor",
            frequency=None,
            deadline=None,
            source_clause="The Client shall provide access to all designated facilities.",
            source_page=3,
            confidence=0.85,
        )

        pairs = match_extractions([], [unique_ind])

        assert len(pairs) == 1
        assert pairs[0] == (unique_ind, None)

    def test_mixed_matched_and_solo(self) -> None:
        """Mix of matched and unmatched obligations."""
        solo_primary = Obligation(
            obligation_text="Vendor must provide training.",
            obligation_type="Delivery",
            responsible_party="Vendor",
            counterparty="Client",
            frequency=None,
            deadline=None,
            source_clause="The Vendor shall provide on-site training sessions.",
            source_page=2,
            confidence=0.80,
        )

        pairs = match_extractions(
            [SAMPLE_OBLIGATION, solo_primary],
            [SAMPLE_OBLIGATION_INDEPENDENT],
        )

        assert len(pairs) == 2
        # First pair: matched
        assert pairs[0][1] == SAMPLE_OBLIGATION_INDEPENDENT
        # Second pair: solo primary
        assert pairs[1] == (solo_primary, None)


class TestCheckAgreement:
    """Tests for check_agreement()."""

    def test_agreement_when_matching(self) -> None:
        """Two obligations with same type, party, and similar text agree."""
        assert check_agreement(SAMPLE_OBLIGATION, SAMPLE_OBLIGATION_INDEPENDENT) is True

    def test_disagreement_on_type(self) -> None:
        """Different obligation_type causes disagreement."""
        different_type = SAMPLE_OBLIGATION_INDEPENDENT.model_copy(
            update={"obligation_type": "Financial"}
        )
        assert check_agreement(SAMPLE_OBLIGATION, different_type) is False

    def test_disagreement_on_party(self) -> None:
        """Different responsible_party causes disagreement."""
        different_party = SAMPLE_OBLIGATION_INDEPENDENT.model_copy(
            update={"responsible_party": "Client"}
        )
        assert check_agreement(SAMPLE_OBLIGATION, different_party) is False

    def test_disagreement_on_text(self) -> None:
        """Vastly different obligation_text causes disagreement."""
        different_text = SAMPLE_OBLIGATION_INDEPENDENT.model_copy(
            update={"obligation_text": "Something completely unrelated to anything."}
        )
        assert check_agreement(SAMPLE_OBLIGATION, different_text) is False


class TestCoVe:
    """Tests for run_cove() Chain-of-Verification."""

    def test_cove_passes_when_all_found(self) -> None:
        """CoVe passes when all answers are found in the document."""
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

    def test_cove_fails_when_not_found(self) -> None:
        """CoVe fails when answers contain NOT FOUND."""
        from echelonos.stages.stage_3_extraction import (
            _CoVeAnswersResponse,
            _CoVeQuestionsResponse,
        )

        with _patch_structured_side_effect([
            _CoVeQuestionsResponse(questions=["Q1?", "Q2?"]),
            _CoVeAnswersResponse(answers=["Answer 1", "NOT FOUND"]),
        ]):
            result = run_cove(
                SAMPLE_LOW_CONFIDENCE_OBLIGATION,
                SAMPLE_CONTRACT_TEXT,
                claude_client=MagicMock(),
            )

        assert result["cove_passed"] is False


class TestFullPipeline:
    """End-to-end pipeline tests for extract_and_verify()."""

    def test_full_pipeline_agreed_verified(self) -> None:
        """Both extractions agree and grounding passes -> VERIFIED."""
        from echelonos.stages.stage_3_extraction import (
            _ExtractionResponse,
            _PartyRolesResponse,
        )

        with _patch_structured_side_effect([
            # Call 1: party roles
            _PartyRolesResponse(party_roles=SAMPLE_PARTY_ROLES),
            # Call 2: primary extraction
            _ExtractionResponse(obligations=[SAMPLE_OBLIGATION]),
            # Call 3: independent extraction (agrees)
            _ExtractionResponse(obligations=[SAMPLE_OBLIGATION_INDEPENDENT]),
            # No CoVe calls -- AGREED path
        ]):
            results = extract_and_verify(
                SAMPLE_CONTRACT_TEXT,
                claude_client=MagicMock(),
            )

        assert len(results) == 1
        entry = results[0]

        # Obligation data is present.
        assert entry["obligation"]["obligation_type"] == "Delivery"
        assert entry["obligation"]["responsible_party"] == "Vendor"

        # Grounding passes (source_clause is in the contract text).
        assert entry["grounding"] is True

        # Ensemble shows agreement.
        assert entry["ensemble"]["agreement"] == "AGREED"
        assert entry["ensemble"]["primary_extraction"] is not None
        assert entry["ensemble"]["independent_extraction"] is not None

        # No CoVe needed (AGREED).
        assert entry["cove"] is None

        # Final status.
        assert entry["status"] == "VERIFIED"

    def test_unverified_disagreement(self) -> None:
        """Extractions disagree + CoVe fails -> UNVERIFIED."""
        from echelonos.stages.stage_3_extraction import (
            _CoVeAnswersResponse,
            _CoVeQuestionsResponse,
            _ExtractionResponse,
            _PartyRolesResponse,
        )

        # Independent extraction has different obligation_type.
        disagreeing_obligation = SAMPLE_OBLIGATION_INDEPENDENT.model_copy(
            update={"obligation_type": "Financial"}
        )

        with _patch_structured_side_effect([
            # Call 1: party roles
            _PartyRolesResponse(party_roles=SAMPLE_PARTY_ROLES),
            # Call 2: primary extraction
            _ExtractionResponse(obligations=[SAMPLE_OBLIGATION]),
            # Call 3: independent extraction (disagrees on type)
            _ExtractionResponse(obligations=[disagreeing_obligation]),
            # Call 4: CoVe questions (triggered by DISAGREED)
            _CoVeQuestionsResponse(questions=["Is the type correct?"]),
            # Call 5: CoVe answers (NOT FOUND -> fails)
            _CoVeAnswersResponse(answers=["NOT FOUND"]),
        ]):
            results = extract_and_verify(
                SAMPLE_CONTRACT_TEXT,
                claude_client=MagicMock(),
            )

        assert len(results) == 1
        entry = results[0]

        # Ensemble shows disagreement.
        assert entry["ensemble"]["agreement"] == "DISAGREED"

        # CoVe was triggered and failed.
        assert entry["cove"] is not None
        assert entry["cove"]["cove_passed"] is False

        # Final status.
        assert entry["status"] == "UNVERIFIED"

    def test_pipeline_with_cove_triggered_passes(self) -> None:
        """SOLO obligation with successful CoVe -> VERIFIED."""
        from echelonos.stages.stage_3_extraction import (
            _CoVeAnswersResponse,
            _CoVeQuestionsResponse,
            _ExtractionResponse,
            _PartyRolesResponse,
        )

        with _patch_structured_side_effect([
            # Call 1: party roles
            _PartyRolesResponse(party_roles=SAMPLE_PARTY_ROLES),
            # Call 2: primary extraction (one obligation)
            _ExtractionResponse(obligations=[SAMPLE_OBLIGATION]),
            # Call 3: independent extraction (empty -- no match)
            _ExtractionResponse(obligations=[]),
            # Call 4: CoVe questions (triggered by SOLO)
            _CoVeQuestionsResponse(
                questions=[
                    "What must the Vendor deliver?",
                    "What is the delivery deadline?",
                ]
            ),
            # Call 5: CoVe answers (all found -> passes)
            _CoVeAnswersResponse(
                answers=[
                    "All hardware components",
                    "30 calendar days of the purchase order date",
                ]
            ),
        ]):
            results = extract_and_verify(
                SAMPLE_CONTRACT_TEXT,
                claude_client=MagicMock(),
            )

        assert len(results) == 1
        entry = results[0]

        # Grounding passes.
        assert entry["grounding"] is True

        # Ensemble shows SOLO.
        assert entry["ensemble"]["agreement"] == "SOLO"
        assert entry["ensemble"]["independent_extraction"] is None

        # CoVe was triggered and passed.
        assert entry["cove"] is not None
        assert entry["cove"]["cove_passed"] is True

        # All checks passed.
        assert entry["status"] == "VERIFIED"

    def test_agreed_but_ungrounded(self) -> None:
        """Both extractions agree but grounding fails -> UNVERIFIED."""
        from echelonos.stages.stage_3_extraction import (
            _ExtractionResponse,
            _PartyRolesResponse,
        )

        # Both extractions cite a clause NOT in the document.
        bad_clause = "The Vendor shall deliver within 7 business days."
        bad_primary = SAMPLE_OBLIGATION.model_copy(
            update={"source_clause": bad_clause}
        )
        bad_independent = SAMPLE_OBLIGATION_INDEPENDENT.model_copy(
            update={"source_clause": bad_clause}
        )

        with _patch_structured_side_effect([
            _PartyRolesResponse(party_roles=SAMPLE_PARTY_ROLES),
            _ExtractionResponse(obligations=[bad_primary]),
            _ExtractionResponse(obligations=[bad_independent]),
        ]):
            results = extract_and_verify(
                SAMPLE_CONTRACT_TEXT,
                claude_client=MagicMock(),
            )

        assert len(results) == 1
        entry = results[0]

        assert entry["ensemble"]["agreement"] == "AGREED"
        assert entry["grounding"] is False
        assert entry["status"] == "UNVERIFIED"
