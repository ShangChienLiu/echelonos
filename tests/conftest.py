"""Shared test fixtures."""

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_org_folder(tmp_path: Path) -> Path:
    """Create a temporary organization folder with sample files."""
    org = tmp_path / "test_org"
    org.mkdir()
    return org


@pytest.fixture
def sample_pdf(tmp_org_folder: Path) -> Path:
    """Create a minimal valid PDF file."""
    pdf_path = tmp_org_folder / "sample.pdf"
    # Minimal valid PDF
    pdf_content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 44 >>
stream
BT /F1 12 Tf 100 700 Td (Test contract) Tj ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000266 00000 n
0000000360 00000 n
trailer
<< /Size 6 /Root 1 0 R >>
startxref
441
%%EOF"""
    pdf_path.write_bytes(pdf_content)
    return pdf_path


@pytest.fixture
def sample_docx(tmp_org_folder: Path) -> Path:
    """Create a minimal DOCX file."""
    from docx import Document

    docx_path = tmp_org_folder / "sample.docx"
    doc = Document()
    doc.add_paragraph("This is a test contract document.")
    doc.save(str(docx_path))
    return docx_path


@pytest.fixture
def zero_byte_file(tmp_org_folder: Path) -> Path:
    """Create a zero-byte file."""
    f = tmp_org_folder / "empty.pdf"
    f.write_bytes(b"")
    return f
