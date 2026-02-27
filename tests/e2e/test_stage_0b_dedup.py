"""End-to-end tests for Stage 0b: File Deduplication (4-Layer Hash Pipeline).

Each test creates temporary PDF/DOCX files on disk and runs them through
the full ``deduplicate_files`` pipeline so that all four layers are exercised
together, end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document as DocxDocument

from echelonos.stages.stage_0b_dedup import (
    compute_content_hash,
    compute_file_hash,
    compute_simhash,
    compute_structural_fingerprint,
    deduplicate_files,
    extract_text,
    hamming_distance,
)

# ---------------------------------------------------------------------------
# Helpers to create test files
# ---------------------------------------------------------------------------


def _build_pdf_bytes(text: str) -> bytes:
    """Build a minimal valid PDF whose page content stream contains *text*.

    The xref table is computed dynamically so offsets are always correct.
    ``pypdf.PdfReader.pages[0].extract_text()`` will return the text.
    """
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream_content = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET"
    stream_bytes = stream_content.encode("latin-1")
    stream_len = len(stream_bytes)

    objects: list[bytes] = []

    # obj 1 - Catalog
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    # obj 2 - Pages
    objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    # obj 3 - Page
    objects.append(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
    )
    # obj 4 - Content stream
    objects.append(
        f"4 0 obj\n<< /Length {stream_len} >>\nstream\n".encode("latin-1")
        + stream_bytes
        + b"\nendstream\nendobj\n"
    )
    # obj 5 - Font
    objects.append(
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    )

    header = b"%PDF-1.4\n"
    body = b""
    offsets: list[int] = []
    pos = len(header)
    for obj in objects:
        offsets.append(pos)
        body += obj
        pos += len(obj)

    xref_offset = pos
    xref = b"xref\n"
    xref += f"0 {len(objects) + 1}\n".encode()
    xref += b"0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n".encode()

    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()

    return header + body + xref + trailer


def _make_pdf(path: Path, text: str) -> Path:
    """Write a single-page PDF containing *text* to *path*."""
    path.write_bytes(_build_pdf_bytes(text))
    return path


def _make_docx(path: Path, text: str) -> Path:
    """Write a DOCX file containing *text* to *path*."""
    doc = DocxDocument()
    doc.add_paragraph(text)
    doc.save(str(path))
    return path


def _entry(file_path: str, **kwargs) -> dict:
    """Build a minimal file entry dict for ``deduplicate_files``."""
    base = {"file_path": file_path, "status": "VALID"}
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExactDuplicateDetected:
    """Layer 1: Two byte-identical PDFs should yield one duplicate."""

    def test_exact_duplicate_detected(self, tmp_path: Path):
        text = "Master Services Agreement between Acme Corp and Widget Inc dated 2024-01-15."
        pdf_a = _make_pdf(tmp_path / "contract_a.pdf", text)
        pdf_b = tmp_path / "contract_b.pdf"
        # Byte-for-byte copy
        pdf_b.write_bytes(pdf_a.read_bytes())

        files = [_entry(str(pdf_a)), _entry(str(pdf_b))]
        unique = deduplicate_files(files)

        # Only the first file should survive
        assert len(unique) == 1
        assert unique[0]["file_path"] == str(pdf_a)
        assert unique[0]["is_duplicate"] is False

        # The second entry (mutated in-place) should be flagged
        dup = files[1]
        assert dup["is_duplicate"] is True
        assert dup["duplicate_of"] == str(pdf_a)
        assert dup["dedup_layer"] == 1


class TestContentDuplicateDetected:
    """Layer 2: Same text in PDF vs DOCX should be caught."""

    def test_content_duplicate_detected(self, tmp_path: Path):
        text = "Statement of Work for Project Phoenix between Acme Corp and Widget Inc."
        pdf_file = _make_pdf(tmp_path / "sow.pdf", text)
        docx_file = _make_docx(tmp_path / "sow.docx", text)

        files = [_entry(str(pdf_file)), _entry(str(docx_file))]
        unique = deduplicate_files(files)

        assert len(unique) == 1
        assert unique[0]["file_path"] == str(pdf_file)

        dup = files[1]
        assert dup["is_duplicate"] is True
        assert dup["duplicate_of"] == str(pdf_file)
        assert dup["dedup_layer"] == 2


class TestNearDuplicateDetected:
    """Layer 3: Files with minor text differences should be flagged via SimHash."""

    def test_near_duplicate_detected(self, tmp_path: Path):
        base_text = (
            "This Master Services Agreement is entered into by and between "
            "Acme Corporation and Widget Incorporated effective January 15 2024. "
            "The parties agree to the following terms and conditions for the "
            "provision of consulting services as described herein."
        )
        # Minor edit: change one word and add a typo
        edited_text = (
            "This Master Services Agreement is entered into by and between "
            "Acme Corporation and Widget Incorporated effective January 15 2024. "
            "The parties agree to the following terms and conditions for the "
            "provision of advisory services as described herein."
        )

        pdf_a = _make_pdf(tmp_path / "original.pdf", base_text)
        pdf_b = _make_pdf(tmp_path / "edited.pdf", edited_text)

        # Verify they are NOT exact or content-hash duplicates
        assert compute_file_hash(str(pdf_a)) != compute_file_hash(str(pdf_b))
        text_a = extract_text(str(pdf_a))
        text_b = extract_text(str(pdf_b))
        assert compute_content_hash(text_a) != compute_content_hash(text_b)

        # But SimHash distance should be small
        sh_a = compute_simhash(text_a)
        sh_b = compute_simhash(text_b)
        assert hamming_distance(sh_a, sh_b) <= 3, (
            f"Expected hamming distance <= 3, got {hamming_distance(sh_a, sh_b)}"
        )

        files = [_entry(str(pdf_a)), _entry(str(pdf_b))]
        unique = deduplicate_files(files)

        assert len(unique) == 1
        assert unique[0]["file_path"] == str(pdf_a)
        dup = files[1]
        assert dup["is_duplicate"] is True
        assert dup["dedup_layer"] == 3


class TestAmendmentNotFlagged:
    """Layer 4: Similar files with different structural metadata must NOT be flagged."""

    def test_amendment_not_flagged(self, tmp_path: Path):
        # Base contract and its amendment share nearly identical text
        base_text = (
            "This Master Services Agreement is entered into by and between "
            "Acme Corporation and Widget Incorporated effective January 15 2024."
        )
        amendment_text = (
            "This Master Services Agreement is entered into by and between "
            "Acme Corporation and Widget Incorporated effective January 15 2024."
        )

        pdf_base = _make_pdf(tmp_path / "base_contract.pdf", base_text)
        pdf_amend = _make_pdf(tmp_path / "amendment.pdf", amendment_text)

        files = [
            _entry(
                str(pdf_base),
                doc_type="MSA",
                date="2024-01-15",
                parties=["Acme Corporation", "Widget Incorporated"],
            ),
            _entry(
                str(pdf_amend),
                doc_type="Amendment",
                date="2024-06-01",
                parties=["Acme Corporation", "Widget Incorporated"],
            ),
        ]

        unique = deduplicate_files(files)

        # Both should survive because structural fingerprints differ
        assert len(unique) == 2
        paths = {f["file_path"] for f in unique}
        assert str(pdf_base) in paths
        assert str(pdf_amend) in paths
        for f in unique:
            assert f["is_duplicate"] is False


class TestUniqueFilesPass:
    """Completely different files should all pass through with no duplicates."""

    def test_unique_files_pass(self, tmp_path: Path):
        texts = [
            "Non-Disclosure Agreement between Alpha Inc and Beta LLC dated 2023-03-01.",
            "Software License Agreement between Gamma Corp and Delta Partners.",
            "Employment Agreement for John Smith with Epsilon Industries effective 2024-07-01.",
        ]
        entries = []
        for i, text in enumerate(texts):
            pdf = _make_pdf(tmp_path / f"unique_{i}.pdf", text)
            entries.append(_entry(str(pdf)))

        unique = deduplicate_files(entries)

        assert len(unique) == 3
        for f in unique:
            assert f["is_duplicate"] is False


class TestEmptyInput:
    """An empty file list should return an empty list without errors."""

    def test_empty_input(self):
        assert deduplicate_files([]) == []


class TestHammingDistanceCalculation:
    """Unit-level tests for the hamming_distance helper."""

    def test_identical_hashes(self):
        assert hamming_distance(0, 0) == 0
        assert hamming_distance(0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF) == 0

    def test_single_bit_difference(self):
        assert hamming_distance(0b0000, 0b0001) == 1
        assert hamming_distance(0b1000, 0b0000) == 1

    def test_known_distance(self):
        # 0b1010 vs 0b0101 -> all 4 bits differ
        assert hamming_distance(0b1010, 0b0101) == 4

    def test_large_distance(self):
        # All 64 bits differ
        assert hamming_distance(0, 0xFFFFFFFFFFFFFFFF) == 64

    def test_symmetry(self):
        a, b = 12345, 67890
        assert hamming_distance(a, b) == hamming_distance(b, a)


class TestComputeStructuralFingerprint:
    """Structural fingerprint should be deterministic and order-independent for parties."""

    def test_deterministic(self):
        fp1 = compute_structural_fingerprint("MSA", "2024-01-15", ["Acme", "Beta"])
        fp2 = compute_structural_fingerprint("MSA", "2024-01-15", ["Acme", "Beta"])
        assert fp1 == fp2

    def test_party_order_independent(self):
        fp1 = compute_structural_fingerprint("MSA", "2024-01-15", ["Acme", "Beta"])
        fp2 = compute_structural_fingerprint("MSA", "2024-01-15", ["Beta", "Acme"])
        assert fp1 == fp2

    def test_different_doc_type_differs(self):
        fp1 = compute_structural_fingerprint("MSA", "2024-01-15", ["Acme"])
        fp2 = compute_structural_fingerprint("Amendment", "2024-01-15", ["Acme"])
        assert fp1 != fp2

    def test_different_date_differs(self):
        fp1 = compute_structural_fingerprint("MSA", "2024-01-15", ["Acme"])
        fp2 = compute_structural_fingerprint("MSA", "2024-06-01", ["Acme"])
        assert fp1 != fp2


class TestExtractText:
    """Text extraction should work for both PDF and DOCX."""

    def test_extract_pdf(self, tmp_path: Path):
        text = "Hello from a PDF document."
        pdf = _make_pdf(tmp_path / "test.pdf", text)
        extracted = extract_text(str(pdf))
        assert "Hello from a PDF document" in extracted

    def test_extract_docx(self, tmp_path: Path):
        text = "Hello from a DOCX document."
        docx = _make_docx(tmp_path / "test.docx", text)
        extracted = extract_text(str(docx))
        assert "Hello from a DOCX document" in extracted

    def test_unsupported_format(self, tmp_path: Path):
        txt_file = tmp_path / "readme.txt"
        txt_file.write_text("plain text")
        assert extract_text(str(txt_file)) == ""


class TestComputeFileHash:
    """File hash should be deterministic and differ for different content."""

    def test_deterministic(self, tmp_path: Path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello world")
        assert compute_file_hash(str(f)) == compute_file_hash(str(f))

    def test_different_content(self, tmp_path: Path):
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        f1.write_bytes(b"hello")
        f2.write_bytes(b"world")
        assert compute_file_hash(str(f1)) != compute_file_hash(str(f2))


class TestComputeContentHash:
    """Content hash normalizes before hashing."""

    def test_case_insensitive(self):
        assert compute_content_hash("Hello World") == compute_content_hash("hello world")

    def test_whitespace_insensitive(self):
        assert compute_content_hash("hello   world") == compute_content_hash("hello world")

    def test_punctuation_insensitive(self):
        assert compute_content_hash("hello, world!") == compute_content_hash("hello world")
