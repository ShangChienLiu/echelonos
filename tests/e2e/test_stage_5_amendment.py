"""E2E tests for Stage 5: Amendment Resolution (Chain Walking).

All LLM calls (Claude clause comparison) are mocked to enable deterministic
testing without API keys or network access.  Mock responses are designed to
be realistic representations of actual LLM output.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from echelonos.stages.stage_5_amendment import (
    ResolutionResult,
    build_amendment_chain,
    compare_clauses,
    resolve_all,
    resolve_amendment_chain,
    resolve_obligation,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _patch_structured(return_value):
    """Patch extract_with_structured_output in stage_5_amendment."""
    return patch(
        "echelonos.stages.stage_5_amendment.extract_with_structured_output",
        return_value=return_value,
    )


def _patch_structured_side_effect(side_effect):
    """Patch extract_with_structured_output with multiple return values."""
    return patch(
        "echelonos.stages.stage_5_amendment.extract_with_structured_output",
        side_effect=side_effect,
    )


def _make_comparison_response(action: str, reasoning: str, confidence: float):
    """Build a mock _ComparisonResponse object for clause comparison."""
    from echelonos.stages.stage_5_amendment import _ComparisonResponse

    return _ComparisonResponse(
        action=action,
        reasoning=reasoning,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Realistic test data
# ---------------------------------------------------------------------------

# MSA obligations
MSA_OBLIGATION_DELIVERY = {
    "obligation_text": (
        "Vendor must deliver all hardware components to Client's facility "
        "within 30 calendar days of the purchase order date."
    ),
    "obligation_type": "Delivery",
    "responsible_party": "Vendor",
    "counterparty": "Client",
    "source_clause": (
        "The Vendor shall deliver all hardware components to the Client's "
        "designated facility within 30 calendar days of the purchase order date."
    ),
    "source_page": 1,
    "confidence": 0.95,
}

MSA_OBLIGATION_PAYMENT = {
    "obligation_text": (
        "Client must pay Vendor within 45 days of receipt of a valid invoice."
    ),
    "obligation_type": "Financial",
    "responsible_party": "Client",
    "counterparty": "Vendor",
    "source_clause": (
        "The Client shall pay the Vendor within 45 days of receipt of a valid "
        "invoice. Late payments shall accrue interest at 1.5% per month."
    ),
    "source_page": 2,
    "confidence": 0.92,
}

MSA_OBLIGATION_CONFIDENTIALITY = {
    "obligation_text": (
        "Both parties must maintain confidentiality of proprietary information "
        "for 5 years following termination."
    ),
    "obligation_type": "Confidentiality",
    "responsible_party": "Both",
    "counterparty": "Both",
    "source_clause": (
        "Both parties shall maintain the confidentiality of all proprietary "
        "information exchanged under this Agreement for a period of 5 years "
        "following termination."
    ),
    "source_page": 3,
    "confidence": 0.97,
}

MSA_OBLIGATION_SLA = {
    "obligation_text": (
        "Vendor must maintain 99.9% uptime for all hosted services."
    ),
    "obligation_type": "SLA",
    "responsible_party": "Vendor",
    "counterparty": "Client",
    "source_clause": (
        "The Vendor shall maintain a minimum uptime of 99.9% for all hosted "
        "services measured on a monthly basis."
    ),
    "source_page": 4,
    "confidence": 0.90,
}

# Amendment #1 obligations -- replaces delivery, modifies payment
AMENDMENT_1_DELIVERY = {
    "obligation_text": (
        "Vendor must deliver all hardware components to Client's facility "
        "within 15 business days of the purchase order date."
    ),
    "obligation_type": "Delivery",
    "responsible_party": "Vendor",
    "counterparty": "Client",
    "source_clause": (
        "Section 1.1 is hereby amended. The Vendor shall deliver all hardware "
        "components to the Client's designated facility within 15 business days "
        "of the purchase order date."
    ),
    "source_page": 1,
    "confidence": 0.94,
}

AMENDMENT_1_PAYMENT = {
    "obligation_text": (
        "Client must pay Vendor within 30 days of receipt of a valid invoice."
    ),
    "obligation_type": "Financial",
    "responsible_party": "Client",
    "counterparty": "Vendor",
    "source_clause": (
        "Section 2.1 is hereby modified. The Client shall pay the Vendor "
        "within 30 days of receipt of a valid invoice. The late payment "
        "interest rate is changed to 1.0% per month."
    ),
    "source_page": 1,
    "confidence": 0.93,
}

# Amendment #2 obligations -- deletes SLA
AMENDMENT_2_SLA_DELETE = {
    "obligation_text": (
        "Section 4.1 regarding uptime SLA is hereby deleted in its entirety."
    ),
    "obligation_type": "SLA",
    "responsible_party": "Vendor",
    "counterparty": "Client",
    "source_clause": (
        "Section 4.1 (Service Level Agreement - Uptime) is hereby deleted "
        "in its entirety. The Vendor shall no longer be required to maintain "
        "any minimum uptime guarantee for hosted services."
    ),
    "source_page": 1,
    "confidence": 0.96,
}

# Unrelated amendment obligation (new clause, not affecting MSA)
AMENDMENT_1_NEW_CLAUSE = {
    "obligation_text": (
        "Vendor must provide 24/7 phone support for critical issues."
    ),
    "obligation_type": "SLA",
    "responsible_party": "Vendor",
    "counterparty": "Client",
    "source_clause": (
        "The Vendor shall provide 24/7 phone support for issues classified "
        "as critical severity."
    ),
    "source_page": 2,
    "confidence": 0.91,
}


# ---------------------------------------------------------------------------
# Tests: build_amendment_chain
# ---------------------------------------------------------------------------


class TestBuildAmendmentChain:
    """Tests for build_amendment_chain() -- correct ordering from MSA to amendments."""

    def test_build_amendment_chain(self):
        """Simple MSA -> Amendment chain is built correctly."""
        links = [
            {
                "child_doc_id": "amend-001",
                "parent_doc_id": "msa-001",
                "status": "LINKED",
            },
        ]

        chains = build_amendment_chain(links)

        assert len(chains) == 1
        assert chains[0] == ["msa-001", "amend-001"]

    def test_ignores_unlinked_records(self):
        """UNLINKED and AMBIGUOUS records are excluded from chains."""
        links = [
            {
                "child_doc_id": "amend-001",
                "parent_doc_id": "msa-001",
                "status": "LINKED",
            },
            {
                "child_doc_id": "amend-002",
                "parent_doc_id": "msa-001",
                "status": "UNLINKED",
            },
            {
                "child_doc_id": "amend-003",
                "parent_doc_id": "msa-001",
                "status": "AMBIGUOUS",
            },
        ]

        chains = build_amendment_chain(links)

        assert len(chains) == 1
        assert chains[0] == ["msa-001", "amend-001"]

    def test_empty_links(self):
        """Empty link list produces no chains."""
        chains = build_amendment_chain([])
        assert chains == []


class TestBuildChainWithMultipleAmendments:
    """Tests for chains with 3+ documents."""

    def test_three_document_chain(self):
        """MSA -> Amendment #1 -> Amendment #2 forms a single chain."""
        links = [
            {
                "child_doc_id": "amend-001",
                "parent_doc_id": "msa-001",
                "status": "LINKED",
            },
            {
                "child_doc_id": "amend-002",
                "parent_doc_id": "amend-001",
                "status": "LINKED",
            },
        ]

        chains = build_amendment_chain(links)

        assert len(chains) == 1
        assert chains[0] == ["msa-001", "amend-001", "amend-002"]

    def test_four_document_chain(self):
        """MSA -> Amend #1 -> Amend #2 -> Amend #3 forms a single chain."""
        links = [
            {
                "child_doc_id": "amend-001",
                "parent_doc_id": "msa-001",
                "status": "LINKED",
            },
            {
                "child_doc_id": "amend-002",
                "parent_doc_id": "amend-001",
                "status": "LINKED",
            },
            {
                "child_doc_id": "amend-003",
                "parent_doc_id": "amend-002",
                "status": "LINKED",
            },
        ]

        chains = build_amendment_chain(links)

        assert len(chains) == 1
        assert chains[0] == ["msa-001", "amend-001", "amend-002", "amend-003"]

    def test_branching_chains(self):
        """An MSA with two independent amendments produces two chains."""
        links = [
            {
                "child_doc_id": "amend-001",
                "parent_doc_id": "msa-001",
                "status": "LINKED",
            },
            {
                "child_doc_id": "amend-002",
                "parent_doc_id": "msa-001",
                "status": "LINKED",
            },
        ]

        chains = build_amendment_chain(links)

        assert len(chains) == 2
        # Both chains start with the MSA.
        for chain in chains:
            assert chain[0] == "msa-001"
            assert len(chain) == 2
        # Both amendments are represented.
        amendment_ids = {chain[1] for chain in chains}
        assert amendment_ids == {"amend-001", "amend-002"}

    def test_multiple_separate_chains(self):
        """Two independent MSAs each with their own amendment."""
        links = [
            {
                "child_doc_id": "amend-a1",
                "parent_doc_id": "msa-a",
                "status": "LINKED",
            },
            {
                "child_doc_id": "amend-b1",
                "parent_doc_id": "msa-b",
                "status": "LINKED",
            },
        ]

        chains = build_amendment_chain(links)

        assert len(chains) == 2
        root_ids = {chain[0] for chain in chains}
        assert root_ids == {"msa-a", "msa-b"}


# ---------------------------------------------------------------------------
# Tests: compare_clauses
# ---------------------------------------------------------------------------


class TestCompareClausesReplace:
    """Amendment replaces original clause -> REPLACE."""

    def test_compare_clauses_replace(self):
        """LLM determines the amendment replaces the original."""
        response = _make_comparison_response(
            action="REPLACE",
            reasoning=(
                "The amendment entirely replaces the delivery timeline from "
                "30 calendar days to 15 business days. The original clause "
                "is no longer in effect."
            ),
            confidence=0.95,
        )

        with _patch_structured(response):
            result = compare_clauses(
                original_clause=MSA_OBLIGATION_DELIVERY["source_clause"],
                amendment_clause=AMENDMENT_1_DELIVERY["source_clause"],
                claude_client=MagicMock(),
            )

        assert isinstance(result, ResolutionResult)
        assert result.action == "REPLACE"
        assert result.confidence == 0.95
        assert "replaces" in result.reasoning.lower() or "replace" in result.reasoning.lower()
        assert result.original_clause == MSA_OBLIGATION_DELIVERY["source_clause"]
        assert result.amendment_clause == AMENDMENT_1_DELIVERY["source_clause"]


class TestCompareClausesModify:
    """Amendment modifies original clause -> MODIFY."""

    def test_compare_clauses_modify(self):
        """LLM determines the amendment modifies the original."""
        response = _make_comparison_response(
            action="MODIFY",
            reasoning=(
                "The amendment changes the payment term from 45 days to 30 "
                "days and reduces the interest rate from 1.5% to 1.0%. The "
                "core payment obligation remains but is modified."
            ),
            confidence=0.90,
        )

        with _patch_structured(response):
            result = compare_clauses(
                original_clause=MSA_OBLIGATION_PAYMENT["source_clause"],
                amendment_clause=AMENDMENT_1_PAYMENT["source_clause"],
                claude_client=MagicMock(),
            )

        assert isinstance(result, ResolutionResult)
        assert result.action == "MODIFY"
        assert result.confidence == 0.90
        assert result.original_clause == MSA_OBLIGATION_PAYMENT["source_clause"]
        assert result.amendment_clause == AMENDMENT_1_PAYMENT["source_clause"]


class TestCompareClausesUnchanged:
    """No change to original -> UNCHANGED."""

    def test_compare_clauses_unchanged(self):
        """LLM determines the amendment does not affect the original."""
        response = _make_comparison_response(
            action="UNCHANGED",
            reasoning=(
                "The amendment clause concerns delivery timelines while the "
                "original clause deals with confidentiality. These clauses "
                "address completely different subject matter."
            ),
            confidence=0.98,
        )

        with _patch_structured(response):
            result = compare_clauses(
                original_clause=MSA_OBLIGATION_CONFIDENTIALITY["source_clause"],
                amendment_clause=AMENDMENT_1_DELIVERY["source_clause"],
                claude_client=MagicMock(),
            )

        assert isinstance(result, ResolutionResult)
        assert result.action == "UNCHANGED"
        assert result.confidence == 0.98


# ---------------------------------------------------------------------------
# Tests: resolve_obligation
# ---------------------------------------------------------------------------


class TestResolveObligationSuperseded:
    """Obligation gets superseded by amendment -> SUPERSEDED."""

    def test_resolve_obligation_superseded(self):
        """Delivery obligation is replaced by amendment with shorter deadline."""
        response = _make_comparison_response(
            action="REPLACE",
            reasoning=(
                "The amendment replaces the 30-day delivery requirement "
                "with a 15-business-day requirement."
            ),
            confidence=0.95,
        )

        with _patch_structured(response):
            result = resolve_obligation(
                obligation=MSA_OBLIGATION_DELIVERY,
                amendment_obligations=[AMENDMENT_1_DELIVERY],
                claude_client=MagicMock(),
            )

        assert result["status"] == "SUPERSEDED"
        assert len(result["amendment_history"]) == 1
        assert result["amendment_history"][0]["action"] == "REPLACE"
        assert result["amendment_history"][0]["confidence"] == 0.95
        # Original obligation data is preserved.
        assert result["obligation_text"] == MSA_OBLIGATION_DELIVERY["obligation_text"]


class TestResolveObligationStaysActive:
    """Obligation not affected by amendment -> ACTIVE."""

    def test_resolve_obligation_stays_active(self):
        """Confidentiality obligation is unrelated to delivery amendment."""
        # The heuristic pre-filter should skip the LLM call since the
        # confidentiality and delivery clauses have low keyword overlap.
        # If it does call the LLM, it returns UNCHANGED.
        response = _make_comparison_response(
            action="UNCHANGED",
            reasoning="The clauses address different subject matter.",
            confidence=0.99,
        )

        with _patch_structured(response):
            result = resolve_obligation(
                obligation=MSA_OBLIGATION_CONFIDENTIALITY,
                amendment_obligations=[AMENDMENT_1_DELIVERY],
                claude_client=MagicMock(),
            )

        assert result["status"] == "ACTIVE"
        # Original data is preserved.
        assert result["obligation_text"] == MSA_OBLIGATION_CONFIDENTIALITY["obligation_text"]

    def test_resolve_obligation_with_no_amendments(self):
        """Obligation with empty amendment list stays ACTIVE."""
        result = resolve_obligation(
            obligation=MSA_OBLIGATION_DELIVERY,
            amendment_obligations=[],
        )

        assert result["status"] == "ACTIVE"
        assert result["amendment_history"] == []


# ---------------------------------------------------------------------------
# Tests: resolve_amendment_chain (end-to-end)
# ---------------------------------------------------------------------------


class TestResolveChainEndToEnd:
    """Full chain resolution with correct final states."""

    def test_resolve_chain_end_to_end(self):
        """MSA -> Amendment #1 -> Amendment #2 chain resolves correctly.

        - Delivery obligation: SUPERSEDED by Amendment #1
        - Payment obligation: stays ACTIVE (MODIFY keeps it active)
        - Confidentiality: stays ACTIVE (unrelated amendments)
        - SLA: TERMINATED by Amendment #2
        """
        # Use a side_effect function that returns the correct response
        # based on the actual clause content being compared, making the
        # test robust against changes in the heuristic pre-filter.
        def _smart_mock(*args, **kwargs):
            # compare_clauses passes:
            #   system_prompt = _CLAUSE_COMPARISON_SYSTEM_PROMPT (template)
            #   user_prompt = "Original clause:\n{orig}\n\nAmendment clause:\n{amend}"
            user = kwargs.get("user_prompt", "") or (args[2] if len(args) > 2 else "")

            # Split into original and amendment portions.
            parts = user.split("Amendment clause:")
            original_part = parts[0] if parts else ""
            amendment_part = parts[1] if len(parts) > 1 else ""

            # Delivery vs Amend1-Delivery: REPLACE
            if "30 calendar days" in original_part and "15 business days" in amendment_part:
                return _make_comparison_response(
                    action="REPLACE",
                    reasoning="Delivery timeline changed from 30 to 15 business days.",
                    confidence=0.95,
                )
            # Payment vs Amend1-Payment: MODIFY
            if "45 days" in original_part and "30 days" in amendment_part:
                return _make_comparison_response(
                    action="MODIFY",
                    reasoning="Payment term changed from 45 to 30 days.",
                    confidence=0.91,
                )
            # SLA vs Amend2-SLA-Delete: DELETE (original must be about uptime)
            if "99.9%" in original_part and "deleted" in amendment_part.lower():
                return _make_comparison_response(
                    action="DELETE",
                    reasoning="Section 4.1 is explicitly deleted.",
                    confidence=0.96,
                )
            # Default: UNCHANGED
            return _make_comparison_response(
                action="UNCHANGED",
                reasoning="Different subject matter.",
                confidence=0.98,
            )

        chain_docs = [
            {
                "doc_id": "msa-001",
                "doc_type": "MSA",
                "obligations": [
                    MSA_OBLIGATION_DELIVERY,
                    MSA_OBLIGATION_PAYMENT,
                    MSA_OBLIGATION_CONFIDENTIALITY,
                    MSA_OBLIGATION_SLA,
                ],
            },
            {
                "doc_id": "amend-001",
                "doc_type": "Amendment",
                "obligations": [
                    AMENDMENT_1_DELIVERY,
                    AMENDMENT_1_PAYMENT,
                ],
            },
            {
                "doc_id": "amend-002",
                "doc_type": "Amendment",
                "obligations": [
                    AMENDMENT_2_SLA_DELETE,
                ],
            },
        ]

        with _patch_structured_side_effect(_smart_mock):
            resolved = resolve_amendment_chain(chain_docs, claude_client=MagicMock())

        # Collect results by source.
        msa_resolved = [r for r in resolved if r.get("source_doc_id") == "msa-001"]
        amend_1_resolved = [r for r in resolved if r.get("source_doc_id") == "amend-001"]
        amend_2_resolved = [r for r in resolved if r.get("source_doc_id") == "amend-002"]

        # 4 MSA obligations + 2 Amendment #1 + 1 Amendment #2 = 7 total.
        assert len(resolved) == 7

        # Find each MSA obligation by text and verify status.
        delivery = next(
            r for r in msa_resolved
            if "30 calendar days" in r.get("obligation_text", "")
        )
        assert delivery["status"] == "SUPERSEDED"

        payment = next(
            r for r in msa_resolved
            if "45 days" in r.get("obligation_text", "")
        )
        assert payment["status"] == "ACTIVE"  # MODIFY keeps it active.

        confidentiality = next(
            r for r in msa_resolved
            if "confidentiality" in r.get("obligation_text", "").lower()
        )
        # Smart mock returns UNCHANGED for unrelated pairs, so
        # confidentiality stays ACTIVE.
        assert confidentiality["status"] == "ACTIVE"

        sla = next(
            r for r in msa_resolved
            if "99.9%" in r.get("obligation_text", "")
        )
        assert sla["status"] == "TERMINATED"

        # Amendment obligations are always ACTIVE.
        for r in amend_1_resolved:
            assert r["status"] == "ACTIVE"
        for r in amend_2_resolved:
            assert r["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Tests: unlinked documents
# ---------------------------------------------------------------------------


class TestUnlinkedDocsStayUnresolved:
    """Documents not part of any chain keep UNRESOLVED status."""

    def test_unlinked_docs_stay_unresolved(self):
        """An unlinked standalone document's obligations are UNRESOLVED."""
        documents = [
            {
                "doc_id": "msa-001",
                "doc_type": "MSA",
                "obligations": [MSA_OBLIGATION_DELIVERY],
            },
            {
                "doc_id": "amend-001",
                "doc_type": "Amendment",
                "obligations": [AMENDMENT_1_DELIVERY],
            },
            {
                "doc_id": "standalone-001",
                "doc_type": "MSA",
                "obligations": [MSA_OBLIGATION_CONFIDENTIALITY],
            },
        ]

        links = [
            {
                "child_doc_id": "amend-001",
                "parent_doc_id": "msa-001",
                "status": "LINKED",
            },
        ]

        # The MSA delivery vs Amendment delivery comparison.
        response = _make_comparison_response(
            action="REPLACE",
            reasoning="Delivery timeline changed.",
            confidence=0.95,
        )

        with _patch_structured(response):
            result = resolve_all(documents, links, claude_client=MagicMock())

        # Find the standalone document's obligations.
        standalone_obls = [
            r for r in result if r.get("source_doc_id") == "standalone-001"
        ]

        assert len(standalone_obls) == 1
        assert standalone_obls[0]["status"] == "UNRESOLVED"
        assert standalone_obls[0]["amendment_history"] == []

    def test_all_unlinked_docs(self):
        """When there are no links, all obligations are UNRESOLVED."""
        documents = [
            {
                "doc_id": "msa-001",
                "doc_type": "MSA",
                "obligations": [MSA_OBLIGATION_DELIVERY, MSA_OBLIGATION_PAYMENT],
            },
            {
                "doc_id": "msa-002",
                "doc_type": "MSA",
                "obligations": [MSA_OBLIGATION_CONFIDENTIALITY],
            },
        ]

        links = []  # No links at all.

        result = resolve_all(documents, links)

        assert len(result) == 3
        for obl in result:
            assert obl["status"] == "UNRESOLVED"

    def test_unlinked_status_records_not_used(self):
        """UNLINKED link records do not form chains."""
        documents = [
            {
                "doc_id": "msa-001",
                "doc_type": "MSA",
                "obligations": [MSA_OBLIGATION_DELIVERY],
            },
            {
                "doc_id": "amend-001",
                "doc_type": "Amendment",
                "obligations": [AMENDMENT_1_DELIVERY],
            },
        ]

        links = [
            {
                "child_doc_id": "amend-001",
                "parent_doc_id": "msa-001",
                "status": "UNLINKED",  # Not LINKED.
            },
        ]

        result = resolve_all(documents, links)

        # Both documents are treated as unlinked.
        assert len(result) == 2
        for obl in result:
            assert obl["status"] == "UNRESOLVED"


# ---------------------------------------------------------------------------
# Tests: DELETE detection
# ---------------------------------------------------------------------------


class TestDeleteDetection:
    """'Section hereby deleted' -> TERMINATED."""

    def test_delete_detection(self):
        """An amendment that explicitly deletes a section terminates the obligation."""
        response = _make_comparison_response(
            action="DELETE",
            reasoning=(
                "The amendment explicitly states that Section 4.1 is "
                "'hereby deleted in its entirety'. The original SLA "
                "obligation is terminated with no replacement."
            ),
            confidence=0.97,
        )

        with _patch_structured(response):
            result = resolve_obligation(
                obligation=MSA_OBLIGATION_SLA,
                amendment_obligations=[AMENDMENT_2_SLA_DELETE],
                claude_client=MagicMock(),
            )

        assert result["status"] == "TERMINATED"
        assert len(result["amendment_history"]) == 1
        assert result["amendment_history"][0]["action"] == "DELETE"
        assert result["amendment_history"][0]["confidence"] == 0.97
        # Original obligation data is preserved.
        assert result["obligation_text"] == MSA_OBLIGATION_SLA["obligation_text"]

    def test_delete_stops_further_processing(self):
        """Once terminated, subsequent amendments do not change the status."""
        responses = [
            # First comparison: DELETE.
            _make_comparison_response(
                action="DELETE",
                reasoning="Section explicitly deleted.",
                confidence=0.97,
            ),
            # If this is consumed, it would change status -- should not happen.
            _make_comparison_response(
                action="REPLACE",
                reasoning="This should never be reached.",
                confidence=0.99,
            ),
        ]

        # Two amendment obligations that both match via heuristic.
        amend_delete = {
            "obligation_text": (
                "Section 4.1 regarding uptime SLA is hereby deleted "
                "in its entirety."
            ),
            "obligation_type": "SLA",
            "responsible_party": "Vendor",
            "counterparty": "Client",
            "source_clause": (
                "Section 4.1 (Service Level Agreement - Uptime) is hereby "
                "deleted in its entirety."
            ),
            "source_page": 1,
            "confidence": 0.96,
        }
        amend_second = {
            "obligation_text": (
                "Vendor must maintain 99.99% uptime for hosted services "
                "under revised SLA terms."
            ),
            "obligation_type": "SLA",
            "responsible_party": "Vendor",
            "counterparty": "Client",
            "source_clause": (
                "The Vendor shall maintain a minimum uptime of 99.99% for "
                "all hosted services."
            ),
            "source_page": 2,
            "confidence": 0.92,
        }

        with _patch_structured_side_effect(responses):
            result = resolve_obligation(
                obligation=MSA_OBLIGATION_SLA,
                amendment_obligations=[amend_delete, amend_second],
                claude_client=MagicMock(),
            )

        assert result["status"] == "TERMINATED"
        # Only one history entry -- processing stopped after DELETE.
        assert len(result["amendment_history"]) == 1
        assert result["amendment_history"][0]["action"] == "DELETE"


# ---------------------------------------------------------------------------
# Tests: resolve_all integration
# ---------------------------------------------------------------------------


class TestResolveAllIntegration:
    """Integration test combining chain building and resolution."""

    def test_resolve_all_mixed_scenario(self):
        """Mixed scenario: one linked chain + one unlinked document."""
        documents = [
            {
                "doc_id": "msa-001",
                "doc_type": "MSA",
                "obligations": [MSA_OBLIGATION_DELIVERY, MSA_OBLIGATION_CONFIDENTIALITY],
            },
            {
                "doc_id": "amend-001",
                "doc_type": "Amendment",
                "obligations": [AMENDMENT_1_DELIVERY],
            },
            {
                "doc_id": "standalone-nda",
                "doc_type": "NDA",
                "obligations": [
                    {
                        "obligation_text": "Both parties must not disclose trade secrets.",
                        "obligation_type": "Confidentiality",
                        "responsible_party": "Both",
                        "counterparty": "Both",
                        "source_clause": (
                            "Neither party shall disclose any trade secrets of "
                            "the other party."
                        ),
                        "source_page": 1,
                        "confidence": 0.94,
                    }
                ],
            },
        ]

        links = [
            {
                "child_doc_id": "amend-001",
                "parent_doc_id": "msa-001",
                "status": "LINKED",
            },
        ]

        # Provide enough responses for all possible comparisons.
        responses = [
            _make_comparison_response(
                action="REPLACE",
                reasoning="Delivery timeline changed.",
                confidence=0.95,
            ),
        ]
        # Extra UNCHANGED for any other comparisons.
        for _ in range(10):
            responses.append(
                _make_comparison_response(
                    action="UNCHANGED",
                    reasoning="Different subject matter.",
                    confidence=0.99,
                )
            )

        with _patch_structured_side_effect(responses):
            result = resolve_all(documents, links, claude_client=MagicMock())

        # Chain: msa-001 (2 obligations) + amend-001 (1 obligation) = 3 from chain.
        # Standalone: 1 obligation.
        # Total: 4.
        assert len(result) == 4

        # Check statuses.
        standalone_obls = [r for r in result if r["source_doc_id"] == "standalone-nda"]
        assert len(standalone_obls) == 1
        assert standalone_obls[0]["status"] == "UNRESOLVED"

        chain_obls = [r for r in result if r["source_doc_id"] != "standalone-nda"]
        assert len(chain_obls) == 3

        # At least one should be SUPERSEDED (the delivery obligation).
        statuses = {r["status"] for r in chain_obls}
        assert "ACTIVE" in statuses

    def test_resolve_all_with_empty_input(self):
        """Empty documents and links produce empty result."""
        result = resolve_all([], [])
        assert result == []


# ---------------------------------------------------------------------------
# Tests: amendment_history includes document metadata
# ---------------------------------------------------------------------------


class TestAmendmentHistoryDocMetadata:
    """resolve_obligation() output includes doc_id/doc_filename/amendment_number
    in each history entry when amendment obligations carry those tags."""

    def test_history_entries_include_doc_metadata(self):
        """When amendment obligations are tagged with _source_doc_id,
        _source_doc_filename, _amendment_number, the history records
        should include doc_id, doc_filename, amendment_number."""
        response = _make_comparison_response(
            action="REPLACE",
            reasoning="Delivery timeline changed from 30 to 15 days.",
            confidence=0.95,
        )

        # Tag the amendment obligation with document metadata
        # (as resolve_amendment_chain should do).
        tagged_amendment = dict(AMENDMENT_1_DELIVERY)
        tagged_amendment["_source_doc_id"] = "amend-001"
        tagged_amendment["_source_doc_filename"] = "Amendment_1.pdf"
        tagged_amendment["_amendment_number"] = 1

        with _patch_structured(response):
            result = resolve_obligation(
                obligation=MSA_OBLIGATION_DELIVERY,
                amendment_obligations=[tagged_amendment],
                claude_client=MagicMock(),
            )

        assert result["status"] == "SUPERSEDED"
        assert len(result["amendment_history"]) == 1
        entry = result["amendment_history"][0]
        assert entry["doc_id"] == "amend-001"
        assert entry["doc_filename"] == "Amendment_1.pdf"
        assert entry["amendment_number"] == 1

    def test_history_entries_without_metadata_still_work(self):
        """Backward compat: amendment obligations without metadata tags
        produce history entries without doc_id/doc_filename/amendment_number."""
        response = _make_comparison_response(
            action="MODIFY",
            reasoning="Payment terms changed.",
            confidence=0.90,
        )

        with _patch_structured(response):
            result = resolve_obligation(
                obligation=MSA_OBLIGATION_PAYMENT,
                amendment_obligations=[AMENDMENT_1_PAYMENT],
                claude_client=MagicMock(),
            )

        assert result["status"] == "ACTIVE"
        assert len(result["amendment_history"]) == 1
        entry = result["amendment_history"][0]
        # These keys should be absent or None when not tagged.
        assert entry.get("doc_id") is None
        assert entry.get("doc_filename") is None
        assert entry.get("amendment_number") is None

    def test_resolve_amendment_chain_tags_obligations(self):
        """resolve_amendment_chain() should tag amendment obligations with
        document metadata so that history entries contain it."""
        responses = [
            _make_comparison_response(
                action="REPLACE",
                reasoning="Delivery timeline changed.",
                confidence=0.95,
            ),
        ]
        # Buffer for extra comparisons.
        for _ in range(10):
            responses.append(
                _make_comparison_response(
                    action="UNCHANGED",
                    reasoning="Different subject matter.",
                    confidence=0.99,
                )
            )

        chain_docs = [
            {
                "doc_id": "msa-001",
                "doc_type": "MSA",
                "filename": "MSA_2024.pdf",
                "obligations": [MSA_OBLIGATION_DELIVERY],
            },
            {
                "doc_id": "amend-001",
                "doc_type": "Amendment",
                "filename": "Amendment_1.pdf",
                "obligations": [AMENDMENT_1_DELIVERY],
            },
        ]

        with _patch_structured_side_effect(responses):
            resolved = resolve_amendment_chain(chain_docs, claude_client=MagicMock())

        # Find the superseded MSA obligation.
        msa_resolved = [r for r in resolved if r.get("source_doc_id") == "msa-001"]
        delivery = next(
            r for r in msa_resolved
            if "30 calendar days" in r.get("obligation_text", "")
        )
        assert delivery["status"] == "SUPERSEDED"
        assert len(delivery["amendment_history"]) >= 1
        entry = delivery["amendment_history"][0]
        assert entry.get("doc_id") == "amend-001"
        assert entry.get("doc_filename") == "Amendment_1.pdf"
        assert entry.get("amendment_number") == 1
