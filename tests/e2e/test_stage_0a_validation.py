"""E2E tests for Stage 0a: File Validation Gate (expanded).

Each test exercises the public API of stage_0a_validation against real (or
realistically crafted) files on disk, using the shared fixtures from
conftest.py where appropriate.  Internal helpers (_classify_format, etc.)
are also tested directly.
"""

import os
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from echelonos.stages.stage_0a_validation import (
    convert_to_pdf,
    validate_file,
    validate_folder,
)

# Internal helpers — imported for direct unit testing.  These may not exist
# yet in the module; tests that reference them are guarded with importability
# checks or mocks so the suite degrades gracefully.
try:
    from echelonos.stages.stage_0a_validation import _classify_format
except ImportError:
    _classify_format = None


try:
    from echelonos.stages.stage_0a_validation import _extract_html_text
except ImportError:
    _extract_html_text = None

try:
    from echelonos.stages.stage_0a_validation import _extract_xlsx_tables
except ImportError:
    _extract_xlsx_tables = None

try:
    from echelonos.stages.stage_0a_validation import _extract_zip_contents
except ImportError:
    _extract_zip_contents = None

# Re-export the minimal PNG bytes for convenience in image tests.
from tests.conftest import MINIMAL_JPG, MINIMAL_PNG

# ---------------------------------------------------------------------------
# Expected output fields (the new 7-key schema)
# ---------------------------------------------------------------------------

EXPECTED_RESULT_KEYS = {
    "file_path",
    "status",
    "reason",
    "original_format",
    "needs_ocr",
    "extracted_from",
    "child_files",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_text_file(folder: Path, name: str, content: str = "hello") -> Path:
    """Create a plain-text file inside *folder*."""
    p = folder / name
    p.write_text(content)
    return p


def _write_csv_file(folder: Path, name: str = "data.csv") -> Path:
    p = folder / name
    p.write_text("col1,col2\n1,2\n3,4\n")
    return p


def _write_random_bytes(folder: Path, name: str, size: int = 512) -> Path:
    """Write *size* random bytes with the given filename."""
    p = folder / name
    p.write_bytes(os.urandom(size))
    return p


def _assert_result_schema(result: dict) -> None:
    """Assert that *result* contains all expected output keys."""
    missing = EXPECTED_RESULT_KEYS - set(result.keys())
    assert not missing, f"Result is missing keys: {missing}"


# ---------------------------------------------------------------------------
# 1. TestValidateFilePdf
# ---------------------------------------------------------------------------


class TestValidateFilePdf:
    """Tests for validate_file() with PDF inputs."""

    def test_valid_pdf_with_text_passes(self, sample_pdf: Path) -> None:
        """A PDF with extractable text content must return VALID, needs_ocr=True.

        All PDFs are sent to OCR unconditionally.
        """
        result = validate_file(str(sample_pdf))

        assert result["status"] == "VALID"
        assert result["original_format"] == "PDF"
        assert result["file_path"] == str(sample_pdf)
        assert result["needs_ocr"] is True
        assert result["reason"]  # non-empty string

    def test_image_only_pdf_needs_ocr(self, tmp_org_folder: Path) -> None:
        """An image-only PDF (no extractable text) must return VALID, needs_ocr=True.

        We mock PdfReader so that pages return no extractable text.
        """
        pdf_path = tmp_org_folder / "image_only.pdf"
        # Write a minimal PDF that libmagic identifies as application/pdf.
        pdf_content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>
endobj
xref
0 4
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
trailer
<< /Size 4 /Root 1 0 R >>
startxref
190
%%EOF"""
        pdf_path.write_bytes(pdf_content)

        # Build a mock reader whose pages return empty text.
        mock_page = MagicMock()
        mock_page.extract_text.return_value = ""

        mock_reader = MagicMock()
        mock_reader.is_encrypted = False
        mock_reader.pages = [mock_page]

        with patch(
            "echelonos.stages.stage_0a_validation.PdfReader",
            return_value=mock_reader,
        ):
            result = validate_file(str(pdf_path))

        assert result["status"] == "VALID"
        assert result["original_format"] == "PDF"
        assert result["needs_ocr"] is True

    def test_zero_byte_file_rejected(self, zero_byte_file: Path) -> None:
        """A zero-byte file must be INVALID with expanded schema fields."""
        result = validate_file(str(zero_byte_file))

        assert result["status"] == "INVALID"
        assert "zero bytes" in result["reason"].lower()
        # Verify new schema fields are present.
        assert "needs_ocr" in result
        assert "extracted_from" in result
        assert "child_files" in result

    def test_nonexistent_file_rejected(self, tmp_path: Path) -> None:
        """A path that does not exist on disk must be INVALID."""
        fake = tmp_path / "does_not_exist.pdf"
        result = validate_file(str(fake))

        assert result["status"] == "INVALID"
        assert "not exist" in result["reason"].lower()

    def test_corrupted_pdf_rejected(self, tmp_org_folder: Path) -> None:
        """Random bytes disguised as a .pdf must be rejected.

        libmagic detects random bytes as application/octet-stream (not
        application/pdf), so the file is REJECTED rather than INVALID.
        """
        bad_pdf = _write_random_bytes(tmp_org_folder, "garbage.pdf")
        result = validate_file(str(bad_pdf))

        assert result["status"] in ("INVALID", "REJECTED")

    def test_password_protected_pdf(self, tmp_org_folder: Path) -> None:
        """A password-protected PDF must return NEEDS_PASSWORD.

        We mock PdfReader to simulate an encrypted file that cannot be
        decrypted with an empty password.
        """
        pdf_path = tmp_org_folder / "encrypted.pdf"
        pdf_content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>
endobj
xref
0 4
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
trailer
<< /Size 4 /Root 1 0 R >>
startxref
190
%%EOF"""
        pdf_path.write_bytes(pdf_content)

        class _MockReader:
            is_encrypted = True
            pages = []

            def decrypt(self, password: str) -> None:
                raise Exception("Invalid password")

        with patch(
            "echelonos.stages.stage_0a_validation.PdfReader",
            return_value=_MockReader(),
        ):
            result = validate_file(str(pdf_path))

        assert result["status"] == "NEEDS_PASSWORD"
        assert result["original_format"] == "PDF"
        assert "password" in result["reason"].lower()


# ---------------------------------------------------------------------------
# 2. TestValidateFileDocx
# ---------------------------------------------------------------------------


class TestValidateFileDocx:
    """Tests for validate_file() with DOCX inputs."""

    def test_valid_docx_passes(self, sample_docx: Path) -> None:
        """A valid DOCX produced by python-docx must return VALID."""
        result = validate_file(str(sample_docx))

        assert result["status"] == "VALID"
        assert result["original_format"] == "DOCX"
        assert result["needs_ocr"] is False

    def test_corrupted_docx_rejected(self, tmp_org_folder: Path) -> None:
        """Random bytes disguised as a .docx must be rejected.

        libmagic detects random bytes as application/octet-stream, so the
        file is REJECTED rather than INVALID.
        """
        bad_docx = _write_random_bytes(tmp_org_folder, "garbage.docx")
        result = validate_file(str(bad_docx))

        assert result["status"] in ("INVALID", "REJECTED")


# ---------------------------------------------------------------------------
# 3. TestValidateFileHtml
# ---------------------------------------------------------------------------


class TestValidateFileHtml:
    """Tests for validate_file() with HTML inputs."""

    def test_valid_html_passes(self, sample_html: Path) -> None:
        """HTML with meaningful text content must return VALID, original_format='HTML'."""
        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="text/html",
        ):
            result = validate_file(str(sample_html))

        assert result["status"] == "VALID"
        assert result["original_format"] == "HTML"

    def test_empty_html_rejected(self, tmp_org_folder: Path) -> None:
        """HTML with only structural tags and no meaningful text content must be INVALID."""
        html_path = tmp_org_folder / "empty.html"
        html_path.write_text(
            "<html><head><title></title></head><body></body></html>",
            encoding="utf-8",
        )

        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="text/html",
        ):
            result = validate_file(str(html_path))

        assert result["status"] == "INVALID"


# ---------------------------------------------------------------------------
# 4. TestValidateFileImage
# ---------------------------------------------------------------------------


class TestValidateFileImage:
    """Tests for validate_file() with image inputs (PNG, JPG)."""

    def test_png_needs_ocr(self, sample_png: Path) -> None:
        """A PNG file must return VALID, needs_ocr=True, original_format='PNG'."""
        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="image/png",
        ):
            result = validate_file(str(sample_png))

        assert result["status"] == "VALID"
        assert result["needs_ocr"] is True
        assert result["original_format"] == "PNG"

    def test_jpg_needs_ocr(self, tmp_org_folder: Path) -> None:
        """A JPG file must return VALID, needs_ocr=True, original_format='JPG'."""
        jpg_path = tmp_org_folder / "photo.jpg"
        jpg_path.write_bytes(MINIMAL_JPG)

        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="image/jpeg",
        ):
            result = validate_file(str(jpg_path))

        assert result["status"] == "VALID"
        assert result["needs_ocr"] is True
        assert result["original_format"] == "JPG"


# ---------------------------------------------------------------------------
# 5. TestValidateFileXlsx
# ---------------------------------------------------------------------------


class TestValidateFileXlsx:
    """Tests for validate_file() with XLSX inputs."""

    def test_valid_xlsx_passes(self, sample_xlsx: Path) -> None:
        """An XLSX file with data must return VALID, original_format='XLSX'."""
        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ):
            result = validate_file(str(sample_xlsx))

        assert result["status"] == "VALID"
        assert result["original_format"] == "XLSX"

    def test_empty_xlsx_rejected(self, tmp_org_folder: Path) -> None:
        """An XLSX file with no data rows must be INVALID."""
        from openpyxl import Workbook

        xlsx_path = tmp_org_folder / "empty_sheet.xlsx"
        wb = Workbook()
        # Active sheet exists but has no data at all.
        wb.save(str(xlsx_path))

        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ):
            result = validate_file(str(xlsx_path))

        assert result["status"] == "INVALID"


# ---------------------------------------------------------------------------
# 6. TestValidateFileContainer
# ---------------------------------------------------------------------------


class TestValidateFileContainer:
    """Tests for validate_file() with container inputs (ZIP, etc.)."""

    def test_zip_extracts_children(self, sample_zip: Path) -> None:
        """A ZIP with 2 inner files must return VALID with child_files containing 2 entries."""
        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="application/zip",
        ):
            result = validate_file(str(sample_zip))

        assert result["status"] == "VALID"
        assert result["original_format"] == "ZIP"
        assert isinstance(result["child_files"], list)
        assert len(result["child_files"]) == 2

    def test_zip_bomb_rejected(self, tmp_org_folder: Path) -> None:
        """A ZIP containing an excessive number of files (>100) must be INVALID or REJECTED.

        We mock zipfile.ZipFile.namelist to pretend the archive has >100 entries.
        """
        zip_path = tmp_org_folder / "bomb.zip"
        # Create a real but tiny ZIP so the file exists and passes basic checks.
        with zipfile.ZipFile(str(zip_path), "w") as zf:
            zf.writestr("dummy.txt", "x")

        fake_names = [f"file_{i}.pdf" for i in range(150)]

        with patch(
            "echelonos.stages.stage_0a_validation._detect_mime_type",
            return_value="application/zip",
        ):
            with patch("zipfile.ZipFile") as mock_zf:
                mock_ctx = MagicMock()
                mock_ctx.namelist.return_value = fake_names
                mock_zf.return_value.__enter__ = MagicMock(return_value=mock_ctx)
                mock_zf.return_value.__exit__ = MagicMock(return_value=False)

                result = validate_file(str(zip_path))

        assert result["status"] in ("INVALID", "REJECTED")


# ---------------------------------------------------------------------------
# 7. TestValidateFileRejected
# ---------------------------------------------------------------------------


class TestValidateFileRejected:
    """Tests for files with unsupported formats that must be REJECTED."""

    def test_text_file_rejected(self, tmp_org_folder: Path) -> None:
        """A .txt file must be REJECTED (unsupported format)."""
        txt = _write_text_file(tmp_org_folder, "notes.txt")
        result = validate_file(str(txt))

        assert result["status"] in ("INVALID", "REJECTED")
        assert "unsupported" in result["reason"].lower()

    def test_csv_file_rejected(self, tmp_org_folder: Path) -> None:
        """A .csv file must be REJECTED (unsupported format)."""
        csv = _write_csv_file(tmp_org_folder)
        result = validate_file(str(csv))

        assert result["status"] in ("INVALID", "REJECTED")
        assert "unsupported" in result["reason"].lower()

    def test_random_binary_rejected(self, tmp_org_folder: Path) -> None:
        """Random binary data with no recognisable format must be REJECTED or INVALID."""
        blob = _write_random_bytes(tmp_org_folder, "mystery.bin")
        result = validate_file(str(blob))

        assert result["status"] in ("INVALID", "REJECTED")


# ---------------------------------------------------------------------------
# 8. TestValidateFileProvenance
# ---------------------------------------------------------------------------


class TestValidateFileProvenance:
    """Tests for the extracted_from provenance tracking field."""

    def test_extracted_from_field(self, sample_pdf: Path) -> None:
        """Calling validate_file with extracted_from='email.msg' must set the field."""
        result = validate_file(str(sample_pdf), extracted_from="email.msg")

        assert result["extracted_from"] == "email.msg"

    def test_default_extracted_from_is_none(self, sample_pdf: Path) -> None:
        """A normal call without extracted_from must default to None."""
        result = validate_file(str(sample_pdf))

        assert result["extracted_from"] is None


# ---------------------------------------------------------------------------
# 9. TestClassifyFormat
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_classify_format is None, reason="_classify_format not yet implemented")
class TestClassifyFormat:
    """Tests for the internal _classify_format(mime_type) helper.

    Returns (format_name, category) where category is one of:
    'direct', 'image', 'container', 'special', 'rejected'.
    """

    def test_classify_pdf(self) -> None:
        assert _classify_format("application/pdf") == ("PDF", "direct")

    def test_classify_docx(self) -> None:
        result = _classify_format(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert result == ("DOCX", "direct")

    def test_classify_png(self) -> None:
        assert _classify_format("image/png") == ("PNG", "image")

    def test_classify_msg(self) -> None:
        assert _classify_format("application/vnd.ms-outlook") == ("MSG", "container")

    def test_classify_xlsx(self) -> None:
        result = _classify_format(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert result == ("XLSX", "special")

    def test_classify_video(self) -> None:
        fmt, category = _classify_format("video/mp4")
        assert category == "rejected"

    def test_classify_unknown(self) -> None:
        fmt, category = _classify_format("application/octet-stream")
        assert category == "rejected"


# ---------------------------------------------------------------------------
# 10. TestConvertToPdf
# ---------------------------------------------------------------------------


class TestConvertToPdf:
    """Tests targeting convert_to_pdf()."""

    def test_pdf_copied_as_is(self, sample_pdf: Path, tmp_path: Path) -> None:
        """A PDF should be copied verbatim into the output directory."""
        out = tmp_path / "out"
        dest = convert_to_pdf(str(sample_pdf), str(out))

        assert Path(dest).exists()
        assert Path(dest).read_bytes() == sample_pdf.read_bytes()

    def test_docx_conversion_fallback(
        self, sample_docx: Path, tmp_path: Path
    ) -> None:
        """When LibreOffice is not available, DOCX should be copied with a .needs_conversion marker."""
        out = tmp_path / "out"
        dest = convert_to_pdf(str(sample_docx), str(out))

        assert Path(dest).exists()
        marker = Path(dest + ".needs_conversion")
        assert marker.exists()
        marker_text = marker.read_text()
        assert "DOCX" in marker_text

    def test_output_directory_created(self, sample_pdf: Path, tmp_path: Path) -> None:
        """convert_to_pdf must create the output directory if it does not exist."""
        out = tmp_path / "brand_new" / "nested"
        dest = convert_to_pdf(str(sample_pdf), str(out))

        assert Path(dest).exists()
        assert out.is_dir()


# ---------------------------------------------------------------------------
# 11. TestValidateFolder
# ---------------------------------------------------------------------------


class TestValidateFolder:
    """Tests targeting validate_folder()."""

    def test_folder_validation_mixed_files(
        self, sample_pdf: Path, sample_docx: Path, tmp_org_folder: Path
    ) -> None:
        """Validate a folder containing valid and invalid files — correct status counts."""
        # sample_pdf and sample_docx are already inside tmp_org_folder.
        _write_text_file(tmp_org_folder, "readme.txt")
        _write_random_bytes(tmp_org_folder, "junk.bin")

        results = validate_folder(str(tmp_org_folder))

        # Should have processed all 4 files.
        assert len(results) == 4

        statuses = [r["status"] for r in results]
        assert statuses.count("VALID") == 2  # PDF + DOCX
        # txt + random bytes should be non-VALID (INVALID or REJECTED).
        non_valid = [s for s in statuses if s != "VALID"]
        assert len(non_valid) == 2

    def test_folder_validation_empty_folder(self, tmp_path: Path) -> None:
        """An empty folder should return an empty list."""
        empty = tmp_path / "empty_dir"
        empty.mkdir()

        results = validate_folder(str(empty))
        assert results == []

    def test_folder_validation_nonexistent_folder(self, tmp_path: Path) -> None:
        """A nonexistent folder should return an empty list (not raise)."""
        results = validate_folder(str(tmp_path / "nope"))
        assert results == []

    def test_folder_validation_recursive(self, tmp_org_folder: Path) -> None:
        """Files inside sub-directories should also be discovered."""
        sub = tmp_org_folder / "subdir"
        sub.mkdir()
        _write_text_file(sub, "deep.txt")

        results = validate_folder(str(tmp_org_folder))

        deep_results = [r for r in results if "deep.txt" in r["file_path"]]
        assert len(deep_results) == 1
        assert deep_results[0]["status"] in ("INVALID", "REJECTED")


# ---------------------------------------------------------------------------
# 12. TestNewOutputFields
# ---------------------------------------------------------------------------


class TestNewOutputFields:
    """Tests verifying the expanded 7-key output schema."""

    def test_valid_result_has_all_fields(self, sample_pdf: Path) -> None:
        """A valid PDF result must contain all 7 expected keys."""
        result = validate_file(str(sample_pdf))
        _assert_result_schema(result)

    def test_needs_ocr_true_for_pdf(self, sample_pdf: Path) -> None:
        """All PDFs must have needs_ocr=True (OCR applied unconditionally)."""
        result = validate_file(str(sample_pdf))

        assert result["needs_ocr"] is True

    def test_child_files_empty_for_non_container(self, sample_pdf: Path) -> None:
        """A non-container file (PDF) must have an empty child_files list."""
        result = validate_file(str(sample_pdf))

        assert isinstance(result["child_files"], list)
        assert result["child_files"] == []

    def test_invalid_result_has_all_fields(self, zero_byte_file: Path) -> None:
        """Even an INVALID result must contain all 7 expected keys."""
        result = validate_file(str(zero_byte_file))
        _assert_result_schema(result)

    def test_needs_password_result_has_all_fields(self, tmp_org_folder: Path) -> None:
        """A NEEDS_PASSWORD result must also contain all 7 expected keys."""
        pdf_path = tmp_org_folder / "locked.pdf"
        pdf_content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>
endobj
xref
0 4
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
trailer
<< /Size 4 /Root 1 0 R >>
startxref
190
%%EOF"""
        pdf_path.write_bytes(pdf_content)

        class _MockReader:
            is_encrypted = True
            pages = []

            def decrypt(self, password: str) -> None:
                raise Exception("Invalid password")

        with patch(
            "echelonos.stages.stage_0a_validation.PdfReader",
            return_value=_MockReader(),
        ):
            result = validate_file(str(pdf_path))

        assert result["status"] == "NEEDS_PASSWORD"
        _assert_result_schema(result)
