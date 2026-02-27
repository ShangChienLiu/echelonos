"""E2E tests for Stage 1: Document Ingestion / OCR.

Mistral OCR is an external service, so all tests mock the Mistral client
with realistic response structures matching the actual API.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from echelonos.stages.stage_1_ocr import get_full_text, ingest_document


# ---------------------------------------------------------------------------
# Mock helpers -- realistic Mistral OCR responses
# ---------------------------------------------------------------------------


def _make_page(index: int, markdown: str):
    """Create a mock Mistral OCR page object.

    Mistral pages use 0-indexed ``index`` and return content as ``markdown``.
    """
    return SimpleNamespace(index=index, markdown=markdown)


def _build_mistral_result(pages: list):
    """Assemble a mock Mistral OCR response object."""
    return SimpleNamespace(pages=pages)


def _make_mock_client(mistral_result):
    """Create a mock Mistral client whose ``ocr.process()`` returns *mistral_result*."""
    client = MagicMock()
    client.ocr.process.return_value = mistral_result
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIngestDocumentSuccess:
    """Happy-path tests for ingest_document()."""

    def test_ingest_document_success(self, tmp_path) -> None:
        """Mock Mistral client returning 3 pages with text -- verify structure."""
        pdf = tmp_path / "contract.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        pages = [
            _make_page(0, "# MASTER SERVICE AGREEMENT\n\nThis Agreement is entered into as of January 1, 2025.\n"),
            _make_page(1, "## Section 2: Scope of Services\n\nThe Contractor shall provide the following services.\n"),
            _make_page(2, "## Section 3: Payment Terms\n\nPayment shall be made within 30 days of invoice.\n"),
        ]
        mistral_result = _build_mistral_result(pages)
        mock_client = _make_mock_client(mistral_result)

        result = ingest_document(str(pdf), doc_id="doc-001", ocr_client=mock_client)

        # Structure checks.
        assert result["doc_id"] == "doc-001"
        assert result["total_pages"] == 3
        assert len(result["pages"]) == 3
        assert "flags" in result

        # Per-page content checks.
        p1 = result["pages"][0]
        assert p1["page_number"] == 1
        assert "MASTER SERVICE AGREEMENT" in p1["text"]
        assert p1["ocr_confidence"] == 0.95

        p2 = result["pages"][1]
        assert p2["page_number"] == 2
        assert "Scope of Services" in p2["text"]

        p3 = result["pages"][2]
        assert p3["page_number"] == 3
        assert "Payment" in p3["text"]

        # High confidence (default 0.95) -- no flags expected.
        assert result["flags"] == []


class TestIngestDocumentWithTables:
    """Tests verifying markdown table preservation."""

    def test_ingest_document_with_tables(self, tmp_path) -> None:
        """Tables should be separated from text and placed on the correct page."""
        pdf = tmp_path / "contract_with_tables.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        page1_md = (
            "Pricing Schedule\n\n"
            "| Item | Quantity | Price |\n"
            "| --- | --- | --- |\n"
            "| Widget A | 100 | $5.00 |\n"
        )
        page2_md = "Additional terms apply.\n"

        pages = [_make_page(0, page1_md), _make_page(1, page2_md)]
        mistral_result = _build_mistral_result(pages)
        mock_client = _make_mock_client(mistral_result)

        result = ingest_document(str(pdf), doc_id="doc-002", ocr_client=mock_client)

        p1 = result["pages"][0]
        # Table markdown should be present.
        assert p1["tables_markdown"] != ""
        assert "Item" in p1["tables_markdown"]
        assert "Widget A" in p1["tables_markdown"]
        assert "Quantity" in p1["tables_markdown"]
        assert "$5.00" in p1["tables_markdown"]
        # Should contain markdown table pipe characters.
        assert "|" in p1["tables_markdown"]
        # Separator row should be present.
        assert "---" in p1["tables_markdown"]

        # Page 2 should have no tables.
        p2 = result["pages"][1]
        assert p2["tables_markdown"] == ""

    def test_multiple_tables_on_same_page(self, tmp_path) -> None:
        """Multiple tables on the same page should both appear in tables_markdown."""
        pdf = tmp_path / "multi_table.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        page_md = (
            "Overview\n\n"
            "| Name | Role |\n"
            "| --- | --- |\n"
            "| Alice | Manager |\n"
            "\n"
            "Milestones:\n\n"
            "| Milestone | Date |\n"
            "| --- | --- |\n"
            "| Kickoff | 2025-03-01 |\n"
        )
        pages = [_make_page(0, page_md)]
        mistral_result = _build_mistral_result(pages)
        mock_client = _make_mock_client(mistral_result)

        result = ingest_document(str(pdf), doc_id="doc-003", ocr_client=mock_client)

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

        # Return pages with varying confidence via the raw page dict,
        # simulating what _build_page_result would consume.
        # We mock _call_mistral to return raw data with custom confidence.
        raw_result = {
            "pages": [
                {"page_number": 1, "text": "Blurry text\n", "tables": [], "confidence": 0.45},
                {"page_number": 2, "text": "Clear text\n", "tables": [], "confidence": 0.92},
                {"page_number": 3, "text": "Illegible text\n", "tables": [], "confidence": 0.30},
            ],
            "total_pages": 3,
        }

        with patch("echelonos.stages.stage_1_ocr._call_mistral", return_value=raw_result):
            result = ingest_document(str(pdf), doc_id="doc-low", ocr_client=MagicMock())

        low_flags = [f for f in result["flags"] if f["flag_type"] == "LOW_OCR_QUALITY"]
        assert len(low_flags) == 2

        flagged_pages = sorted(f["page_number"] for f in low_flags)
        assert flagged_pages == [1, 3]

        for flag in low_flags:
            assert "below the minimum threshold" in flag["message"]

    def test_medium_confidence_warning(self, tmp_path) -> None:
        """Pages between 0.60 and 0.85 must be flagged as MEDIUM_OCR_QUALITY."""
        pdf = tmp_path / "medium_quality.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        raw_result = {
            "pages": [
                {"page_number": 1, "text": "Somewhat blurry\n", "tables": [], "confidence": 0.70},
                {"page_number": 2, "text": "Slightly blurry\n", "tables": [], "confidence": 0.75},
                {"page_number": 3, "text": "Clear\n", "tables": [], "confidence": 0.92},
            ],
            "total_pages": 3,
        }

        with patch("echelonos.stages.stage_1_ocr._call_mistral", return_value=raw_result):
            result = ingest_document(str(pdf), doc_id="doc-med", ocr_client=MagicMock())

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

        raw_result = {
            "pages": [
                {"page_number": 1, "text": "Bad scan\n", "tables": [], "confidence": 0.40},
                {"page_number": 2, "text": "Okay scan\n", "tables": [], "confidence": 0.72},
                {"page_number": 3, "text": "Good scan\n", "tables": [], "confidence": 0.95},
            ],
            "total_pages": 3,
        }

        with patch("echelonos.stages.stage_1_ocr._call_mistral", return_value=raw_result):
            result = ingest_document(str(pdf), doc_id="doc-mix", ocr_client=MagicMock())

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

        raw_result = {
            "pages": [
                {"page_number": 1, "text": "Boundary\n", "tables": [], "confidence": 0.60},
            ],
            "total_pages": 1,
        }

        with patch("echelonos.stages.stage_1_ocr._call_mistral", return_value=raw_result):
            result = ingest_document(str(pdf), doc_id="doc-boundary", ocr_client=MagicMock())

        low = [f for f in result["flags"] if f["flag_type"] == "LOW_OCR_QUALITY"]
        medium = [f for f in result["flags"] if f["flag_type"] == "MEDIUM_OCR_QUALITY"]
        assert len(low) == 0
        assert len(medium) == 1

    def test_exactly_at_medium_threshold(self, tmp_path) -> None:
        """A page at exactly 0.85 should NOT be flagged at all."""
        pdf = tmp_path / "boundary_high.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        raw_result = {
            "pages": [
                {"page_number": 1, "text": "Clear enough\n", "tables": [], "confidence": 0.85},
            ],
            "total_pages": 1,
        }

        with patch("echelonos.stages.stage_1_ocr._call_mistral", return_value=raw_result):
            result = ingest_document(str(pdf), doc_id="doc-boundary2", ocr_client=MagicMock())

        assert result["flags"] == []


class TestEmptyDocument:
    """Tests for edge cases with empty or zero-page documents."""

    def test_empty_document(self, tmp_path) -> None:
        """A document with no pages should return an empty pages list."""
        pdf = tmp_path / "empty.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        mistral_result = _build_mistral_result(pages=[])
        mock_client = _make_mock_client(mistral_result)

        result = ingest_document(str(pdf), doc_id="doc-empty", ocr_client=mock_client)

        assert result["doc_id"] == "doc-empty"
        assert result["pages"] == []
        assert result["total_pages"] == 0
        assert result["flags"] == []


class TestMistralApiErrorHandling:
    """Tests for graceful handling of Mistral API errors."""

    def test_connection_error(self, tmp_path) -> None:
        """A ConnectionError should be caught and returned as an OCR_ERROR flag."""
        pdf = tmp_path / "error.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        with patch(
            "echelonos.stages.stage_1_ocr._call_mistral",
            side_effect=ConnectionError("Service temporarily unavailable"),
        ):
            result = ingest_document(str(pdf), doc_id="doc-err", ocr_client=MagicMock())

        assert result["doc_id"] == "doc-err"
        assert result["pages"] == []
        assert result["total_pages"] == 0
        assert len(result["flags"]) == 1
        assert result["flags"][0]["flag_type"] == "OCR_ERROR"
        assert "Mistral OCR API error" in result["flags"][0]["message"]

    def test_timeout_error(self, tmp_path) -> None:
        """A TimeoutError should also be handled gracefully."""
        pdf = tmp_path / "network_error.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        with patch(
            "echelonos.stages.stage_1_ocr._call_mistral",
            side_effect=TimeoutError("Connection timed out"),
        ):
            result = ingest_document(str(pdf), doc_id="doc-net", ocr_client=MagicMock())

        assert result["doc_id"] == "doc-net"
        assert result["pages"] == []
        assert result["total_pages"] == 0
        assert len(result["flags"]) == 1
        assert result["flags"][0]["flag_type"] == "OCR_ERROR"

    def test_unexpected_error(self, tmp_path) -> None:
        """An unexpected exception is caught and reported as OCR_ERROR."""
        pdf = tmp_path / "unexpected.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        with patch(
            "echelonos.stages.stage_1_ocr._call_mistral",
            side_effect=RuntimeError("Something went wrong"),
        ):
            result = ingest_document(str(pdf), doc_id="doc-unk", ocr_client=MagicMock())

        assert result["doc_id"] == "doc-unk"
        assert result["pages"] == []
        assert result["total_pages"] == 0
        assert len(result["flags"]) == 1
        assert result["flags"][0]["flag_type"] == "OCR_ERROR"
        assert "Mistral OCR API error" in result["flags"][0]["message"]

    def test_error_result_shape_matches_success(self, tmp_path) -> None:
        """Error results should have the same top-level keys as success results."""
        pdf = tmp_path / "shape.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        with patch(
            "echelonos.stages.stage_1_ocr._call_mistral",
            side_effect=ConnectionError("Boom"),
        ):
            error_result = ingest_document(str(pdf), doc_id="doc-shape", ocr_client=MagicMock())

        expected_keys = {"doc_id", "pages", "total_pages", "flags"}
        assert set(error_result.keys()) == expected_keys
