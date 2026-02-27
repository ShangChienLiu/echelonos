"""Shared test fixtures."""

import os
import zipfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Minimal valid file bytes
# ---------------------------------------------------------------------------

MINIMAL_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
    b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00'
    b'\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00'
    b'\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
)

# Minimal valid JPEG (1x1 white pixel, ~283 bytes).
MINIMAL_JPG = (
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01'
    b'\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06'
    b'\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b'
    b'\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c'
    b'\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xc0'
    b'\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4'
    b'\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00'
    b'\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06'
    b'\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03'
    b'\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02'
    b'\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q\x142\x81'
    b'\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16'
    b'\x17\x18\x19\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghij'
    b'stuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94'
    b'\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8'
    b'\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3'
    b'\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7'
    b'\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea'
    b'\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00'
    b'\x08\x01\x01\x00\x00?\x00T\xdb\xae\x8a(\x03\xff\xd9'
)

# Minimal valid PDF content that libmagic identifies as application/pdf.
MINIMAL_PDF_BYTES = b"""%PDF-1.4
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


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_org_folder(tmp_path: Path) -> Path:
    """Create a temporary organization folder with sample files."""
    org = tmp_path / "test_org"
    org.mkdir()
    return org


@pytest.fixture
def sample_pdf(tmp_org_folder: Path) -> Path:
    """Create a minimal valid PDF file with extractable text."""
    pdf_path = tmp_org_folder / "sample.pdf"
    pdf_path.write_bytes(MINIMAL_PDF_BYTES)
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


# ---------------------------------------------------------------------------
# New fixtures for expanded Stage 0a
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_html(tmp_org_folder: Path) -> Path:
    """Create a simple HTML file with contract text."""
    html_path = tmp_org_folder / "contract.html"
    html_content = (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head><title>Contract</title></head>\n"
        "<body>\n"
        "<h1>Service Agreement</h1>\n"
        "<p>This contract is entered into by Party A and Party B.</p>\n"
        "<p>Section 1: Terms and conditions apply.</p>\n"
        "</body>\n"
        "</html>\n"
    )
    html_path.write_text(html_content, encoding="utf-8")
    return html_path


@pytest.fixture
def sample_xlsx(tmp_org_folder: Path) -> Path:
    """Create a minimal XLSX file with data using openpyxl."""
    from openpyxl import Workbook

    xlsx_path = tmp_org_folder / "data.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["col1", "col2", "col3"])
    ws.append([1, 2, 3])
    ws.append([4, 5, 6])
    wb.save(str(xlsx_path))
    return xlsx_path


@pytest.fixture
def sample_png(tmp_org_folder: Path) -> Path:
    """Create a tiny 1x1 white PNG image."""
    png_path = tmp_org_folder / "image.png"
    png_path.write_bytes(MINIMAL_PNG)
    return png_path


@pytest.fixture
def sample_jpg(tmp_org_folder: Path) -> Path:
    """Create a tiny 1x1 white JPEG image."""
    jpg_path = tmp_org_folder / "photo.jpg"
    jpg_path.write_bytes(MINIMAL_JPG)
    return jpg_path


@pytest.fixture
def sample_zip(tmp_org_folder: Path) -> Path:
    """Create a ZIP archive containing a sample PDF and a text file."""
    zip_path = tmp_org_folder / "bundle.zip"
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        # Add a minimal PDF inside the ZIP.
        zf.writestr("inner_contract.pdf", MINIMAL_PDF_BYTES)
        # Add a plain text file inside the ZIP.
        zf.writestr("notes.txt", "Some notes about the contract.")
    return zip_path
