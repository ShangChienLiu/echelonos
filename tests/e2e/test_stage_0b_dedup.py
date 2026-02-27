"""End-to-end tests for Stage 0b: File Deduplication (4-Layer Hash Pipeline).

Each test creates temporary PDF/DOCX files on disk and runs them through
the full ``deduplicate_files`` pipeline so that all four layers are exercised
together, end-to-end.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from datasketch import MinHash
from docx import Document as DocxDocument

from echelonos.stages.stage_0b_dedup import (
    BlockingKeyFields,
    _blocking_keys_match,
    _normalize_amount,
    _normalize_date,
    _normalize_id,
    _normalize_vendor,
    _regex_fallback_blocking_keys,
    compute_content_hash,
    compute_file_hash,
    compute_minhash,
    compute_structural_fingerprint,
    deduplicate_files,
    extract_identity_tokens,
    extract_text,
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


def _write_pdf_no_text(folder: Path, name: str, extra_byte: bytes = b"") -> Path:
    """Write a minimal valid PDF with NO text content (simulates scanned PDF).

    Each call with different *extra_byte* produces a different file hash.
    """
    # A valid PDF with an empty content stream — pypdf extracts "".
    content = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
        b"xref\n0 4\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\n"
        b"startxref\n175\n%%EOF"
        + extra_byte
    )
    path = folder / name
    path.write_bytes(content)
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
    """Layer 3: Files with high Jaccard similarity (>= 0.85) should be flagged."""

    def test_near_duplicate_detected(self, tmp_path: Path):
        """Same document with word-order swap (e.g. OCR artefact) should be
        caught by Layer 3 — MinHash uses bag-of-words so word order doesn't
        matter and Jaccard should be ~1.0.
        """
        base_text = (
            "This Master Services Agreement is entered into by and between "
            "Acme Corporation and Widget Incorporated effective January 15 2024. "
            "The parties agree to the following terms and conditions for the "
            "provision of consulting services as described herein."
        )
        # Swap two words: "Acme Corporation" -> "Corporation Acme"
        # MinHash uses bag-of-words so this has Jaccard ~1.0
        variant_text = (
            "This Master Services Agreement is entered into by and between "
            "Corporation Acme and Widget Incorporated effective January 15 2024. "
            "The parties agree to the following terms and conditions for the "
            "provision of consulting services as described herein."
        )

        pdf_a = _make_pdf(tmp_path / "original.pdf", base_text)
        pdf_b = _make_pdf(tmp_path / "variant.pdf", variant_text)

        # Verify they are NOT caught by Layer 1 or Layer 2
        assert compute_file_hash(str(pdf_a)) != compute_file_hash(str(pdf_b))
        text_a = extract_text(str(pdf_a))
        text_b = extract_text(str(pdf_b))
        assert compute_content_hash(text_a) != compute_content_hash(text_b)

        # MinHash Jaccard should be very high (word-order swap -> same bag-of-words)
        mh_a = compute_minhash(text_a)
        mh_b = compute_minhash(text_b)
        assert mh_a.jaccard(mh_b) >= 0.85, (
            f"Expected Jaccard >= 0.85, got {mh_a.jaccard(mh_b)}"
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


class TestScannedPdfsNotDeduplicated:
    """Scanned PDFs (no extractable text) should NOT be collapsed as duplicates.

    When pypdf extracts zero text from a scanned PDF, the content hash is
    the hash of an empty string.  Layer 2/3 must skip files with no
    extractable text to avoid treating every scanned PDF as a duplicate.
    """

    def test_different_scanned_pdfs_kept_as_unique(self, tmp_org_folder: Path) -> None:
        """Three scanned PDFs (different file bytes, no text) should all be kept.

        Each file differs in raw bytes (different extra_byte) so Layer 1
        won't catch them.  Layer 2/3 must not collapse them because no
        text was extracted.
        """
        _write_pdf_no_text(tmp_org_folder, "scan_a.pdf", extra_byte=b"A")
        _write_pdf_no_text(tmp_org_folder, "scan_b.pdf", extra_byte=b"B")
        _write_pdf_no_text(tmp_org_folder, "scan_c.pdf", extra_byte=b"C")

        files = [
            {"file_path": str(tmp_org_folder / n), "status": "VALID"}
            for n in ("scan_a.pdf", "scan_b.pdf", "scan_c.pdf")
        ]
        result = deduplicate_files(files)
        # All 3 have different bytes — should be unique
        assert len(result) == 3, f"Expected 3 unique, got {len(result)}"


class TestMinhashThresholdRejectsDissimilarDocs:
    """Layer 3 should NOT flag documents with Jaccard similarity below 0.85.

    Legal contracts share boilerplate language that produces artificially
    similar fingerprints.  MinHash with threshold 0.85 should only flag
    truly near-identical documents.
    """

    def test_different_contracts_below_threshold_not_flagged(self, tmp_path: Path):
        """Two contracts sharing some boilerplate but with substantial
        differences should NOT be flagged as duplicates.
        """
        contract_a = (
            "Non-Disclosure Agreement between Alpha Corp and Beta LLC. "
            "This agreement governs the sharing of confidential information "
            "between the parties for the purpose of evaluating a potential "
            "business relationship in the field of software development."
        )
        contract_b = (
            "Master Services Agreement between Gamma Inc and Delta Partners. "
            "This agreement establishes the terms and conditions under which "
            "consulting services will be provided for the enterprise data "
            "migration project commencing January 2025."
        )

        pdf_a = _make_pdf(tmp_path / "nda.pdf", contract_a)
        pdf_b = _make_pdf(tmp_path / "msa.pdf", contract_b)

        text_a = extract_text(str(pdf_a))
        text_b = extract_text(str(pdf_b))

        # Verify Jaccard is below threshold
        mh_a = compute_minhash(text_a)
        mh_b = compute_minhash(text_b)
        assert mh_a.jaccard(mh_b) < 0.85, (
            f"Test setup error: expected Jaccard < 0.85, got {mh_a.jaccard(mh_b)}"
        )

        files = [_entry(str(pdf_a)), _entry(str(pdf_b))]
        unique = deduplicate_files(files)

        # Both should be kept — they are different contracts
        assert len(unique) == 2, (
            f"Expected 2 unique, got {len(unique)}; "
            f"Jaccard was {mh_a.jaccard(mh_b)}"
        )


class TestMinimalTextSkipsContentLayers:
    """Files with very little extractable text (e.g. a single period from
    a scanned PDF) should skip Layer 2/3 to avoid false collapses.
    """

    def test_near_empty_text_not_deduplicated(self, tmp_path: Path):
        """Two PDFs with real text content that is very short (< 50 chars)
        should not be collapsed by Layer 2/3 even if their normalized text
        happens to be similar.
        """
        # Simulate scanned PDFs where OCR extracts only a few garbage chars
        pdf_a = _make_pdf(tmp_path / "scan_a.pdf", ".")
        pdf_b = _make_pdf(tmp_path / "scan_b.pdf", ",")

        files = [_entry(str(pdf_a)), _entry(str(pdf_b))]
        unique = deduplicate_files(files)

        # Both should survive — too little text for reliable content comparison
        assert len(unique) == 2, f"Expected 2 unique, got {len(unique)}"


class TestEmptyInput:
    """An empty file list should return an empty list without errors."""

    def test_empty_input(self):
        assert deduplicate_files([]) == []


class TestComputeMinhash:
    """Unit-level tests for the compute_minhash function."""

    def test_identical_text_yields_jaccard_one(self):
        text = "This is a test document with several words for hashing."
        mh1 = compute_minhash(text)
        mh2 = compute_minhash(text)
        assert mh1.jaccard(mh2) == 1.0

    def test_word_order_swap_yields_jaccard_one(self):
        """MinHash is set-based, so word order doesn't matter."""
        text_a = "alpha beta gamma delta epsilon"
        text_b = "epsilon delta gamma beta alpha"
        mh_a = compute_minhash(text_a)
        mh_b = compute_minhash(text_b)
        assert mh_a.jaccard(mh_b) == 1.0

    def test_completely_different_text_yields_low_jaccard(self):
        text_a = "apple banana cherry dragonfruit elderberry fig grape"
        text_b = "quantum physics thermodynamics relativity mechanics entropy"
        mh_a = compute_minhash(text_a)
        mh_b = compute_minhash(text_b)
        assert mh_a.jaccard(mh_b) < 0.2

    def test_empty_text_returns_valid_minhash(self):
        mh = compute_minhash("")
        assert isinstance(mh, MinHash)

    def test_respects_num_perm(self):
        mh = compute_minhash("test text", num_perm=64)
        assert len(mh.hashvalues) == 64


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


class TestExtractIdentityTokens:
    """Identity tokens extract PO numbers, dollar amounts, and dates from text."""

    def test_extracts_po_numbers(self):
        text = "Purchase Order 4501693981 for 7th Street Solutions"
        tokens = extract_identity_tokens(text)
        assert "4501693981" in tokens

    def test_extracts_dollar_amounts(self):
        text = "Total amount due: $3,800.00 payable within Net 30"
        tokens = extract_identity_tokens(text)
        assert "$3,800.00" in tokens

    def test_extracts_dates(self):
        text = "Effective date 05/13/2025 through 06/02/2025"
        tokens = extract_identity_tokens(text)
        assert "05/13/2025" in tokens
        assert "06/02/2025" in tokens

    def test_different_pos_produce_different_tokens(self):
        text_a = "Purchase Order 4501693981 Date 05/13/2025 Amount $3,800.00"
        text_b = "Purchase Order 4501703538 Date 06/02/2025 Amount $4,125.00"
        assert extract_identity_tokens(text_a) != extract_identity_tokens(text_b)

    def test_identical_text_produces_same_tokens(self):
        text = "Invoice 12345 dated 01/15/2024 total $100,000"
        assert extract_identity_tokens(text) == extract_identity_tokens(text)

    def test_empty_text_produces_empty_tokens(self):
        assert extract_identity_tokens("") == ""
        assert extract_identity_tokens("no numbers here") == ""


class TestIdentityTokensPreventMinhashFalsePositives:
    """Template-based documents with different identifying details (PO numbers,
    amounts, dates) should NOT be collapsed by Layer 3, because their identity
    tokens differ and Layer 4 protects them.
    """

    def test_same_template_different_po_numbers_kept(self, tmp_path: Path):
        """Two POs from the same vendor template with different PO numbers,
        dates, and amounts must both survive dedup.  They have high Jaccard
        similarity (would be flagged by Layer 3) but identity tokens differ
        (Layer 4 protects).
        """
        # Long shared boilerplate with identifying details that differ.
        # Needs enough shared words to push Jaccard above 0.85 threshold.
        boilerplate = (
            "7th Street Solutions LLC Temporary Staffing Services "
            "Terms and Conditions Net 30 Ship to 123 Main Street "
            "Authorized by John Smith Department of Operations "
            "General Manager Cadillac Products Automotive Company "
            "This purchase order is subject to the terms and conditions "
            "attached hereto and incorporated herein by reference "
            "Please remit payment to the address listed above "
            "All invoices must reference the purchase order number "
            "Vendor agrees to comply with all applicable laws and regulations "
            "governing the performance of services under this agreement"
        )
        po_a = f"Purchase Order 4501693981 Date 05/13/2025 Amount $3,800.00 {boilerplate}"
        po_b = f"Purchase Order 4501703538 Date 06/02/2025 Amount $4,125.00 {boilerplate}"

        pdf_a = _make_pdf(tmp_path / "po_a.pdf", po_a)
        pdf_b = _make_pdf(tmp_path / "po_b.pdf", po_b)

        text_a = extract_text(str(pdf_a))
        text_b = extract_text(str(pdf_b))

        # Verify high Jaccard similarity (MinHash would flag them)
        mh_a = compute_minhash(text_a)
        mh_b = compute_minhash(text_b)
        assert mh_a.jaccard(mh_b) >= 0.85, (
            f"Test setup: expected Jaccard >= 0.85, got {mh_a.jaccard(mh_b)}"
        )

        # Identity tokens must differ
        assert extract_identity_tokens(text_a) != extract_identity_tokens(text_b)

        files = [_entry(str(pdf_a)), _entry(str(pdf_b))]
        unique = deduplicate_files(files)

        # Both should survive — different POs protected by identity tokens
        assert len(unique) == 2, (
            f"Expected 2 unique POs, got {len(unique)}; "
            f"identity tokens should have protected them"
        )

    def test_true_duplicate_different_format_still_caught(self, tmp_path: Path):
        """A true near-duplicate (same document, word-order swap) with the
        same identity tokens should still be caught by Layer 3.
        """
        base_text = (
            "Advisory Agreement effective 5/27/2025 between Ascension Corp "
            "and Rexair LLC for consulting services total fee $100,000 "
            "reference number 49601 payment terms net thirty days"
        )
        # Word-order swap — same tokens, different content hash
        variant_text = (
            "Advisory Agreement effective 5/27/2025 between Rexair LLC "
            "and Ascension Corp for consulting services total fee $100,000 "
            "reference number 49601 payment terms net thirty days"
        )

        pdf_a = _make_pdf(tmp_path / "original.pdf", base_text)
        pdf_b = _make_pdf(tmp_path / "variant.pdf", variant_text)

        text_a = extract_text(str(pdf_a))
        text_b = extract_text(str(pdf_b))

        # Same identity tokens
        assert extract_identity_tokens(text_a) == extract_identity_tokens(text_b)
        # Different content hash
        assert compute_content_hash(text_a) != compute_content_hash(text_b)

        files = [_entry(str(pdf_a)), _entry(str(pdf_b))]
        unique = deduplicate_files(files)

        # Should be deduped — same identity tokens, MinHash match
        assert len(unique) == 1
        assert files[1]["is_duplicate"] is True
        assert files[1]["dedup_layer"] == 3


# ---------------------------------------------------------------------------
# Blocking Key Field Comparison Tests
# ---------------------------------------------------------------------------


class TestBlockingKeyFieldComparison:
    """Tests for _blocking_keys_match field-level comparison logic."""

    def test_different_po_numbers_protect(self):
        """Different PO numbers should protect documents from collapse."""
        a = BlockingKeyFields(
            vendor_name="7th Street Solutions",
            po_number="4501693981",
            total_amount="$3,800.00",
        )
        b = BlockingKeyFields(
            vendor_name="7th Street Solutions",
            po_number="4501703538",
            total_amount="$4,125.00",
        )
        assert _blocking_keys_match(a, b) is False

    def test_same_po_numbers_collapse(self):
        """Same PO numbers should allow collapse."""
        a = BlockingKeyFields(
            vendor_name="7th Street Solutions",
            po_number="4501693981",
            total_amount="$3,800.00",
        )
        b = BlockingKeyFields(
            vendor_name="7th Street Solutions",
            po_number="4501693981",
            total_amount="$3,800.00",
        )
        assert _blocking_keys_match(a, b) is True

    def test_different_invoice_numbers_protect(self):
        """Different invoice numbers should protect documents."""
        a = BlockingKeyFields(
            vendor_name="Acme Corp",
            invoice_number="INV-2024-001",
        )
        b = BlockingKeyFields(
            vendor_name="Acme Corp",
            invoice_number="INV-2024-002",
        )
        assert _blocking_keys_match(a, b) is False

    def test_same_vendor_different_amount_protect(self):
        """Same vendor with different amounts should protect."""
        a = BlockingKeyFields(
            vendor_name="7th Street Solutions",
            total_amount="$3,800.00",
        )
        b = BlockingKeyFields(
            vendor_name="7th Street Solutions",
            total_amount="$4,125.00",
        )
        assert _blocking_keys_match(a, b) is False

    def test_same_vendor_different_date_protect(self):
        """Same vendor with different dates should protect."""
        a = BlockingKeyFields(
            vendor_name="7th Street Solutions",
            document_date="2025-05-13",
        )
        b = BlockingKeyFields(
            vendor_name="7th Street Solutions",
            document_date="2025-06-02",
        )
        assert _blocking_keys_match(a, b) is False

    def test_no_distinguishing_fields_collapse(self):
        """No distinguishing fields should allow collapse."""
        a = BlockingKeyFields()
        b = BlockingKeyFields()
        assert _blocking_keys_match(a, b) is True

    def test_vendor_normalization_llc_variants(self):
        """Vendor names with LLC vs LLC. should be treated as same."""
        a = BlockingKeyFields(
            vendor_name="7th Street Solutions LLC",
            po_number="4501693981",
        )
        b = BlockingKeyFields(
            vendor_name="7th Street Solutions LLC.",
            po_number="4501693981",
        )
        assert _blocking_keys_match(a, b) is True

    def test_same_invoice_number_collapse(self):
        """Same invoice number should allow collapse."""
        a = BlockingKeyFields(
            vendor_name="Acme Corp",
            invoice_number="INV-2024-001",
            total_amount="$5,000.00",
        )
        b = BlockingKeyFields(
            vendor_name="Acme Corp",
            invoice_number="INV-2024-001",
            total_amount="$5,000.00",
        )
        assert _blocking_keys_match(a, b) is True


# ---------------------------------------------------------------------------
# Normalization Function Tests
# ---------------------------------------------------------------------------


class TestNormalizationFunctions:
    """Tests for field normalization helpers."""

    def test_normalize_vendor_strips_llc(self):
        assert _normalize_vendor("7th Street Solutions LLC") == "7th street solutions"

    def test_normalize_vendor_strips_inc(self):
        assert _normalize_vendor("Acme Corporation Inc.") == "acme corporation"

    def test_normalize_vendor_strips_corp(self):
        assert _normalize_vendor("Delta Corp") == "delta"

    def test_normalize_vendor_collapses_whitespace(self):
        assert _normalize_vendor("  Acme   Corp  ") == "acme"

    def test_normalize_vendor_none_returns_empty(self):
        assert _normalize_vendor(None) == ""

    def test_normalize_amount_strips_dollar_and_commas(self):
        assert _normalize_amount("$3,800.00") == "3800"

    def test_normalize_amount_rounds_to_int(self):
        assert _normalize_amount("$4,125.50") == "4126"

    def test_normalize_amount_none_returns_empty(self):
        assert _normalize_amount(None) == ""

    def test_normalize_amount_plain_number(self):
        assert _normalize_amount("1500") == "1500"

    def test_normalize_date_iso_format(self):
        assert _normalize_date("2025-05-13") == "2025-05-13"

    def test_normalize_date_us_format(self):
        assert _normalize_date("05/13/2025") == "2025-05-13"

    def test_normalize_date_none_returns_empty(self):
        assert _normalize_date(None) == ""

    def test_normalize_id_strips_and_lowercases(self):
        assert _normalize_id("  INV-2024-001  ") == "inv-2024-001"

    def test_normalize_id_none_returns_empty(self):
        assert _normalize_id(None) == ""


# ---------------------------------------------------------------------------
# Blocking Keys with Claude (mocked) Tests
# ---------------------------------------------------------------------------


class TestBlockingKeysWithClaude:
    """Tests using mocked Claude extraction for blocking keys."""

    def test_different_po_numbers_both_survive_with_claude(self, tmp_path: Path):
        """Mock Claude returning different PO numbers — both docs survive."""
        boilerplate = (
            "7th Street Solutions LLC Temporary Staffing Services "
            "Terms and Conditions Net 30 Ship to 123 Main Street "
            "Authorized by John Smith Department of Operations "
            "General Manager Cadillac Products Automotive Company "
            "This purchase order is subject to the terms and conditions "
            "attached hereto and incorporated herein by reference "
            "Please remit payment to the address listed above "
            "All invoices must reference the purchase order number "
            "Vendor agrees to comply with all applicable laws and regulations "
            "governing the performance of services under this agreement"
        )
        po_a = f"Purchase Order 4501693981 Date 05/13/2025 Amount $3,800.00 {boilerplate}"
        po_b = f"Purchase Order 4501703538 Date 06/02/2025 Amount $4,125.00 {boilerplate}"

        pdf_a = _make_pdf(tmp_path / "po_a.pdf", po_a)
        pdf_b = _make_pdf(tmp_path / "po_b.pdf", po_b)

        keys_a = BlockingKeyFields(
            vendor_name="7th Street Solutions LLC",
            po_number="4501693981",
            total_amount="$3,800.00",
            document_date="2025-05-13",
        )
        keys_b = BlockingKeyFields(
            vendor_name="7th Street Solutions LLC",
            po_number="4501703538",
            total_amount="$4,125.00",
            document_date="2025-06-02",
        )

        call_count = 0
        def mock_extract(client, system_prompt, user_prompt, response_format):
            nonlocal call_count
            call_count += 1
            if "4501693981" in user_prompt:
                return keys_a
            return keys_b

        mock_client = MagicMock()

        with patch(
            "echelonos.stages.stage_0b_dedup.extract_with_structured_output",
            side_effect=mock_extract,
        ):
            files = [_entry(str(pdf_a)), _entry(str(pdf_b))]
            unique = deduplicate_files(files, claude_client=mock_client)

        assert len(unique) == 2

    def test_same_blocking_keys_collapse_with_claude(self, tmp_path: Path):
        """Mock Claude returning identical keys — second doc collapses."""
        boilerplate = (
            "7th Street Solutions LLC Temporary Staffing Services "
            "Terms and Conditions Net 30 Ship to 123 Main Street "
            "Authorized by John Smith Department of Operations "
            "General Manager Cadillac Products Automotive Company "
            "This purchase order is subject to the terms and conditions "
            "attached hereto and incorporated herein by reference "
            "Please remit payment to the address listed above "
            "All invoices must reference the purchase order number "
            "Vendor agrees to comply with all applicable laws and regulations "
            "governing the performance of services under this agreement"
        )
        text_a = f"Purchase Order 4501693981 Date 05/13/2025 Amount $3,800.00 {boilerplate}"
        # Word-order variant with same details
        text_b = f"Date 05/13/2025 Purchase Order 4501693981 Amount $3,800.00 {boilerplate}"

        pdf_a = _make_pdf(tmp_path / "original.pdf", text_a)
        pdf_b = _make_pdf(tmp_path / "variant.pdf", text_b)

        same_keys = BlockingKeyFields(
            vendor_name="7th Street Solutions LLC",
            po_number="4501693981",
            total_amount="$3,800.00",
            document_date="2025-05-13",
        )

        mock_client = MagicMock()

        with patch(
            "echelonos.stages.stage_0b_dedup.extract_with_structured_output",
            return_value=same_keys,
        ):
            files = [_entry(str(pdf_a)), _entry(str(pdf_b))]
            unique = deduplicate_files(files, claude_client=mock_client)

        assert len(unique) == 1

    def test_claude_failure_falls_back_to_regex(self, tmp_path: Path):
        """When Claude extraction fails, regex fallback should work."""
        boilerplate = (
            "7th Street Solutions LLC Temporary Staffing Services "
            "Terms and Conditions Net 30 Ship to 123 Main Street "
            "Authorized by John Smith Department of Operations "
            "General Manager Cadillac Products Automotive Company "
            "This purchase order is subject to the terms and conditions "
            "attached hereto and incorporated herein by reference "
            "Please remit payment to the address listed above "
            "All invoices must reference the purchase order number "
            "Vendor agrees to comply with all applicable laws and regulations "
            "governing the performance of services under this agreement"
        )
        po_a = f"Purchase Order 4501693981 Date 05/13/2025 Amount $3,800.00 {boilerplate}"
        po_b = f"Purchase Order 4501703538 Date 06/02/2025 Amount $4,125.00 {boilerplate}"

        pdf_a = _make_pdf(tmp_path / "po_a.pdf", po_a)
        pdf_b = _make_pdf(tmp_path / "po_b.pdf", po_b)

        mock_client = MagicMock()

        with patch(
            "echelonos.stages.stage_0b_dedup.extract_with_structured_output",
            side_effect=Exception("API error"),
        ):
            files = [_entry(str(pdf_a)), _entry(str(pdf_b))]
            unique = deduplicate_files(files, claude_client=mock_client)

        # Regex fallback should still protect different POs
        assert len(unique) == 2


# ---------------------------------------------------------------------------
# Blocking Keys Fallback Tests
# ---------------------------------------------------------------------------


class TestBlockingKeysFallback:
    """Tests for graceful fallback when Claude is unavailable."""

    def test_no_api_key_pipeline_completes(self, tmp_path: Path):
        """Without Claude client, pipeline uses regex fallback and completes."""
        boilerplate = (
            "7th Street Solutions LLC Temporary Staffing Services "
            "Terms and Conditions Net 30 Ship to 123 Main Street "
            "Authorized by John Smith Department of Operations "
            "General Manager Cadillac Products Automotive Company "
            "This purchase order is subject to the terms and conditions "
            "attached hereto and incorporated herein by reference "
            "Please remit payment to the address listed above "
            "All invoices must reference the purchase order number "
            "Vendor agrees to comply with all applicable laws and regulations "
            "governing the performance of services under this agreement"
        )
        po_a = f"Purchase Order 4501693981 Date 05/13/2025 Amount $3,800.00 {boilerplate}"
        po_b = f"Purchase Order 4501703538 Date 06/02/2025 Amount $4,125.00 {boilerplate}"

        pdf_a = _make_pdf(tmp_path / "po_a.pdf", po_a)
        pdf_b = _make_pdf(tmp_path / "po_b.pdf", po_b)

        # No claude_client — should use regex fallback
        files = [_entry(str(pdf_a)), _entry(str(pdf_b))]
        unique = deduplicate_files(files)

        # Both should survive via regex fallback protecting different POs
        assert len(unique) == 2

    def test_regex_fallback_extracts_blocking_keys(self):
        """Regex fallback should extract PO numbers, amounts, and dates."""
        text = "Purchase Order 4501693981 Date 05/13/2025 Amount $3,800.00"
        keys = _regex_fallback_blocking_keys(text)
        assert keys is not None
        assert keys.po_number is not None
        assert keys.total_amount is not None
        assert keys.document_date is not None
