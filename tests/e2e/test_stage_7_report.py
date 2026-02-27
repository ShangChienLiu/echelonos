"""End-to-end tests for Stage 7: Report Generation.

Stage 7 is pure Python (no LLM, no DB) so every test is fully self-contained
with inline test data -- no mocking or fixtures required.
"""

from __future__ import annotations

import json
import uuid

import pytest

from echelonos.stages.stage_7_report import (
    FlagItem,
    ObligationReport,
    ObligationRow,
    build_flag_report,
    build_obligation_matrix,
    build_summary,
    export_to_json,
    export_to_markdown,
    generate_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_NAME = "Acme Corp"


def _obligation(
    *,
    obligation_text: str = "Deliver monthly report",
    obligation_type: str = "Delivery",
    responsible_party: str = "Vendor",
    counterparty: str = "Client",
    status: str = "ACTIVE",
    confidence: float = 0.95,
    doc_id: str = "doc-001",
    source_clause: str = "Section 4.2: Vendor shall deliver monthly reports",
    frequency: str | None = "Monthly",
    deadline: str | None = "30th of each month",
    obl_id: str | None = None,
    verification_result: dict | None = None,
) -> dict:
    """Build a minimal obligation dict for testing."""
    return {
        "id": obl_id or str(uuid.uuid4()),
        "doc_id": doc_id,
        "obligation_text": obligation_text,
        "obligation_type": obligation_type,
        "responsible_party": responsible_party,
        "counterparty": counterparty,
        "status": status,
        "confidence": confidence,
        "source_clause": source_clause,
        "frequency": frequency,
        "deadline": deadline,
        "verification_result": verification_result,
    }


def _document(
    *,
    doc_id: str = "doc-001",
    doc_type: str = "SOW",
    parties: list[str] | None = None,
) -> dict:
    """Build a minimal document dict for testing."""
    return {
        "id": doc_id,
        "doc_type": doc_type,
        "parties": parties or ["Acme Corp", "Widget Inc"],
    }


def _link(
    *,
    child_doc_id: str = "doc-002",
    parent_doc_id: str | None = "doc-001",
    status: str = "LINKED",
    candidates: list | None = None,
) -> dict:
    """Build a minimal link dict for testing."""
    return {
        "child_doc_id": child_doc_id,
        "parent_doc_id": parent_doc_id,
        "status": status,
        "candidates": candidates or [],
    }


def _sample_obligations() -> list[dict]:
    """Return a realistic set of sample obligations covering different types
    and statuses."""
    return [
        _obligation(
            obl_id="obl-001",
            obligation_text="Vendor shall deliver monthly SLA reports",
            obligation_type="Delivery",
            responsible_party="Vendor",
            counterparty="Client",
            status="ACTIVE",
            confidence=0.95,
            doc_id="doc-sow",
            source_clause="Section 4.2: Vendor shall deliver monthly SLA reports",
            frequency="Monthly",
            deadline="5th business day",
        ),
        _obligation(
            obl_id="obl-002",
            obligation_text="Client shall pay invoices within 30 days",
            obligation_type="Financial",
            responsible_party="Client",
            counterparty="Vendor",
            status="ACTIVE",
            confidence=0.92,
            doc_id="doc-msa",
            source_clause="Section 8.1: Client shall pay all invoices within 30 days",
            frequency=None,
            deadline="30 days from invoice date",
        ),
        _obligation(
            obl_id="obl-003",
            obligation_text="Vendor shall maintain 99.9% uptime SLA",
            obligation_type="SLA",
            responsible_party="Vendor",
            counterparty="Client",
            status="SUPERSEDED",
            confidence=0.88,
            doc_id="doc-sow",
            source_clause="Section 5.1: Vendor shall maintain 99.9% uptime",
            frequency="Continuous",
            deadline=None,
        ),
        _obligation(
            obl_id="obl-004",
            obligation_text="Both parties shall maintain confidentiality",
            obligation_type="Confidentiality",
            responsible_party="Both",
            counterparty="Both",
            status="ACTIVE",
            confidence=0.70,
            doc_id="doc-nda",
            source_clause="Article 2: Both parties agree to maintain confidentiality",
            frequency=None,
            deadline=None,
        ),
        _obligation(
            obl_id="obl-005",
            obligation_text="Vendor shall comply with amended reporting requirements",
            obligation_type="Compliance",
            responsible_party="Vendor",
            counterparty="Client",
            status="UNRESOLVED",
            confidence=0.60,
            doc_id="doc-amd",
            source_clause="Section 3: Vendor shall comply with new reporting requirements",
            frequency="Quarterly",
            deadline="End of quarter",
        ),
    ]


def _sample_documents() -> dict[str, dict]:
    """Return a mapping of doc_id -> document dict."""
    return {
        "doc-msa": _document(doc_id="doc-msa", doc_type="MSA"),
        "doc-sow": _document(doc_id="doc-sow", doc_type="SOW"),
        "doc-nda": _document(doc_id="doc-nda", doc_type="NDA"),
        "doc-amd": _document(doc_id="doc-amd", doc_type="Amendment"),
    }


def _sample_links() -> list[dict]:
    """Return sample link results."""
    return [
        _link(child_doc_id="doc-sow", parent_doc_id="doc-msa", status="LINKED"),
        _link(child_doc_id="doc-amd", parent_doc_id="doc-msa", status="LINKED"),
    ]


# ---------------------------------------------------------------------------
# test_build_obligation_matrix
# ---------------------------------------------------------------------------


class TestBuildObligationMatrix:
    """Correct rows with proper source formatting."""

    def test_build_obligation_matrix(self):
        obligations = _sample_obligations()
        documents = _sample_documents()
        links = _sample_links()

        rows = build_obligation_matrix(obligations, documents, links)

        assert len(rows) == 5
        # All rows should be ObligationRow instances.
        assert all(isinstance(r, ObligationRow) for r in rows)
        # Rows should be numbered sequentially.
        assert [r.number for r in rows] == [1, 2, 3, 4, 5]

    def test_source_formatting_with_section(self):
        obligations = [
            _obligation(
                doc_id="doc-sow",
                source_clause="Section 4.2: Vendor shall deliver reports",
            ),
        ]
        documents = {"doc-sow": _document(doc_id="doc-sow", doc_type="SOW")}
        links: list[dict] = []

        rows = build_obligation_matrix(obligations, documents, links)

        assert len(rows) == 1
        assert "SOW" in rows[0].source
        assert "\u00a74.2" in rows[0].source

    def test_source_formatting_amendment_modified(self):
        """An obligation from an amendment should show 'Amd #N modified'."""
        obligations = [
            _obligation(
                doc_id="doc-amd",
                source_clause="Section 3: new requirements",
            ),
        ]
        documents = {
            "doc-msa": _document(doc_id="doc-msa", doc_type="MSA"),
            "doc-amd": _document(doc_id="doc-amd", doc_type="Amendment"),
        }
        links = [
            _link(child_doc_id="doc-amd", parent_doc_id="doc-msa", status="LINKED"),
        ]

        rows = build_obligation_matrix(obligations, documents, links)

        assert len(rows) == 1
        assert "Amd #1 modified" in rows[0].source


# ---------------------------------------------------------------------------
# test_matrix_sorting
# ---------------------------------------------------------------------------


class TestMatrixSorting:
    """ACTIVE obligations should appear first, then by type, then by party."""

    def test_matrix_sorting(self):
        obligations = [
            _obligation(
                obl_id="obl-sup",
                status="SUPERSEDED",
                obligation_type="Delivery",
                responsible_party="Vendor",
            ),
            _obligation(
                obl_id="obl-active-fin",
                status="ACTIVE",
                obligation_type="Financial",
                responsible_party="Client",
            ),
            _obligation(
                obl_id="obl-active-del",
                status="ACTIVE",
                obligation_type="Delivery",
                responsible_party="Vendor",
            ),
            _obligation(
                obl_id="obl-terminated",
                status="TERMINATED",
                obligation_type="Compliance",
                responsible_party="Both",
            ),
        ]
        documents = {"doc-001": _document()}
        links: list[dict] = []

        rows = build_obligation_matrix(obligations, documents, links)

        statuses = [r.status for r in rows]
        # ACTIVE should come before SUPERSEDED, which comes before TERMINATED.
        assert statuses.index("ACTIVE") < statuses.index("SUPERSEDED")
        assert statuses.index("SUPERSEDED") < statuses.index("TERMINATED")

        # Among ACTIVE rows: Delivery before Financial (alphabetical).
        active_types = [r.obligation_type for r in rows if r.status == "ACTIVE"]
        assert active_types == ["Delivery", "Financial"]


# ---------------------------------------------------------------------------
# test_build_flag_report_unverified
# ---------------------------------------------------------------------------


class TestBuildFlagReportUnverified:
    """An obligation with a failed verification should produce a RED flag."""

    def test_build_flag_report_unverified(self):
        obligations = [
            _obligation(
                obl_id="obl-unv",
                verification_result={"verified": False, "reason": "Clause not found"},
            ),
        ]
        documents: list[dict] = []
        links: list[dict] = []

        flags = build_flag_report(obligations, documents, links)

        unverified_flags = [f for f in flags if f.flag_type == "UNVERIFIED"]
        assert len(unverified_flags) == 1
        assert unverified_flags[0].severity == "RED"
        assert unverified_flags[0].entity_type == "obligation"
        assert unverified_flags[0].entity_id == "obl-unv"


# ---------------------------------------------------------------------------
# test_build_flag_report_unlinked
# ---------------------------------------------------------------------------


class TestBuildFlagReportUnlinked:
    """An unlinked document should produce a YELLOW flag."""

    def test_build_flag_report_unlinked(self):
        obligations: list[dict] = []
        documents: list[dict] = []
        links = [
            _link(
                child_doc_id="doc-orphan",
                parent_doc_id=None,
                status="UNLINKED",
            ),
        ]

        flags = build_flag_report(obligations, documents, links)

        unlinked_flags = [f for f in flags if f.flag_type == "UNLINKED"]
        assert len(unlinked_flags) == 1
        assert unlinked_flags[0].severity == "YELLOW"
        assert unlinked_flags[0].entity_type == "document"
        assert unlinked_flags[0].entity_id == "doc-orphan"

    def test_ambiguous_link_produces_orange_flag(self):
        obligations: list[dict] = []
        documents: list[dict] = []
        links = [
            _link(
                child_doc_id="doc-ambig",
                parent_doc_id=None,
                status="AMBIGUOUS",
                candidates=[{"id": "cand-1"}, {"id": "cand-2"}],
            ),
        ]

        flags = build_flag_report(obligations, documents, links)

        ambiguous_flags = [f for f in flags if f.flag_type == "AMBIGUOUS"]
        assert len(ambiguous_flags) == 1
        assert ambiguous_flags[0].severity == "ORANGE"
        assert "2" in ambiguous_flags[0].message  # mentions candidate count


# ---------------------------------------------------------------------------
# test_build_flag_report_low_confidence
# ---------------------------------------------------------------------------


class TestBuildFlagReportLowConfidence:
    """A low-confidence obligation should produce a WHITE flag."""

    def test_build_flag_report_low_confidence(self):
        obligations = [
            _obligation(
                obl_id="obl-low",
                confidence=0.55,
            ),
        ]
        documents: list[dict] = []
        links: list[dict] = []

        flags = build_flag_report(obligations, documents, links)

        low_conf_flags = [f for f in flags if f.flag_type == "LOW_CONFIDENCE"]
        assert len(low_conf_flags) == 1
        assert low_conf_flags[0].severity == "WHITE"
        assert "0.55" in low_conf_flags[0].message

    def test_high_confidence_no_flag(self):
        obligations = [
            _obligation(obl_id="obl-high", confidence=0.95),
        ]
        documents: list[dict] = []
        links: list[dict] = []

        flags = build_flag_report(obligations, documents, links)

        low_conf_flags = [f for f in flags if f.flag_type == "LOW_CONFIDENCE"]
        assert len(low_conf_flags) == 0

    def test_unresolved_obligation_from_unlinked_doc(self):
        """An obligation belonging to an unlinked document should produce
        an UNRESOLVED YELLOW flag."""
        obligations = [
            _obligation(obl_id="obl-dangling", doc_id="doc-orphan"),
        ]
        documents: list[dict] = []
        links = [
            _link(
                child_doc_id="doc-orphan",
                parent_doc_id=None,
                status="UNLINKED",
            ),
        ]

        flags = build_flag_report(obligations, documents, links)

        unresolved_flags = [f for f in flags if f.flag_type == "UNRESOLVED"]
        assert len(unresolved_flags) == 1
        assert unresolved_flags[0].severity == "YELLOW"


# ---------------------------------------------------------------------------
# test_build_summary
# ---------------------------------------------------------------------------


class TestBuildSummary:
    """Correct aggregation counts."""

    def test_summary_uses_by_responsible_party_key(self):
        """The summary must use 'by_responsible_party' (not 'by_party') so
        the frontend SummaryData interface can consume it without errors."""
        rows = [
            ObligationRow(
                number=1,
                obligation_text="Deliver reports",
                obligation_type="Delivery",
                responsible_party="Vendor",
                counterparty="Client",
                source="SOW",
                status="ACTIVE",
                confidence=0.95,
            ),
        ]
        flags: list[FlagItem] = []

        summary = build_summary(rows, flags)

        assert "by_responsible_party" in summary, (
            "build_summary must return 'by_responsible_party' key to match "
            "the frontend SummaryData interface; got keys: "
            + str(list(summary.keys()))
        )
        assert "by_party" not in summary, (
            "'by_party' key should be renamed to 'by_responsible_party'"
        )
        assert summary["by_responsible_party"]["Vendor"] == 1

    def test_build_summary(self):
        rows = [
            ObligationRow(
                number=1,
                obligation_text="Deliver reports",
                obligation_type="Delivery",
                responsible_party="Vendor",
                counterparty="Client",
                source="SOW",
                status="ACTIVE",
                confidence=0.95,
            ),
            ObligationRow(
                number=2,
                obligation_text="Pay invoices",
                obligation_type="Financial",
                responsible_party="Client",
                counterparty="Vendor",
                source="MSA",
                status="ACTIVE",
                confidence=0.90,
            ),
            ObligationRow(
                number=3,
                obligation_text="Old SLA",
                obligation_type="SLA",
                responsible_party="Vendor",
                counterparty="Client",
                source="SOW",
                status="SUPERSEDED",
                confidence=0.88,
            ),
        ]
        flags = [
            FlagItem(
                flag_type="UNVERIFIED",
                severity="RED",
                entity_type="obligation",
                entity_id="obl-1",
                message="Test",
            ),
            FlagItem(
                flag_type="LOW_CONFIDENCE",
                severity="WHITE",
                entity_type="obligation",
                entity_id="obl-2",
                message="Test",
            ),
            FlagItem(
                flag_type="UNLINKED",
                severity="YELLOW",
                entity_type="document",
                entity_id="doc-1",
                message="Test",
            ),
        ]

        summary = build_summary(rows, flags)

        # By type.
        assert summary["by_type"]["Delivery"] == 1
        assert summary["by_type"]["Financial"] == 1
        assert summary["by_type"]["SLA"] == 1

        # By status.
        assert summary["by_status"]["ACTIVE"] == 2
        assert summary["by_status"]["SUPERSEDED"] == 1

        # By party.
        assert summary["by_responsible_party"]["Vendor"] == 2
        assert summary["by_responsible_party"]["Client"] == 1

        # Flags by severity.
        assert summary["flags_by_severity"]["RED"] == 1
        assert summary["flags_by_severity"]["WHITE"] == 1
        assert summary["flags_by_severity"]["YELLOW"] == 1

        # Flags by type.
        assert summary["flags_by_type"]["UNVERIFIED"] == 1
        assert summary["flags_by_type"]["LOW_CONFIDENCE"] == 1
        assert summary["flags_by_type"]["UNLINKED"] == 1


# ---------------------------------------------------------------------------
# test_generate_report_complete
# ---------------------------------------------------------------------------


class TestGenerateReportComplete:
    """Full report with all sections."""

    def test_generate_report_complete(self):
        obligations = _sample_obligations()
        documents = _sample_documents()
        links = _sample_links()

        report = generate_report(_ORG_NAME, obligations, documents, links)

        assert isinstance(report, ObligationReport)
        assert report.org_name == _ORG_NAME
        assert report.generated_at  # non-empty ISO timestamp
        assert report.total_obligations == 5
        assert report.active_obligations == 3  # obl-001, obl-002, obl-004
        assert report.superseded_obligations == 1  # obl-003
        assert report.unresolved_obligations == 1  # obl-005
        assert len(report.obligations) == 5
        assert len(report.summary) > 0

    def test_report_obligations_are_sorted(self):
        obligations = _sample_obligations()
        documents = _sample_documents()
        links = _sample_links()

        report = generate_report(_ORG_NAME, obligations, documents, links)

        statuses = [r.status for r in report.obligations]
        # All ACTIVE should come before UNRESOLVED, which comes before
        # SUPERSEDED.
        first_active = statuses.index("ACTIVE")
        last_active = len(statuses) - 1 - statuses[::-1].index("ACTIVE")
        first_non_active = next(
            (i for i, s in enumerate(statuses) if s != "ACTIVE"), None
        )
        if first_non_active is not None:
            assert first_non_active > last_active or first_non_active == 0

    def test_report_flags_generated(self):
        """The sample data should produce at least one LOW_CONFIDENCE flag
        (obl-004 has confidence 0.70, obl-005 has 0.60)."""
        obligations = _sample_obligations()
        documents = _sample_documents()
        links = _sample_links()

        report = generate_report(_ORG_NAME, obligations, documents, links)

        low_conf = [f for f in report.flags if f.flag_type == "LOW_CONFIDENCE"]
        assert len(low_conf) >= 2  # obl-004 (0.70) and obl-005 (0.60)

    def test_report_summary_has_expected_keys(self):
        obligations = _sample_obligations()
        documents = _sample_documents()
        links = _sample_links()

        report = generate_report(_ORG_NAME, obligations, documents, links)

        assert "by_type" in report.summary
        assert "by_status" in report.summary
        assert "by_responsible_party" in report.summary
        assert "flags_by_severity" in report.summary
        assert "flags_by_type" in report.summary


# ---------------------------------------------------------------------------
# test_export_to_markdown
# ---------------------------------------------------------------------------


class TestExportToMarkdown:
    """Valid markdown table output."""

    def test_export_to_markdown(self):
        obligations = _sample_obligations()
        documents = _sample_documents()
        links = _sample_links()
        report = generate_report(_ORG_NAME, obligations, documents, links)

        md = export_to_markdown(report)

        assert isinstance(md, str)
        assert len(md) > 0

        # Should contain the header.
        assert f"# Obligation Report: {_ORG_NAME}" in md

        # Should contain markdown table delimiters.
        assert "| # |" in md
        assert "| --- |" in md

        # Should contain section headers.
        assert "## Obligation Matrix" in md
        assert "## Flag Report" in md
        assert "## Summary" in md

    def test_markdown_contains_obligation_data(self):
        obligations = [
            _obligation(
                obl_id="obl-md",
                obligation_text="Deliver quarterly reports",
                obligation_type="Delivery",
                status="ACTIVE",
            ),
        ]
        documents = {"doc-001": _document()}
        links: list[dict] = []
        report = generate_report(_ORG_NAME, obligations, documents, links)

        md = export_to_markdown(report)

        assert "Deliver quarterly reports" in md
        assert "Delivery" in md
        assert "ACTIVE" in md

    def test_markdown_flag_severity_indicators(self):
        obligations = [
            _obligation(
                obl_id="obl-low",
                confidence=0.50,
                verification_result={"verified": False, "reason": "fail"},
            ),
        ]
        documents = {"doc-001": _document()}
        links: list[dict] = []
        report = generate_report(_ORG_NAME, obligations, documents, links)

        md = export_to_markdown(report)

        # Should contain severity indicators.
        assert "[RED]" in md or "[WHITE]" in md


# ---------------------------------------------------------------------------
# test_export_to_json
# ---------------------------------------------------------------------------


class TestExportToJson:
    """Valid JSON output that can be parsed back."""

    def test_export_to_json(self):
        obligations = _sample_obligations()
        documents = _sample_documents()
        links = _sample_links()
        report = generate_report(_ORG_NAME, obligations, documents, links)

        json_str = export_to_json(report)

        assert isinstance(json_str, str)
        assert len(json_str) > 0

        # Must be valid JSON.
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)

    def test_json_roundtrip(self):
        """JSON output can be parsed back into an ObligationReport."""
        obligations = _sample_obligations()
        documents = _sample_documents()
        links = _sample_links()
        report = generate_report(_ORG_NAME, obligations, documents, links)

        json_str = export_to_json(report)
        parsed = json.loads(json_str)

        # Verify key fields survived the roundtrip.
        assert parsed["org_name"] == _ORG_NAME
        assert parsed["total_obligations"] == 5
        assert len(parsed["obligations"]) == 5
        assert isinstance(parsed["flags"], list)
        assert isinstance(parsed["summary"], dict)

    def test_json_obligation_fields(self):
        """Each obligation in the JSON should have all expected fields."""
        obligations = [_obligation(obl_id="obl-json")]
        documents = {"doc-001": _document()}
        links: list[dict] = []
        report = generate_report(_ORG_NAME, obligations, documents, links)

        json_str = export_to_json(report)
        parsed = json.loads(json_str)

        obl = parsed["obligations"][0]
        expected_fields = {
            "number", "obligation_text", "obligation_type",
            "responsible_party", "counterparty", "source",
            "status", "frequency", "deadline", "confidence",
        }
        assert expected_fields.issubset(set(obl.keys()))


# ---------------------------------------------------------------------------
# test_empty_obligations
# ---------------------------------------------------------------------------


class TestEmptyObligations:
    """Empty input should produce an empty report without crashes."""

    def test_empty_obligations(self):
        report = generate_report(_ORG_NAME, [], {}, [])

        assert isinstance(report, ObligationReport)
        assert report.org_name == _ORG_NAME
        assert report.total_obligations == 0
        assert report.active_obligations == 0
        assert report.superseded_obligations == 0
        assert report.unresolved_obligations == 0
        assert report.obligations == []
        assert report.flags == []

    def test_empty_markdown_export(self):
        report = generate_report(_ORG_NAME, [], {}, [])
        md = export_to_markdown(report)

        assert isinstance(md, str)
        assert _ORG_NAME in md
        assert "_No obligations found._" in md
        assert "_No flags._" in md

    def test_empty_json_export(self):
        report = generate_report(_ORG_NAME, [], {}, [])
        json_str = export_to_json(report)

        parsed = json.loads(json_str)
        assert parsed["total_obligations"] == 0
        assert parsed["obligations"] == []
        assert parsed["flags"] == []

    def test_empty_summary(self):
        report = generate_report(_ORG_NAME, [], {}, [])

        assert report.summary["by_type"] == {}
        assert report.summary["by_status"] == {}
        assert report.summary["by_responsible_party"] == {}
        assert report.summary["flags_by_severity"] == {}
        assert report.summary["flags_by_type"] == {}
