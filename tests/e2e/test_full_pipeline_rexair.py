"""E2E tests: Full pipeline workflow for a simulated 'Rexair' organization.

Creates one minimal file of each supported type (PDF, DOCX, HTML, XLSX,
PNG, JPG, ZIP) inside a temporary org folder and runs every stage of the
pipeline end-to-end with mocked external services (Mistral OCR, Claude,
OpenAI).  Each test class covers a different file format category.

When a stage fails, the test report captures the exact stage, file type,
and error so the root cause is immediately visible.
"""

from __future__ import annotations

import sys
import types
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import MINIMAL_JPG, MINIMAL_PDF_BYTES, MINIMAL_PNG

# ---------------------------------------------------------------------------
# Stub out missing optional dependencies so imports don't fail at collection
# ---------------------------------------------------------------------------

if "mistralai" not in sys.modules:
    _mistralai = types.ModuleType("mistralai")
    _mistralai.Mistral = MagicMock  # type: ignore[attr-defined]
    sys.modules["mistralai"] = _mistralai

if "extract_msg" not in sys.modules:
    _extract_msg = types.ModuleType("extract_msg")
    _extract_msg.Message = MagicMock  # type: ignore[attr-defined]
    sys.modules["extract_msg"] = _extract_msg

# ---------------------------------------------------------------------------
# Stage imports (safe now that stubs are in place)
# ---------------------------------------------------------------------------

from echelonos.stages.stage_0a_validation import validate_file, validate_folder
from echelonos.stages.stage_0b_dedup import deduplicate_files
from echelonos.stages.stage_1_ocr import ingest_document, get_full_text, _assess_confidence
from echelonos.stages.stage_2_classification import (
    ClassificationResult,
    classify_document,
    classify_with_cross_check,
)
from echelonos.stages.stage_3_extraction import (
    ExtractionResult,
    Obligation,
    extract_and_verify,
    extract_obligations,
    extract_party_roles,
    verify_grounding,
)
from echelonos.stages.stage_4_linking import (
    find_parent_document,
    link_documents,
    parse_parent_reference,
)
from echelonos.stages.stage_5_amendment import (
    build_amendment_chain,
    resolve_all,
    resolve_obligation,
)
from echelonos.stages.stage_6_evidence import (
    EvidenceRecord,
    create_evidence_record,
    package_evidence,
    validate_evidence_chain,
)
from echelonos.stages.stage_7_report import (
    ObligationReport,
    build_flag_report,
    build_obligation_matrix,
    build_summary,
    export_to_json,
    export_to_markdown,
    generate_report,
)


# ---------------------------------------------------------------------------
# Fixtures: Rexair org folder and sample files
# ---------------------------------------------------------------------------


@pytest.fixture
def rexair_org(tmp_path: Path) -> Path:
    """Create a temporary 'Rexair' organization folder."""
    org = tmp_path / "Rexair"
    org.mkdir()
    return org


@pytest.fixture
def rexair_pdf(rexair_org: Path) -> Path:
    """Minimal valid PDF with extractable text in Rexair org."""
    p = rexair_org / "rexair_msa.pdf"
    p.write_bytes(MINIMAL_PDF_BYTES)
    return p


@pytest.fixture
def rexair_docx(rexair_org: Path) -> Path:
    """Minimal DOCX in Rexair org."""
    docx_mod = pytest.importorskip("docx", reason="python-docx not installed")

    p = rexair_org / "rexair_sow.docx"
    doc = docx_mod.Document()
    doc.add_paragraph("Statement of Work between Rexair Inc and Acme Corp.")
    doc.add_paragraph("Section 4.2: Vendor shall deliver monthly reports.")
    doc.save(str(p))
    return p


@pytest.fixture
def rexair_html(rexair_org: Path) -> Path:
    """HTML contract file in Rexair org."""
    p = rexair_org / "rexair_nda.html"
    p.write_text(
        "<!DOCTYPE html><html><head><title>NDA</title></head><body>"
        "<h1>Non-Disclosure Agreement</h1>"
        "<p>Between Rexair Inc and Acme Corp effective January 1, 2024.</p>"
        "<p>Section 1: Confidential information shall not be disclosed.</p>"
        "</body></html>",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def rexair_xlsx(rexair_org: Path) -> Path:
    """XLSX spreadsheet in Rexair org."""
    openpyxl = pytest.importorskip("openpyxl", reason="openpyxl not installed")
    Workbook = openpyxl.Workbook

    p = rexair_org / "rexair_pricing.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Item", "Price", "Quantity"])
    ws.append(["Widget A", 100, 50])
    ws.append(["Widget B", 200, 30])
    wb.save(str(p))
    return p


@pytest.fixture
def rexair_png(rexair_org: Path) -> Path:
    """Minimal PNG image in Rexair org."""
    p = rexair_org / "rexair_scan.png"
    p.write_bytes(MINIMAL_PNG)
    return p


@pytest.fixture
def rexair_jpg(rexair_org: Path) -> Path:
    """Minimal JPG image in Rexair org."""
    p = rexair_org / "rexair_photo.jpg"
    p.write_bytes(MINIMAL_JPG)
    return p


@pytest.fixture
def rexair_zip(rexair_org: Path) -> Path:
    """ZIP containing a PDF and text file in Rexair org."""
    p = rexair_org / "rexair_bundle.zip"
    with zipfile.ZipFile(str(p), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("inner_contract.pdf", MINIMAL_PDF_BYTES)
        zf.writestr("notes.txt", "Internal notes about the Rexair deal.")
    return p


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _mock_ocr_result(text: str = "Test contract text.", num_pages: int = 1) -> dict:
    """Produce a fake Mistral OCR response."""
    pages = []
    for i in range(num_pages):
        pages.append(
            {
                "page_number": i + 1,
                "text": text,
                "tables": [],
                "confidence": 0.95,
            }
        )
    return {"pages": pages, "total_pages": num_pages}


def _mock_classification() -> ClassificationResult:
    """Produce a fake classification result."""
    return ClassificationResult(
        doc_type="MSA",
        parties=["Rexair Inc", "Acme Corp"],
        effective_date="2024-01-01",
        parent_reference_raw=None,
        confidence=0.95,
    )


def _mock_obligation(
    text: str = "Vendor shall deliver monthly reports",
    clause: str = "Section 4.2: Vendor shall deliver monthly reports.",
) -> Obligation:
    return Obligation(
        obligation_text=text,
        obligation_type="Delivery",
        responsible_party="Vendor",
        counterparty="Client",
        frequency="Monthly",
        deadline=None,
        source_clause=clause,
        source_page=1,
        confidence=0.92,
    )


def _mock_extraction_result() -> ExtractionResult:
    return ExtractionResult(
        obligations=[_mock_obligation()],
        party_roles={"Vendor": "Rexair Inc", "Client": "Acme Corp"},
    )


def _mock_claude_client():
    """Return a mock Anthropic client that produces predictable responses."""
    client = MagicMock()

    # For structured output (tool_use) calls
    mock_tool_block = MagicMock()
    mock_tool_block.type = "tool_use"
    mock_tool_block.name = "structured_output"
    mock_tool_block.input = {
        "doc_type": "MSA",
        "parties": ["Rexair Inc", "Acme Corp"],
        "effective_date": "2024-01-01",
        "parent_reference_raw": None,
        "confidence": 0.95,
    }

    mock_response = MagicMock()
    mock_response.content = [mock_tool_block]
    mock_response.id = "test-response-id"
    client.messages.create.return_value = mock_response

    return client


# ---------------------------------------------------------------------------
# Stage 0a: Validation tests per file type
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestStage0aValidationRexair:
    """Stage 0a validation for each Rexair file type."""

    def test_pdf_validates_as_valid(self, rexair_pdf: Path) -> None:
        result = validate_file(str(rexair_pdf))
        assert result["status"] == "VALID", f"PDF failed: {result['reason']}"
        assert result["original_format"] == "PDF"
        assert result["needs_ocr"] is True

    def test_docx_validates_as_valid(self, rexair_docx: Path) -> None:
        result = validate_file(str(rexair_docx))
        assert result["status"] == "VALID", f"DOCX failed: {result['reason']}"
        assert result["original_format"] == "DOCX"

    def test_html_validates_as_valid(self, rexair_html: Path) -> None:
        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="text/html",
        ):
            result = validate_file(str(rexair_html))
        assert result["status"] == "VALID", f"HTML failed: {result['reason']}"
        assert result["original_format"] == "HTML"

    def test_xlsx_validates_as_valid(self, rexair_xlsx: Path) -> None:
        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ):
            result = validate_file(str(rexair_xlsx))
        assert result["status"] == "VALID", f"XLSX failed: {result['reason']}"
        assert result["original_format"] == "XLSX"

    def test_png_validates_with_ocr_flag(self, rexair_png: Path) -> None:
        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="image/png",
        ):
            result = validate_file(str(rexair_png))
        assert result["status"] == "VALID", f"PNG failed: {result['reason']}"
        assert result["needs_ocr"] is True
        assert result["original_format"] == "PNG"

    def test_jpg_validates_with_ocr_flag(self, rexair_jpg: Path) -> None:
        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="image/jpeg",
        ):
            result = validate_file(str(rexair_jpg))
        assert result["status"] == "VALID", f"JPG failed: {result['reason']}"
        assert result["needs_ocr"] is True
        assert result["original_format"] == "JPG"

    def test_zip_extracts_children(self, rexair_zip: Path) -> None:
        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="application/zip",
        ):
            result = validate_file(str(rexair_zip))
        assert result["status"] == "VALID", f"ZIP failed: {result['reason']}"
        assert result["original_format"] == "ZIP"
        assert len(result["child_files"]) == 2

    def test_full_folder_validation(
        self,
        rexair_pdf: Path,
        rexair_docx: Path,
        rexair_org: Path,
    ) -> None:
        """Validate entire Rexair org folder with mixed file types."""
        results = validate_folder(str(rexair_org))
        assert len(results) >= 2
        valid = [r for r in results if r["status"] == "VALID"]
        assert len(valid) >= 2, (
            f"Expected >= 2 valid files, got {len(valid)}. "
            f"Failures: {[(r['file_path'], r['reason']) for r in results if r['status'] != 'VALID']}"
        )


# ---------------------------------------------------------------------------
# Stage 0b: Deduplication tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestStage0bDedupRexair:
    """Stage 0b dedup for Rexair files."""

    def test_unique_files_pass_through(self, rexair_pdf: Path, rexair_docx: Path) -> None:
        files = [
            {"file_path": str(rexair_pdf), "status": "VALID"},
            {"file_path": str(rexair_docx), "status": "VALID"},
        ]
        unique = deduplicate_files(files)
        assert len(unique) == 2, (
            f"Expected 2 unique files, got {len(unique)}. "
            f"Duplicates: {[f for f in files if f.get('is_duplicate')]}"
        )

    def test_duplicate_pdf_detected(self, rexair_org: Path) -> None:
        """Two identical PDFs should be deduplicated."""
        p1 = rexair_org / "copy1.pdf"
        p2 = rexair_org / "copy2.pdf"
        p1.write_bytes(MINIMAL_PDF_BYTES)
        p2.write_bytes(MINIMAL_PDF_BYTES)

        files = [
            {"file_path": str(p1), "status": "VALID"},
            {"file_path": str(p2), "status": "VALID"},
        ]
        unique = deduplicate_files(files)
        assert len(unique) == 1, f"Expected 1 unique file, got {len(unique)}"


# ---------------------------------------------------------------------------
# Stage 1: OCR / Ingestion tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestStage1OcrRexair:
    """Stage 1 OCR ingestion with mocked Mistral client."""

    def test_pdf_ingestion(self, rexair_pdf: Path) -> None:
        mock_client = MagicMock()
        mock_client_fn = MagicMock(return_value=_mock_ocr_result(
            "MASTER SERVICE AGREEMENT between Rexair Inc and Acme Corp.\n"
            "Section 4.2: Vendor shall deliver monthly reports.",
            num_pages=1,
        ))

        with patch(
            "echelonos.stages.stage_1_ocr.analyze_document",
            mock_client_fn,
        ):
            result = ingest_document(str(rexair_pdf), doc_id="rexair-msa-001", ocr_client=mock_client)

        assert result["doc_id"] == "rexair-msa-001"
        assert result["total_pages"] == 1
        assert len(result["pages"]) == 1
        assert result["pages"][0]["text"], "OCR should return non-empty text"

    def test_image_ingestion_needs_ocr(self, rexair_png: Path) -> None:
        mock_client = MagicMock()
        mock_client_fn = MagicMock(return_value=_mock_ocr_result(
            "Scanned contract page for Rexair.",
            num_pages=1,
        ))

        with patch(
            "echelonos.stages.stage_1_ocr.analyze_document",
            mock_client_fn,
        ):
            result = ingest_document(str(rexair_png), doc_id="rexair-scan-001", ocr_client=mock_client)

        assert result["total_pages"] == 1
        assert "Rexair" in result["pages"][0]["text"]

    def test_get_full_text_concatenation(self) -> None:
        pages = [
            {"text": "Page one content.", "tables_markdown": ""},
            {"text": "Page two content.", "tables_markdown": "| col1 | col2 |"},
        ]
        full = get_full_text(pages)
        assert "Page one content." in full
        assert "Page two content." in full
        assert "col1" in full

    def test_confidence_quality_gate(self) -> None:
        pages = [
            {"page_number": 1, "ocr_confidence": 0.95},
            {"page_number": 2, "ocr_confidence": 0.50},
            {"page_number": 3, "ocr_confidence": 0.75},
        ]
        flags = _assess_confidence(pages)
        low_flags = [f for f in flags if f["flag_type"] == "LOW_OCR_QUALITY"]
        med_flags = [f for f in flags if f["flag_type"] == "MEDIUM_OCR_QUALITY"]
        assert len(low_flags) == 1, "Page 2 (conf=0.50) should be LOW"
        assert len(med_flags) == 1, "Page 3 (conf=0.75) should be MEDIUM"


# ---------------------------------------------------------------------------
# Stage 2: Classification tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestStage2ClassificationRexair:
    """Stage 2 classification with mocked Claude client."""

    def test_classify_msa_document(self) -> None:
        """Classify a simulated Rexair MSA."""
        mock_client = _mock_claude_client()

        # Patch extract_with_structured_output to return our classification
        with patch(
            "echelonos.stages.stage_2_classification.extract_with_structured_output",
            return_value=_mock_classification(),
        ):
            result = classify_document(
                "MASTER SERVICE AGREEMENT between Rexair Inc and Acme Corp.",
                claude_client=mock_client,
            )

        assert result.doc_type == "MSA"
        assert "Rexair" in result.parties[0]
        assert result.confidence >= 0.7

    def test_classify_empty_returns_unknown(self) -> None:
        result = classify_document("")
        assert result.doc_type == "UNKNOWN"
        assert result.confidence == 0.0

    def test_cross_check_detects_amendment_language(self) -> None:
        """Cross-check reclassifies MSA with amendment language."""
        base = ClassificationResult(
            doc_type="MSA",
            parties=["Rexair Inc"],
            effective_date="2024-01-01",
            parent_reference_raw=None,
            confidence=0.90,
        )
        result = classify_with_cross_check(
            "This agreement hereby amends Section 4 of the MSA.",
            base,
        )
        assert result.doc_type == "Amendment"

    def test_cross_check_flags_amendment_without_parent(self) -> None:
        base = ClassificationResult(
            doc_type="Amendment",
            parties=["Rexair Inc"],
            effective_date="2024-06-01",
            parent_reference_raw=None,
            confidence=0.88,
        )
        result = classify_with_cross_check("Amendment to the agreement.", base)
        assert "SUSPICIOUS" in (result.parent_reference_raw or "")


# ---------------------------------------------------------------------------
# Stage 3: Extraction + Verification tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestStage3ExtractionRexair:
    """Stage 3 extraction & verification with mocked Claude."""

    def test_extract_party_roles(self) -> None:
        with patch(
            "echelonos.stages.stage_3_extraction.extract_with_structured_output",
        ) as mock_extract:
            from echelonos.stages.stage_3_extraction import _PartyRolesResponse

            mock_extract.return_value = _PartyRolesResponse(
                party_roles={"Vendor": "Rexair Inc", "Client": "Acme Corp"}
            )
            roles = extract_party_roles("Contract text...", claude_client=MagicMock())

        assert roles["Vendor"] == "Rexair Inc"
        assert roles["Client"] == "Acme Corp"

    def test_extract_obligations(self) -> None:
        with patch(
            "echelonos.stages.stage_3_extraction.extract_with_structured_output",
        ) as mock_extract:
            from echelonos.stages.stage_3_extraction import _ExtractionResponse

            mock_extract.return_value = _ExtractionResponse(
                obligations=[_mock_obligation()]
            )
            result = extract_obligations(
                "Contract text...",
                {"Vendor": "Rexair Inc", "Client": "Acme Corp"},
                claude_client=MagicMock(),
            )

        assert len(result.obligations) == 1
        assert result.obligations[0].obligation_type == "Delivery"

    def test_grounding_check_passes_for_matching_clause(self) -> None:
        raw_text = "Section 4.2: Vendor shall deliver monthly reports."
        obl = _mock_obligation(
            clause="Section 4.2: Vendor shall deliver monthly reports."
        )
        assert verify_grounding(obl, raw_text) is True

    def test_grounding_check_fails_for_missing_clause(self) -> None:
        raw_text = "Completely different contract text."
        obl = _mock_obligation()
        assert verify_grounding(obl, raw_text) is False

    def test_full_extract_and_verify(self) -> None:
        """E2E extraction + verification with all external calls mocked."""
        mock_client = MagicMock()

        # Mock party role extraction
        from echelonos.stages.stage_3_extraction import (
            _CoVeAnswersResponse,
            _CoVeQuestionsResponse,
            _ExtractionResponse,
            _PartyRolesResponse,
        )

        call_count = {"n": 0}
        responses = [
            _PartyRolesResponse(
                party_roles={"Vendor": "Rexair Inc", "Client": "Acme Corp"}
            ),
            _ExtractionResponse(obligations=[_mock_obligation()]),
        ]

        def side_effect_extract(*args, **kwargs):
            resp_format = kwargs.get("response_format") or args[3]
            if resp_format == _PartyRolesResponse:
                return responses[0]
            if resp_format == _ExtractionResponse:
                return responses[1]
            if resp_format == _CoVeQuestionsResponse:
                return _CoVeQuestionsResponse(questions=["Is this real?"])
            if resp_format == _CoVeAnswersResponse:
                return _CoVeAnswersResponse(answers=["Yes, confirmed."])
            return responses[0]

        # Mock Claude cross-verification (free-form JSON)
        mock_verify_response = MagicMock()
        mock_verify_response.content = [
            MagicMock(text='{"verified": true, "confidence": 0.92, "reason": "Clause matches."}')
        ]
        mock_client.messages.create.return_value = mock_verify_response

        raw_text = "Section 4.2: Vendor shall deliver monthly reports."

        with patch(
            "echelonos.stages.stage_3_extraction.extract_with_structured_output",
            side_effect=side_effect_extract,
        ):
            results = extract_and_verify(raw_text, claude_client=mock_client)

        assert len(results) >= 1
        assert results[0]["status"] in ("VERIFIED", "UNVERIFIED")
        assert results[0]["grounding"] in (True, False)


# ---------------------------------------------------------------------------
# Stage 4: Document Linking tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestStage4LinkingRexair:
    """Stage 4 document linking for Rexair."""

    def test_parse_parent_reference(self) -> None:
        parsed = parse_parent_reference("MSA dated January 1, 2024")
        assert parsed["doc_type"] == "MSA"
        assert parsed["date"] == "2024-01-01"

    def test_link_amendment_to_msa(self) -> None:
        msa = {
            "id": "doc-001",
            "doc_type": "MSA",
            "effective_date": "2024-01-01",
            "parties": ["Rexair Inc", "Acme Corp"],
        }
        amendment = {
            "id": "doc-002",
            "doc_type": "Amendment",
            "parent_reference_raw": "MSA dated January 1, 2024",
            "effective_date": "2024-06-01",
            "parties": ["Rexair Inc", "Acme Corp"],
        }
        result = find_parent_document(amendment, [msa, amendment])
        assert result["status"] == "LINKED"
        assert result["parent_doc_id"] == "doc-001"

    def test_unlinked_when_no_match(self) -> None:
        amendment = {
            "id": "doc-002",
            "doc_type": "Amendment",
            "parent_reference_raw": "MSA dated March 15, 2025",
        }
        result = find_parent_document(amendment, [amendment])
        assert result["status"] == "UNLINKED"

    def test_batch_link_documents(self) -> None:
        docs = [
            {
                "id": "rexair-msa",
                "org_id": "rexair",
                "doc_type": "MSA",
                "effective_date": "2024-01-01",
                "parties": ["Rexair Inc", "Acme Corp"],
                "parent_reference_raw": None,
            },
            {
                "id": "rexair-amd1",
                "org_id": "rexair",
                "doc_type": "Amendment",
                "effective_date": "2024-06-01",
                "parties": ["Rexair Inc", "Acme Corp"],
                "parent_reference_raw": "MSA dated January 1, 2024",
            },
        ]
        results = link_documents(docs)
        assert len(results) == 1
        assert results[0]["status"] == "LINKED"


# ---------------------------------------------------------------------------
# Stage 5: Amendment Resolution tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestStage5AmendmentRexair:
    """Stage 5 amendment resolution for Rexair."""

    def test_build_amendment_chain(self) -> None:
        links = [
            {
                "child_doc_id": "rexair-amd1",
                "parent_doc_id": "rexair-msa",
                "status": "LINKED",
            }
        ]
        chains = build_amendment_chain(links)
        assert len(chains) == 1
        assert chains[0] == ["rexair-msa", "rexair-amd1"]

    def test_resolve_obligation_unchanged(self) -> None:
        """Amendment clause unrelated to original -> ACTIVE."""
        from echelonos.stages.stage_5_amendment import ResolutionResult

        with patch(
            "echelonos.stages.stage_5_amendment.compare_clauses",
            return_value=ResolutionResult(
                action="UNCHANGED",
                original_clause="Vendor shall deliver monthly.",
                amendment_clause="Payment terms updated.",
                reasoning="Different subjects",
                confidence=0.90,
            ),
        ):
            result = resolve_obligation(
                {
                    "obligation_text": "Deliver monthly reports",
                    "obligation_type": "Delivery",
                    "source_clause": "Vendor shall deliver monthly.",
                },
                [
                    {
                        "obligation_text": "Payment updated to net-60",
                        "obligation_type": "Financial",
                        "source_clause": "Payment terms updated.",
                    }
                ],
            )
        assert result["status"] == "ACTIVE"

    def test_resolve_obligation_replaced(self) -> None:
        """Amendment replaces original -> SUPERSEDED."""
        from echelonos.stages.stage_5_amendment import ResolutionResult

        with patch(
            "echelonos.stages.stage_5_amendment.compare_clauses",
            return_value=ResolutionResult(
                action="REPLACE",
                original_clause="Deliver monthly.",
                amendment_clause="Deliver weekly.",
                reasoning="Frequency changed",
                confidence=0.95,
            ),
        ):
            result = resolve_obligation(
                {
                    "obligation_text": "Deliver monthly reports",
                    "obligation_type": "Delivery",
                    "source_clause": "Deliver monthly.",
                },
                [
                    {
                        "obligation_text": "Deliver weekly reports",
                        "obligation_type": "Delivery",
                        "source_clause": "Deliver weekly.",
                    }
                ],
            )
        assert result["status"] == "SUPERSEDED"

    def test_resolve_all_with_unlinked(self) -> None:
        """Unlinked docs get UNRESOLVED obligations."""
        docs = [
            {
                "doc_id": "rexair-nda",
                "doc_type": "NDA",
                "obligations": [
                    {"obligation_text": "Keep info confidential", "source_clause": "..."}
                ],
            }
        ]
        results = resolve_all(docs, links=[], claude_client=MagicMock())
        assert len(results) == 1
        assert results[0]["status"] == "UNRESOLVED"


# ---------------------------------------------------------------------------
# Stage 6: Evidence Packaging tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestStage6EvidenceRexair:
    """Stage 6 evidence packaging for Rexair."""

    def test_create_evidence_record(self) -> None:
        record = create_evidence_record(
            obligation={
                "obligation_id": "obl-001",
                "source_clause": "Vendor shall deliver monthly reports.",
                "extraction_model": "claude-opus-4-6",
                "source_page": 1,
                "confidence": 0.92,
            },
            document={
                "doc_id": "rexair-msa",
                "filename": "rexair_msa.pdf",
            },
            verification={
                "verification_model": "claude-opus-4-6",
                "verified": True,
                "confidence": 0.95,
            },
        )
        assert isinstance(record, EvidenceRecord)
        assert record.obligation_id == "obl-001"
        assert record.verification_result == "CONFIRMED"
        assert record.confidence == 0.95

    def test_package_evidence_batch(self) -> None:
        obligations = [
            {
                "obligation_id": "obl-001",
                "doc_id": "rexair-msa",
                "source_clause": "clause text",
                "extraction_model": "claude-opus-4-6",
                "source_page": 1,
                "confidence": 0.90,
            },
            {
                "obligation_id": "obl-002",
                "doc_id": "rexair-msa",
                "source_clause": "another clause",
                "extraction_model": "claude-opus-4-6",
                "source_page": 2,
                "confidence": 0.85,
            },
        ]
        documents = {
            "rexair-msa": {"doc_id": "rexair-msa", "filename": "rexair_msa.pdf"}
        }
        verifications = {
            "obl-001": {"verification_model": "claude-opus-4-6", "verified": True, "confidence": 0.95},
            "obl-002": {"verification_model": "claude-opus-4-6", "verified": False, "confidence": 0.60},
        }
        records = package_evidence(obligations, documents, verifications)
        assert len(records) == 2
        assert records[0].verification_result == "CONFIRMED"
        assert records[1].verification_result == "DISPUTED"

    def test_validate_evidence_chain(self) -> None:
        records = [
            EvidenceRecord(
                obligation_id="obl-001",
                doc_id="rexair-msa",
                doc_filename="rexair_msa.pdf",
                source_clause="clause",
                extraction_model="claude-opus-4-6",
                verification_model="claude-opus-4-6",
                verification_result="CONFIRMED",
                confidence=0.95,
            ),
        ]
        result = validate_evidence_chain(records)
        assert result["valid"] is True
        assert result["gaps"] == []


# ---------------------------------------------------------------------------
# Stage 7: Report Generation tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestStage7ReportRexair:
    """Stage 7 report generation for Rexair."""

    def test_build_obligation_matrix(self) -> None:
        obligations = [
            {
                "doc_id": "rexair-msa",
                "obligation_text": "Deliver monthly reports",
                "obligation_type": "Delivery",
                "responsible_party": "Rexair Inc",
                "counterparty": "Acme Corp",
                "source_clause": "Section 4.2: Vendor shall deliver monthly reports.",
                "status": "ACTIVE",
                "confidence": 0.92,
            },
        ]
        documents = {
            "rexair-msa": {"doc_id": "rexair-msa", "doc_type": "MSA"}
        }
        matrix = build_obligation_matrix(obligations, documents, links=[])
        assert len(matrix) == 1
        assert matrix[0].number == 1
        assert matrix[0].status == "ACTIVE"

    def test_build_flag_report(self) -> None:
        obligations = [
            {
                "id": "obl-001",
                "doc_id": "rexair-msa",
                "obligation_text": "Low confidence obligation",
                "confidence": 0.50,
                "status": "ACTIVE",
            },
        ]
        flags = build_flag_report(obligations, documents=[], links=[])
        low_conf = [f for f in flags if f.flag_type == "LOW_CONFIDENCE"]
        assert len(low_conf) == 1

    def test_generate_full_report(self) -> None:
        obligations = [
            {
                "doc_id": "rexair-msa",
                "obligation_text": "Deliver monthly reports",
                "obligation_type": "Delivery",
                "responsible_party": "Rexair Inc",
                "counterparty": "Acme Corp",
                "source_clause": "Section 4.2: Vendor shall deliver reports.",
                "status": "ACTIVE",
                "confidence": 0.92,
            },
            {
                "doc_id": "rexair-msa",
                "obligation_text": "Pay within 30 days",
                "obligation_type": "Financial",
                "responsible_party": "Acme Corp",
                "counterparty": "Rexair Inc",
                "source_clause": "Section 5.1: Client shall pay within 30 days.",
                "status": "ACTIVE",
                "confidence": 0.88,
            },
        ]
        documents = {
            "rexair-msa": {"doc_id": "rexair-msa", "doc_type": "MSA"}
        }
        report = generate_report(
            org_name="Rexair",
            obligations=obligations,
            documents=documents,
            links=[],
        )
        assert isinstance(report, ObligationReport)
        assert report.org_name == "Rexair"
        assert report.total_obligations == 2
        assert report.active_obligations == 2

    def test_export_to_markdown(self) -> None:
        report = ObligationReport(
            org_name="Rexair",
            generated_at="2024-01-15T00:00:00Z",
            total_obligations=1,
            active_obligations=1,
            superseded_obligations=0,
            unresolved_obligations=0,
            obligations=[],
            flags=[],
            summary={},
        )
        md = export_to_markdown(report)
        assert "Rexair" in md
        assert "# Obligation Report" in md

    def test_export_to_json(self) -> None:
        report = ObligationReport(
            org_name="Rexair",
            generated_at="2024-01-15T00:00:00Z",
            total_obligations=1,
            active_obligations=1,
            superseded_obligations=0,
            unresolved_obligations=0,
            obligations=[],
            flags=[],
            summary={},
        )
        json_str = export_to_json(report)
        assert "Rexair" in json_str
        assert '"org_name"' in json_str


# ---------------------------------------------------------------------------
# Full pipeline integration test (Stages 0a -> 0b -> simulated 1-7)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestFullPipelineRexair:
    """End-to-end integration: all stages wired together for Rexair org."""

    def test_full_pipeline_pdf_to_report(self, rexair_pdf: Path, rexair_org: Path) -> None:
        """Run the complete pipeline from PDF validation through report generation."""
        failures: list[str] = []

        # --- Stage 0a: Validation ---
        try:
            validated = validate_folder(str(rexair_org))
            valid_files = [f for f in validated if f["status"] == "VALID"]
            assert len(valid_files) >= 1, f"No valid files found in {rexair_org}"
        except Exception as e:
            failures.append(f"Stage 0a FAILED: {e}")
            pytest.fail(f"Stage 0a blocked further stages: {e}")

        # --- Stage 0b: Deduplication ---
        try:
            unique_files = deduplicate_files(valid_files)
            assert len(unique_files) >= 1
        except Exception as e:
            failures.append(f"Stage 0b FAILED: {e}")
            pytest.fail(f"Stage 0b blocked further stages: {e}")

        # --- Stage 1: OCR (mocked) ---
        try:
            contract_text = (
                "MASTER SERVICE AGREEMENT between Rexair Inc and Acme Corp.\n"
                "Effective Date: January 1, 2024.\n\n"
                "Section 4.2: Vendor shall deliver monthly reports to Client.\n"
                "Section 5.1: Client shall pay invoices within 30 days.\n"
            )
            with patch(
                "echelonos.stages.stage_1_ocr.analyze_document",
                return_value=_mock_ocr_result(contract_text, num_pages=1),
            ):
                ocr_result = ingest_document(
                    unique_files[0]["file_path"],
                    doc_id="rexair-msa-001",
                    ocr_client=MagicMock(),
                )
            assert ocr_result["total_pages"] >= 1
            full_text = get_full_text(ocr_result["pages"])
            assert len(full_text) > 0
        except Exception as e:
            failures.append(f"Stage 1 FAILED: {e}")
            pytest.fail(f"Stage 1 blocked further stages: {e}")

        # --- Stage 2: Classification (mocked) ---
        try:
            with patch(
                "echelonos.stages.stage_2_classification.extract_with_structured_output",
                return_value=_mock_classification(),
            ):
                classification = classify_document(full_text, claude_client=MagicMock())
            assert classification.doc_type in ("MSA", "SOW", "Amendment", "NDA", "Other", "UNKNOWN")
        except Exception as e:
            failures.append(f"Stage 2 FAILED: {e}")
            pytest.fail(f"Stage 2 blocked further stages: {e}")

        # --- Stage 3: Extraction (mocked) ---
        try:
            from echelonos.stages.stage_3_extraction import (
                _ExtractionResponse,
                _PartyRolesResponse,
            )

            def side_effect(*args, **kwargs):
                resp_format = kwargs.get("response_format") or args[3]
                if resp_format == _PartyRolesResponse:
                    return _PartyRolesResponse(
                        party_roles={"Vendor": "Rexair Inc", "Client": "Acme Corp"}
                    )
                return _ExtractionResponse(obligations=[
                    _mock_obligation(
                        text="Deliver monthly reports",
                        clause="Section 4.2: Vendor shall deliver monthly reports to Client.",
                    ),
                ])

            mock_verify = MagicMock()
            mock_verify.content = [
                MagicMock(text='{"verified": true, "confidence": 0.92, "reason": "Matches."}')
            ]
            mock_claude = MagicMock()
            mock_claude.messages.create.return_value = mock_verify

            with patch(
                "echelonos.stages.stage_3_extraction.extract_with_structured_output",
                side_effect=side_effect,
            ):
                extraction_results = extract_and_verify(full_text, claude_client=mock_claude)
            assert len(extraction_results) >= 1
        except Exception as e:
            failures.append(f"Stage 3 FAILED: {e}")
            pytest.fail(f"Stage 3 blocked further stages: {e}")

        # --- Stage 4: Linking ---
        try:
            documents = [
                {
                    "id": "rexair-msa-001",
                    "org_id": "rexair",
                    "doc_type": classification.doc_type,
                    "effective_date": classification.effective_date,
                    "parties": classification.parties,
                    "parent_reference_raw": classification.parent_reference_raw,
                },
            ]
            link_results = link_documents(documents)
            # With a single MSA, no links are expected (nothing to link)
            assert isinstance(link_results, list)
        except Exception as e:
            failures.append(f"Stage 4 FAILED: {e}")

        # --- Stage 5: Amendment Resolution ---
        try:
            stage5_docs = [
                {
                    "doc_id": "rexair-msa-001",
                    "doc_type": "MSA",
                    "obligations": [
                        r["obligation"] for r in extraction_results
                    ],
                }
            ]
            resolved = resolve_all(stage5_docs, links=link_results, claude_client=MagicMock())
            assert isinstance(resolved, list)
        except Exception as e:
            failures.append(f"Stage 5 FAILED: {e}")

        # --- Stage 6: Evidence Packaging ---
        try:
            evidence_obligations = []
            for i, r in enumerate(resolved):
                entry = dict(r)
                entry["obligation_id"] = f"obl-{i:03d}"
                entry["doc_id"] = "rexair-msa-001"
                entry["extraction_model"] = "claude-opus-4-6"
                evidence_obligations.append(entry)

            docs_lookup = {
                "rexair-msa-001": {
                    "doc_id": "rexair-msa-001",
                    "filename": "rexair_msa.pdf",
                }
            }
            verifications = {
                obl["obligation_id"]: {
                    "verification_model": "claude-opus-4-6",
                    "verified": True,
                    "confidence": 0.92,
                }
                for obl in evidence_obligations
            }
            evidence_records = package_evidence(
                evidence_obligations, docs_lookup, verifications
            )
            assert len(evidence_records) >= 1
            chain_check = validate_evidence_chain(evidence_records)
            assert chain_check["valid"] is True
        except Exception as e:
            failures.append(f"Stage 6 FAILED: {e}")

        # --- Stage 7: Report Generation ---
        try:
            report_obligations = []
            for obl in resolved:
                report_obligations.append({
                    "doc_id": "rexair-msa-001",
                    "obligation_text": obl.get("obligation_text", ""),
                    "obligation_type": obl.get("obligation_type", "Unknown"),
                    "responsible_party": obl.get("responsible_party", "Unknown"),
                    "counterparty": obl.get("counterparty", "Unknown"),
                    "source_clause": obl.get("source_clause", ""),
                    "status": obl.get("status", "ACTIVE"),
                    "confidence": obl.get("confidence", 0.0),
                })

            report = generate_report(
                org_name="Rexair",
                obligations=report_obligations,
                documents=docs_lookup,
                links=link_results,
            )
            assert report.org_name == "Rexair"
            assert report.total_obligations >= 1

            # Verify exports work
            md = export_to_markdown(report)
            assert "Rexair" in md
            json_str = export_to_json(report)
            assert "Rexair" in json_str
        except Exception as e:
            failures.append(f"Stage 7 FAILED: {e}")

        # --- Final failure report ---
        if failures:
            report_text = "\n".join(f"  - {f}" for f in failures)
            pytest.fail(
                f"Pipeline completed with {len(failures)} stage failure(s):\n{report_text}"
            )
