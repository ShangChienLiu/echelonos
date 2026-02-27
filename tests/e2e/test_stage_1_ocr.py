"""E2E tests for Stage 1: Document Ingestion / OCR.

Azure Document Intelligence is an external service, so all tests mock the
Azure client with realistic response structures matching the actual API.
"""

from unittest.mock import MagicMock, patch

import pytest
from azure.core.exceptions import HttpResponseError, ServiceRequestError

from echelonos.stages.stage_1_ocr import get_full_text, ingest_document


# ---------------------------------------------------------------------------
# Mock helpers -- realistic Azure Document Intelligence responses
# ---------------------------------------------------------------------------


def _make_span(confidence: float = 0.95):
    """Create a mock span object with a confidence score."""
    span = MagicMock()
    span.confidence = confidence
    return span


def _make_page(page_number: int, confidence: float = 0.95):
    """Create a mock Azure page object."""
    page = MagicMock()
    page.page_number = page_number
    page.spans = [_make_span(confidence)]
    return page


def _make_paragraph(content: str, page_number: int):
    """Create a mock Azure paragraph object."""
    para = MagicMock()
    para.content = content
    region = MagicMock()
    region.page_number = page_number
    para.bounding_regions = [region]
    return para


def _make_table_cell(row_index: int, column_index: int, content: str):
    """Create a mock Azure table cell object."""
    cell = MagicMock()
    cell.row_index = row_index
    cell.column_index = column_index
    cell.content = content
    return cell


def _make_table(cells: list, column_count: int, page_number: int):
    """Create a mock Azure table object."""
    table = MagicMock()
    table.cells = cells
    table.column_count = column_count
    region = MagicMock()
    region.page_number = page_number
    table.bounding_regions = [region]
    return table


def _build_azure_result(
    pages: list[MagicMock],
    paragraphs: list[MagicMock] | None = None,
    tables: list[MagicMock] | None = None,
    content: str = "Document content",
):
    """Assemble a mock Azure AnalyzeResult object."""
    result = MagicMock()
    result.pages = pages
    result.paragraphs = paragraphs
    result.tables = tables
    result.content = content
    return result


def _make_mock_client(azure_result):
    """Create a mock DocumentIntelligenceClient that returns *azure_result*."""
    client = MagicMock()
    poller = MagicMock()
    poller.result.return_value = azure_result
    client.begin_analyze_document.return_value = poller
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIngestDocumentSuccess:
    """Happy-path tests for ingest_document()."""

    def test_ingest_document_success(self, tmp_path) -> None:
        """Mock Azure client returning 3 pages with text -- verify structure."""
        # Create a dummy PDF file on disk (the mock client ignores its contents).
        pdf = tmp_path / "contract.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        pages = [
            _make_page(1, 0.98),
            _make_page(2, 0.95),
            _make_page(3, 0.92),
        ]
        paragraphs = [
            _make_paragraph("MASTER SERVICE AGREEMENT", 1),
            _make_paragraph("This Agreement is entered into as of January 1, 2025.", 1),
            _make_paragraph("Section 2: Scope of Services", 2),
            _make_paragraph("The Contractor shall provide the following services.", 2),
            _make_paragraph("Section 3: Payment Terms", 3),
            _make_paragraph("Payment shall be made within 30 days of invoice.", 3),
        ]
        azure_result = _build_azure_result(pages, paragraphs=paragraphs)
        mock_client = _make_mock_client(azure_result)

        result = ingest_document(str(pdf), doc_id="doc-001", azure_client=mock_client)

        # Structure checks.
        assert result["doc_id"] == "doc-001"
        assert result["total_pages"] == 3
        assert len(result["pages"]) == 3
        assert "flags" in result

        # Per-page content checks.
        p1 = result["pages"][0]
        assert p1["page_number"] == 1
        assert "MASTER SERVICE AGREEMENT" in p1["text"]
        assert p1["ocr_confidence"] == 0.98

        p2 = result["pages"][1]
        assert p2["page_number"] == 2
        assert "Scope of Services" in p2["text"]

        p3 = result["pages"][2]
        assert p3["page_number"] == 3
        assert "Payment" in p3["text"]

        # High confidence -- no flags expected.
        assert result["flags"] == []


class TestIngestDocumentWithTables:
    """Tests verifying markdown table preservation."""

    def test_ingest_document_with_tables(self, tmp_path) -> None:
        """Tables should be converted to markdown and placed on the correct page."""
        pdf = tmp_path / "contract_with_tables.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        pages = [_make_page(1, 0.96), _make_page(2, 0.94)]
        paragraphs = [
            _make_paragraph("Pricing Schedule", 1),
            _make_paragraph("Additional terms apply.", 2),
        ]

        # A 2-row, 3-column table on page 1.
        table_cells = [
            _make_table_cell(0, 0, "Item"),
            _make_table_cell(0, 1, "Quantity"),
            _make_table_cell(0, 2, "Price"),
            _make_table_cell(1, 0, "Widget A"),
            _make_table_cell(1, 1, "100"),
            _make_table_cell(1, 2, "$5.00"),
        ]
        table = _make_table(table_cells, column_count=3, page_number=1)

        azure_result = _build_azure_result(pages, paragraphs=paragraphs, tables=[table])
        mock_client = _make_mock_client(azure_result)

        result = ingest_document(str(pdf), doc_id="doc-002", azure_client=mock_client)

        p1 = result["pages"][0]
        # Table markdown should be present.
        assert p1["tables_markdown"] != ""
        assert "Item" in p1["tables_markdown"]
        assert "Widget A" in p1["tables_markdown"]
        assert "Quantity" in p1["tables_markdown"]
        assert "$5.00" in p1["tables_markdown"]
        # Should contain markdown table pipe characters.
        assert "|" in p1["tables_markdown"]
        # Separator row should be present (--- pattern after header row).
        assert "---" in p1["tables_markdown"]

        # Page 2 should have no tables.
        p2 = result["pages"][1]
        assert p2["tables_markdown"] == ""

    def test_multiple_tables_on_same_page(self, tmp_path) -> None:
        """Multiple tables on the same page should both appear in tables_markdown."""
        pdf = tmp_path / "multi_table.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        pages = [_make_page(1, 0.93)]
        paragraphs = [_make_paragraph("Overview", 1)]

        table1_cells = [
            _make_table_cell(0, 0, "Name"),
            _make_table_cell(0, 1, "Role"),
            _make_table_cell(1, 0, "Alice"),
            _make_table_cell(1, 1, "Manager"),
        ]
        table1 = _make_table(table1_cells, column_count=2, page_number=1)

        table2_cells = [
            _make_table_cell(0, 0, "Milestone"),
            _make_table_cell(0, 1, "Date"),
            _make_table_cell(1, 0, "Kickoff"),
            _make_table_cell(1, 1, "2025-03-01"),
        ]
        table2 = _make_table(table2_cells, column_count=2, page_number=1)

        azure_result = _build_azure_result(
            pages, paragraphs=paragraphs, tables=[table1, table2]
        )
        mock_client = _make_mock_client(azure_result)

        result = ingest_document(str(pdf), doc_id="doc-003", azure_client=mock_client)

        p1 = result["pages"][0]
        assert "Alice" in p1["tables_markdown"]
        assert "Kickoff" in p1["tables_markdown"]


class TestGetFullText:
    """Tests for get_full_text()."""

    def test_get_full_text(self) -> None:
        """Concatenation produces a single string with form-feed separators."""
        pages = [
            {
                "page_number": 1,
                "text": "Page one content.",
                "tables_markdown": "",
                "ocr_confidence": 0.95,
            },
            {
                "page_number": 2,
                "text": "Page two content.",
                "tables_markdown": "| A | B |\n| --- | --- |\n| 1 | 2 |",
                "ocr_confidence": 0.90,
            },
            {
                "page_number": 3,
                "text": "Page three content.",
                "tables_markdown": "",
                "ocr_confidence": 0.88,
            },
        ]

        full = get_full_text(pages)

        assert "Page one content." in full
        assert "Page two content." in full
        assert "Page three content." in full
        # Table markdown should also appear.
        assert "| A | B |" in full
        # Form-feed separators between pages.
        assert full.count("\f") == 2

    def test_get_full_text_single_page(self) -> None:
        """A single page produces output with no form-feed."""
        pages = [
            {
                "page_number": 1,
                "text": "Only page.",
                "tables_markdown": "",
                "ocr_confidence": 0.99,
            },
        ]

        full = get_full_text(pages)
        assert full == "Only page."
        assert "\f" not in full

    def test_get_full_text_empty(self) -> None:
        """An empty page list produces an empty string."""
        assert get_full_text([]) == ""

    def test_get_full_text_with_tables_only(self) -> None:
        """Pages with only tables (no paragraph text) still work."""
        pages = [
            {
                "page_number": 1,
                "text": "",
                "tables_markdown": "| X |\n| --- |\n| 1 |",
                "ocr_confidence": 0.90,
            },
        ]

        full = get_full_text(pages)
        assert "| X |" in full


class TestConfidenceFlagging:
    """Tests for the OCR confidence quality gate."""

    def test_low_confidence_flagging(self, tmp_path) -> None:
        """Pages below 0.60 confidence must be flagged as LOW_OCR_QUALITY."""
        pdf = tmp_path / "low_quality.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        pages = [
            _make_page(1, 0.45),  # LOW
            _make_page(2, 0.92),  # OK
            _make_page(3, 0.30),  # LOW
        ]
        paragraphs = [
            _make_paragraph("Blurry text", 1),
            _make_paragraph("Clear text", 2),
            _make_paragraph("Illegible text", 3),
        ]
        azure_result = _build_azure_result(pages, paragraphs=paragraphs)
        mock_client = _make_mock_client(azure_result)

        result = ingest_document(str(pdf), doc_id="doc-low", azure_client=mock_client)

        low_flags = [f for f in result["flags"] if f["flag_type"] == "LOW_OCR_QUALITY"]
        assert len(low_flags) == 2

        flagged_pages = sorted(f["page_number"] for f in low_flags)
        assert flagged_pages == [1, 3]

        # Messages should include the confidence value.
        for flag in low_flags:
            assert "below the minimum threshold" in flag["message"]

    def test_medium_confidence_warning(self, tmp_path) -> None:
        """Pages between 0.60 and 0.85 must be flagged as MEDIUM_OCR_QUALITY."""
        pdf = tmp_path / "medium_quality.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        pages = [
            _make_page(1, 0.70),  # MEDIUM
            _make_page(2, 0.75),  # MEDIUM
            _make_page(3, 0.92),  # OK
        ]
        paragraphs = [
            _make_paragraph("Somewhat blurry", 1),
            _make_paragraph("Slightly blurry", 2),
            _make_paragraph("Clear", 3),
        ]
        azure_result = _build_azure_result(pages, paragraphs=paragraphs)
        mock_client = _make_mock_client(azure_result)

        result = ingest_document(str(pdf), doc_id="doc-med", azure_client=mock_client)

        medium_flags = [f for f in result["flags"] if f["flag_type"] == "MEDIUM_OCR_QUALITY"]
        assert len(medium_flags) == 2

        flagged_pages = sorted(f["page_number"] for f in medium_flags)
        assert flagged_pages == [1, 2]

        for flag in medium_flags:
            assert "below the recommended threshold" in flag["message"]

    def test_mixed_confidence_levels(self, tmp_path) -> None:
        """A document with LOW, MEDIUM, and HIGH confidence pages."""
        pdf = tmp_path / "mixed.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        pages = [
            _make_page(1, 0.40),  # LOW
            _make_page(2, 0.72),  # MEDIUM
            _make_page(3, 0.95),  # HIGH (no flag)
        ]
        paragraphs = [
            _make_paragraph("Bad scan", 1),
            _make_paragraph("Okay scan", 2),
            _make_paragraph("Good scan", 3),
        ]
        azure_result = _build_azure_result(pages, paragraphs=paragraphs)
        mock_client = _make_mock_client(azure_result)

        result = ingest_document(str(pdf), doc_id="doc-mix", azure_client=mock_client)

        assert len(result["flags"]) == 2

        low = [f for f in result["flags"] if f["flag_type"] == "LOW_OCR_QUALITY"]
        medium = [f for f in result["flags"] if f["flag_type"] == "MEDIUM_OCR_QUALITY"]
        assert len(low) == 1
        assert low[0]["page_number"] == 1
        assert len(medium) == 1
        assert medium[0]["page_number"] == 2

    def test_exactly_at_low_threshold(self, tmp_path) -> None:
        """A page at exactly 0.60 should NOT be LOW -- it falls into MEDIUM."""
        pdf = tmp_path / "boundary.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        pages = [_make_page(1, 0.60)]
        paragraphs = [_make_paragraph("Boundary", 1)]
        azure_result = _build_azure_result(pages, paragraphs=paragraphs)
        mock_client = _make_mock_client(azure_result)

        result = ingest_document(str(pdf), doc_id="doc-boundary", azure_client=mock_client)

        low = [f for f in result["flags"] if f["flag_type"] == "LOW_OCR_QUALITY"]
        medium = [f for f in result["flags"] if f["flag_type"] == "MEDIUM_OCR_QUALITY"]
        assert len(low) == 0
        assert len(medium) == 1

    def test_exactly_at_medium_threshold(self, tmp_path) -> None:
        """A page at exactly 0.85 should NOT be flagged at all."""
        pdf = tmp_path / "boundary_high.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        pages = [_make_page(1, 0.85)]
        paragraphs = [_make_paragraph("Clear enough", 1)]
        azure_result = _build_azure_result(pages, paragraphs=paragraphs)
        mock_client = _make_mock_client(azure_result)

        result = ingest_document(str(pdf), doc_id="doc-boundary2", azure_client=mock_client)

        assert result["flags"] == []


class TestEmptyDocument:
    """Tests for edge cases with empty or zero-page documents."""

    def test_empty_document(self, tmp_path) -> None:
        """A document with no pages should return an empty pages list."""
        pdf = tmp_path / "empty.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        azure_result = _build_azure_result(pages=[], paragraphs=[], content="")
        mock_client = _make_mock_client(azure_result)

        result = ingest_document(str(pdf), doc_id="doc-empty", azure_client=mock_client)

        assert result["doc_id"] == "doc-empty"
        assert result["pages"] == []
        assert result["total_pages"] == 0
        assert result["flags"] == []


class TestAzureApiErrorHandling:
    """Tests for graceful handling of Azure API errors."""

    def test_http_response_error(self, tmp_path) -> None:
        """An HttpResponseError should be caught and returned as an OCR_ERROR flag."""
        pdf = tmp_path / "error.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        mock_client = MagicMock()
        mock_client.begin_analyze_document.side_effect = HttpResponseError(
            message="Service temporarily unavailable"
        )

        # Patch tenacity to avoid retries in tests.
        with patch(
            "echelonos.stages.stage_1_ocr._call_azure",
            side_effect=HttpResponseError(message="Service temporarily unavailable"),
        ):
            result = ingest_document(str(pdf), doc_id="doc-err", azure_client=mock_client)

        assert result["doc_id"] == "doc-err"
        assert result["pages"] == []
        assert result["total_pages"] == 0
        assert len(result["flags"]) == 1
        assert result["flags"][0]["flag_type"] == "OCR_ERROR"
        assert "Azure Document Intelligence API error" in result["flags"][0]["message"]

    def test_service_request_error(self, tmp_path) -> None:
        """A ServiceRequestError (network issue) should also be handled gracefully."""
        pdf = tmp_path / "network_error.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        mock_client = MagicMock()

        with patch(
            "echelonos.stages.stage_1_ocr._call_azure",
            side_effect=ServiceRequestError(message="Connection timed out"),
        ):
            result = ingest_document(str(pdf), doc_id="doc-net", azure_client=mock_client)

        assert result["doc_id"] == "doc-net"
        assert result["pages"] == []
        assert result["total_pages"] == 0
        assert len(result["flags"]) == 1
        assert result["flags"][0]["flag_type"] == "OCR_ERROR"

    def test_unexpected_error(self, tmp_path) -> None:
        """An unexpected exception is caught and reported as OCR_ERROR."""
        pdf = tmp_path / "unexpected.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        mock_client = MagicMock()

        with patch(
            "echelonos.stages.stage_1_ocr._call_azure",
            side_effect=RuntimeError("Something went wrong"),
        ):
            result = ingest_document(str(pdf), doc_id="doc-unk", azure_client=mock_client)

        assert result["doc_id"] == "doc-unk"
        assert result["pages"] == []
        assert result["total_pages"] == 0
        assert len(result["flags"]) == 1
        assert result["flags"][0]["flag_type"] == "OCR_ERROR"
        assert "Unexpected OCR error" in result["flags"][0]["message"]

    def test_error_result_shape_matches_success(self, tmp_path) -> None:
        """Error results should have the same top-level keys as success results."""
        pdf = tmp_path / "shape.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        mock_client = MagicMock()

        with patch(
            "echelonos.stages.stage_1_ocr._call_azure",
            side_effect=HttpResponseError(message="Boom"),
        ):
            error_result = ingest_document(str(pdf), doc_id="doc-shape", azure_client=mock_client)

        expected_keys = {"doc_id", "pages", "total_pages", "flags"}
        assert set(error_result.keys()) == expected_keys
