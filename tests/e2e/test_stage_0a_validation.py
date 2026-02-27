"""E2E tests for Stage 0a: File Validation Gate.

Each test exercises the public API of stage_0a_validation against real (or
realistically crafted) files on disk, using the shared fixtures from
conftest.py where appropriate.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from echelonos.stages.stage_0a_validation import (
    convert_to_pdf,
    validate_file,
    validate_folder,
)


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestValidateFile:
    """Tests targeting validate_file()."""

    def test_valid_pdf_passes(self, sample_pdf: Path) -> None:
        """A minimal but structurally valid PDF must return VALID."""
        result = validate_file(str(sample_pdf))

        assert result["status"] == "VALID"
        assert result["original_format"] == "PDF"
        assert result["file_path"] == str(sample_pdf)
        assert result["reason"]  # non-empty

    def test_valid_docx_passes(self, sample_docx: Path) -> None:
        """A valid DOCX produced by python-docx must return VALID."""
        result = validate_file(str(sample_docx))

        assert result["status"] == "VALID"
        assert result["original_format"] == "DOCX"

    def test_zero_byte_file_rejected(self, zero_byte_file: Path) -> None:
        """A zero-byte file must be INVALID regardless of extension."""
        result = validate_file(str(zero_byte_file))

        assert result["status"] == "INVALID"
        assert "zero bytes" in result["reason"].lower()

    def test_nonexistent_file_rejected(self, tmp_path: Path) -> None:
        """A path that does not exist on disk must be INVALID."""
        fake = tmp_path / "does_not_exist.pdf"
        result = validate_file(str(fake))

        assert result["status"] == "INVALID"
        assert "not exist" in result["reason"].lower()

    def test_non_document_txt_rejected(self, tmp_org_folder: Path) -> None:
        """A plain .txt file is not an accepted MIME type -> INVALID."""
        txt = _write_text_file(tmp_org_folder, "notes.txt")
        result = validate_file(str(txt))

        assert result["status"] == "INVALID"
        assert "unsupported" in result["reason"].lower()

    def test_non_document_csv_rejected(self, tmp_org_folder: Path) -> None:
        """A .csv file is not an accepted MIME type -> INVALID."""
        csv = _write_csv_file(tmp_org_folder)
        result = validate_file(str(csv))

        assert result["status"] == "INVALID"
        assert "unsupported" in result["reason"].lower()

    def test_corrupted_pdf_rejected(self, tmp_org_folder: Path) -> None:
        """Random bytes disguised as a .pdf must be rejected as INVALID."""
        bad_pdf = _write_random_bytes(tmp_org_folder, "garbage.pdf")
        result = validate_file(str(bad_pdf))

        assert result["status"] == "INVALID"
        # Could be rejected at MIME level or at PDF-parse level – either is fine.

    def test_corrupted_docx_rejected(self, tmp_org_folder: Path) -> None:
        """Random bytes disguised as a .docx must be rejected as INVALID."""
        bad_docx = _write_random_bytes(tmp_org_folder, "garbage.docx")
        result = validate_file(str(bad_docx))

        assert result["status"] == "INVALID"

    def test_password_protected_pdf(self, tmp_org_folder: Path) -> None:
        """A password-protected PDF must return NEEDS_PASSWORD.

        Creating a genuinely encrypted PDF is cumbersome without extra
        dependencies.  We mock PdfReader to simulate an encrypted file
        that cannot be decrypted with an empty password.
        """
        # We still need a file that passes the existence + MIME checks.
        # Use the minimal valid PDF bytes and patch the PDF reader.
        pdf_path = tmp_org_folder / "encrypted.pdf"
        # Minimal PDF content that libmagic will identify as application/pdf.
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

        # Build a mock reader that pretends the PDF is encrypted.
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


class TestValidateFolder:
    """Tests targeting validate_folder()."""

    def test_folder_validation_mixed_files(
        self, sample_pdf: Path, sample_docx: Path, tmp_org_folder: Path
    ) -> None:
        """Validate a folder containing valid and invalid files."""
        # sample_pdf and sample_docx are already inside tmp_org_folder.
        # Add some invalid files.
        _write_text_file(tmp_org_folder, "readme.txt")
        _write_random_bytes(tmp_org_folder, "junk.bin")

        results = validate_folder(str(tmp_org_folder))

        # Should have processed all 4 files.
        assert len(results) == 4

        statuses = [r["status"] for r in results]
        assert statuses.count("VALID") == 2  # PDF + DOCX
        assert statuses.count("INVALID") == 2  # txt + random bytes

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

        # At minimum the nested file must appear.
        deep_results = [r for r in results if "deep.txt" in r["file_path"]]
        assert len(deep_results) == 1
        assert deep_results[0]["status"] == "INVALID"


class TestConvertToPdf:
    """Tests targeting convert_to_pdf()."""

    def test_pdf_copied_as_is(self, sample_pdf: Path, tmp_path: Path) -> None:
        """A PDF should be copied verbatim into the output directory."""
        out = tmp_path / "out"
        dest = convert_to_pdf(str(sample_pdf), str(out))

        assert Path(dest).exists()
        assert Path(dest).read_bytes() == sample_pdf.read_bytes()

    def test_docx_flagged_for_conversion(
        self, sample_docx: Path, tmp_path: Path
    ) -> None:
        """A DOCX should be copied and a .needs_conversion marker created."""
        out = tmp_path / "out"
        dest = convert_to_pdf(str(sample_docx), str(out))

        assert Path(dest).exists()
        marker = Path(dest + ".needs_conversion")
        assert marker.exists()
        marker_text = marker.read_text()
        assert "DOCX" in marker_text

    def test_output_directory_created(self, sample_pdf: Path, tmp_path: Path) -> None:
        """convert_to_pdf must create the output directory if it doesn't exist."""
        out = tmp_path / "brand_new" / "nested"
        dest = convert_to_pdf(str(sample_pdf), str(out))

        assert Path(dest).exists()
        assert out.is_dir()
