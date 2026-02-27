"""End-to-end tests for Stage 4: Document Linking.

Stage 4 is pure Python (no LLM, no DB) so every test is fully self-contained
with inline test data -- no mocking or fixtures required.
"""

from __future__ import annotations

import uuid

import pytest

from echelonos.stages.stage_4_linking import (
    backfill_dangling_references,
    find_parent_document,
    link_documents,
    parse_parent_reference,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(
    doc_type: str = "MSA",
    effective_date: str | None = "2023-01-10",
    parties: list[str] | None = None,
    parent_reference_raw: str | None = None,
    org_id: str = "org-1",
    doc_id: str | None = None,
) -> dict:
    """Build a minimal document dict for testing."""
    return {
        "id": doc_id or str(uuid.uuid4()),
        "org_id": org_id,
        "doc_type": doc_type,
        "effective_date": effective_date,
        "parties": parties or [],
        "parent_reference_raw": parent_reference_raw,
    }


# ---------------------------------------------------------------------------
# parse_parent_reference
# ---------------------------------------------------------------------------


class TestParseReferenceWithDateAndType:
    """'MSA dated January 10, 2023' should parse into doc_type + date."""

    def test_parse_reference_with_date_and_type(self):
        result = parse_parent_reference("MSA dated January 10, 2023")

        assert result["doc_type"] == "MSA"
        assert result["date"] == "2023-01-10"
        assert result["parties"] == []


class TestParseReferenceMultipleFormats:
    """Various date format strings should all parse correctly."""

    @pytest.mark.parametrize(
        "reference, expected_date",
        [
            ("MSA dated January 10, 2023", "2023-01-10"),
            ("MSA dated 01/10/2023", "2023-01-10"),
            ("MSA dated 2023-01-10", "2023-01-10"),
            ("NDA dated March 5, 2024", "2024-03-05"),
            ("SOW dated 12/25/2022", "2022-12-25"),
        ],
    )
    def test_parse_reference_multiple_formats(self, reference: str, expected_date: str):
        result = parse_parent_reference(reference)
        assert result["date"] == expected_date

    def test_parse_with_parties_and_generic_type(self):
        result = parse_parent_reference(
            "Agreement between CDW and Acme dated 2023-01-10"
        )
        assert result["doc_type"] is None  # "Agreement" maps to None (generic)
        assert result["date"] == "2023-01-10"
        assert "CDW" in result["parties"]
        assert "Acme" in result["parties"]

    def test_parse_master_services_agreement_long_form(self):
        result = parse_parent_reference(
            "Master Services Agreement dated February 14, 2024"
        )
        assert result["doc_type"] == "MSA"
        assert result["date"] == "2024-02-14"

    def test_parse_empty_string(self):
        result = parse_parent_reference("")
        assert result["doc_type"] is None
        assert result["date"] is None
        assert result["parties"] == []

    def test_parse_no_date(self):
        result = parse_parent_reference("MSA between Acme and Beta")
        assert result["doc_type"] == "MSA"
        assert result["date"] is None


# ---------------------------------------------------------------------------
# find_parent_document -- single match
# ---------------------------------------------------------------------------


class TestSingleMatchLinked:
    """When exactly one candidate matches, the result should be LINKED."""

    def test_single_match_linked(self):
        parent_id = "parent-001"
        parent = _doc(
            doc_type="MSA",
            effective_date="2023-01-10",
            parties=["Acme Corp", "Widget Inc"],
            doc_id=parent_id,
        )
        child = _doc(
            doc_type="Amendment",
            parent_reference_raw="MSA dated January 10, 2023",
            doc_id="child-001",
        )
        org_docs = [parent, child]

        result = find_parent_document(child, org_docs)

        assert result["status"] == "LINKED"
        assert result["parent_doc_id"] == parent_id
        assert result["child_doc_id"] == "child-001"
        assert len(result["candidates"]) == 1


# ---------------------------------------------------------------------------
# find_parent_document -- no match
# ---------------------------------------------------------------------------


class TestNoMatchUnlinked:
    """When no candidates match, the result should be UNLINKED."""

    def test_no_match_unlinked(self):
        # Parent has a different date than what the child references.
        parent = _doc(
            doc_type="MSA",
            effective_date="2022-06-15",
            parties=["Acme Corp"],
            doc_id="parent-001",
        )
        child = _doc(
            doc_type="Amendment",
            parent_reference_raw="MSA dated January 10, 2023",
            doc_id="child-001",
        )
        org_docs = [parent, child]

        result = find_parent_document(child, org_docs)

        assert result["status"] == "UNLINKED"
        assert result["parent_doc_id"] is None
        assert result["candidates"] == []


# ---------------------------------------------------------------------------
# find_parent_document -- ambiguous
# ---------------------------------------------------------------------------


class TestMultipleMatchesAmbiguous:
    """When multiple candidates match, the result should be AMBIGUOUS."""

    def test_multiple_matches_ambiguous(self):
        parent_a = _doc(
            doc_type="MSA",
            effective_date="2023-01-10",
            parties=["Acme Corp"],
            doc_id="parent-a",
        )
        parent_b = _doc(
            doc_type="MSA",
            effective_date="2023-01-10",
            parties=["Beta LLC"],
            doc_id="parent-b",
        )
        child = _doc(
            doc_type="Amendment",
            parent_reference_raw="MSA dated January 10, 2023",
            doc_id="child-001",
        )
        org_docs = [parent_a, parent_b, child]

        result = find_parent_document(child, org_docs)

        assert result["status"] == "AMBIGUOUS"
        assert result["parent_doc_id"] is None
        assert len(result["candidates"]) == 2
        candidate_ids = {c["id"] for c in result["candidates"]}
        assert "parent-a" in candidate_ids
        assert "parent-b" in candidate_ids


# ---------------------------------------------------------------------------
# link_documents batch
# ---------------------------------------------------------------------------


class TestLinkDocumentsBatch:
    """Process multiple documents and verify the batch results."""

    def test_link_documents_batch(self):
        parent_msa = _doc(
            doc_type="MSA",
            effective_date="2023-01-10",
            parties=["Acme Corp", "Widget Inc"],
            doc_id="msa-001",
        )
        amendment = _doc(
            doc_type="Amendment",
            parent_reference_raw="MSA dated January 10, 2023",
            doc_id="amend-001",
        )
        sow = _doc(
            doc_type="SOW",
            parent_reference_raw="MSA dated 2023-01-10",
            doc_id="sow-001",
        )
        addendum = _doc(
            doc_type="Addendum",
            parent_reference_raw="MSA dated March 15, 2025",
            doc_id="addendum-001",
        )

        all_docs = [parent_msa, amendment, sow, addendum]
        results = link_documents(all_docs)

        # 3 linkable docs processed (Amendment, SOW, Addendum).
        assert len(results) == 3

        statuses = {r["child_doc_id"]: r["status"] for r in results}
        # Amendment and SOW should link to the MSA.
        assert statuses["amend-001"] == "LINKED"
        assert statuses["sow-001"] == "LINKED"
        # Addendum references a date that doesn't match -> UNLINKED.
        assert statuses["addendum-001"] == "UNLINKED"


# ---------------------------------------------------------------------------
# Only linkable types processed
# ---------------------------------------------------------------------------


class TestOnlyLinkableTypesProcessed:
    """MSA and other non-child types should be skipped entirely."""

    def test_only_linkable_types_processed(self):
        msa = _doc(doc_type="MSA", doc_id="msa-001")
        nda = _doc(doc_type="NDA", doc_id="nda-001")
        other = _doc(doc_type="Other", doc_id="other-001")
        order_form = _doc(doc_type="Order Form", doc_id="of-001")

        # Even if they have parent_reference_raw, they should not be linked.
        for d in [msa, nda, other, order_form]:
            d["parent_reference_raw"] = "MSA dated 2023-01-10"

        results = link_documents([msa, nda, other, order_form])

        assert len(results) == 0, (
            "Non-linkable doc types should not be processed"
        )


# ---------------------------------------------------------------------------
# Backfill resolves dangling
# ---------------------------------------------------------------------------


class TestBackfillResolvesDangling:
    """A new MSA should resolve a previously dangling amendment reference."""

    def test_backfill_resolves_dangling(self):
        new_msa = _doc(
            doc_type="MSA",
            effective_date="2023-01-10",
            parties=["Acme Corp", "Widget Inc"],
            doc_id="msa-new",
        )

        dangling_refs = [
            {
                "id": "dang-001",
                "doc_id": "amend-001",
                "reference_text": "MSA dated January 10, 2023",
            },
        ]

        resolved = backfill_dangling_references(new_msa, dangling_refs)

        assert len(resolved) == 1
        assert resolved[0]["dangling_ref_id"] == "dang-001"
        assert resolved[0]["child_doc_id"] == "amend-001"
        assert resolved[0]["parent_doc_id"] == "msa-new"
        assert resolved[0]["status"] == "LINKED"


# ---------------------------------------------------------------------------
# Backfill no match
# ---------------------------------------------------------------------------


class TestBackfillNoMatch:
    """A new document that doesn't match any dangling ref returns empty."""

    def test_backfill_no_match(self):
        new_doc = _doc(
            doc_type="NDA",
            effective_date="2024-06-01",
            parties=["Gamma LLC"],
            doc_id="nda-new",
        )

        dangling_refs = [
            {
                "id": "dang-001",
                "doc_id": "amend-001",
                "reference_text": "MSA dated January 10, 2023",
            },
            {
                "id": "dang-002",
                "doc_id": "sow-001",
                "reference_text": "Agreement between Acme and Widget dated 2023-01-10",
            },
        ]

        resolved = backfill_dangling_references(new_doc, dangling_refs)

        assert len(resolved) == 0


# ---------------------------------------------------------------------------
# Party overlap matching
# ---------------------------------------------------------------------------


class TestPartyOverlapMatching:
    """Party names should be compared case-insensitively with overlap logic."""

    def test_party_overlap_matching(self):
        parent = _doc(
            doc_type="MSA",
            effective_date="2023-01-10",
            parties=["CDW Government LLC", "Acme Corp"],
            doc_id="parent-001",
        )
        child = _doc(
            doc_type="Amendment",
            parent_reference_raw="Agreement between CDW Government LLC and Acme Corp dated 2023-01-10",
            doc_id="child-001",
        )
        org_docs = [parent, child]

        result = find_parent_document(child, org_docs)

        assert result["status"] == "LINKED"
        assert result["parent_doc_id"] == "parent-001"

    def test_party_case_insensitive(self):
        parent = _doc(
            doc_type="MSA",
            effective_date="2023-01-10",
            parties=["cdw government llc", "acme corp"],
            doc_id="parent-001",
        )
        child = _doc(
            doc_type="Amendment",
            parent_reference_raw="Agreement between CDW Government LLC and Acme Corp dated 2023-01-10",
            doc_id="child-001",
        )
        org_docs = [parent, child]

        result = find_parent_document(child, org_docs)

        assert result["status"] == "LINKED"
        assert result["parent_doc_id"] == "parent-001"

    def test_single_party_overlap_suffices(self):
        """A match on one party (plus date) is enough when the reference
        mentions two parties and the candidate has both."""
        parent = _doc(
            doc_type="MSA",
            effective_date="2023-01-10",
            parties=["CDW Government LLC", "Acme Corp"],
            doc_id="parent-001",
        )
        # Reference mentions only one of the parent's parties alongside a
        # non-matching one, but the overlap with "CDW Government LLC" is
        # still present.
        child = _doc(
            doc_type="Amendment",
            parent_reference_raw="Agreement between CDW Government LLC and SomeOther dated 2023-01-10",
            doc_id="child-001",
        )
        org_docs = [parent, child]

        result = find_parent_document(child, org_docs)

        assert result["status"] == "LINKED"
        assert result["parent_doc_id"] == "parent-001"
