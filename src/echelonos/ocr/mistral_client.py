"""Mistral OCR client for document text extraction with table preservation."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

import structlog
from mistralai import Mistral

from echelonos.config import settings

log = structlog.get_logger(__name__)

# Mapping of file extensions to MIME types for Mistral document upload.
_MIME_MAP: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def get_mistral_client() -> Mistral:
    """Create and return a Mistral client from application settings."""
    return Mistral(api_key=settings.mistral_api_key)


def _detect_mime(file_path: str) -> str:
    """Detect the MIME type for a file path."""
    ext = Path(file_path).suffix.lower()
    if ext in _MIME_MAP:
        return _MIME_MAP[ext]
    mime, _ = mimetypes.guess_type(file_path)
    return mime or "application/octet-stream"


def analyze_document(client: Mistral, file_path: str) -> dict:
    """Analyze a document using Mistral OCR.

    Reads the file from disk, base64-encodes it, sends it to Mistral's OCR
    endpoint, and returns per-page text with table structures preserved.

    The return format matches the stage-1 contract::

        {
            "pages": [
                {
                    "page_number": int,    # 1-indexed
                    "text": str,           # markdown content (paragraphs)
                    "tables": list[str],   # markdown table strings
                    "confidence": float,   # placeholder (Mistral doesn't provide per-word scores)
                },
                ...
            ],
            "total_pages": int,
        }
    """
    file_bytes = Path(file_path).read_bytes()
    b64_data = base64.standard_b64encode(file_bytes).decode("utf-8")
    mime_type = _detect_mime(file_path)

    # Build the data URI for inline document upload.
    data_uri = f"data:{mime_type};base64,{b64_data}"

    log.info("mistral_ocr_request", file_path=file_path, mime_type=mime_type)

    # Determine document type based on MIME.
    if mime_type.startswith("image/"):
        document = {"type": "image_url", "image_url": data_uri}
    else:
        document = {"type": "document_url", "document_url": data_uri}

    ocr_response = client.ocr.process(
        model=settings.mistral_ocr_model,
        document=document,
        include_image_base64=False,
    )

    # Parse the Mistral OCR response into our standard page format.
    pages: list[dict] = []

    for page in ocr_response.pages:
        page_number = page.index + 1  # Mistral uses 0-indexed; we use 1-indexed.
        markdown = page.markdown or ""

        # Split markdown into text and tables.
        # Mistral returns everything as markdown — tables appear as markdown
        # tables within the content.  We separate them for downstream use.
        text_parts: list[str] = []
        table_parts: list[str] = []
        current_table_lines: list[str] = []
        in_table = False

        for line in markdown.split("\n"):
            stripped = line.strip()
            is_table_line = stripped.startswith("|") and stripped.endswith("|")

            if is_table_line:
                in_table = True
                current_table_lines.append(line)
            else:
                if in_table:
                    # End of a table block — flush it.
                    table_parts.append("\n".join(current_table_lines))
                    current_table_lines = []
                    in_table = False
                if stripped:
                    text_parts.append(line)

        # Flush any trailing table.
        if current_table_lines:
            table_parts.append("\n".join(current_table_lines))

        page_data = {
            "page_number": page_number,
            "text": "\n".join(text_parts) + ("\n" if text_parts else ""),
            "tables": table_parts,
            # Mistral OCR doesn't provide per-page confidence scores.
            # We use 0.95 as a reasonable default since Mistral OCR is
            # generally high-quality for printed documents.
            "confidence": 0.95,
        }
        pages.append(page_data)

    log.info(
        "mistral_ocr_complete",
        file_path=file_path,
        total_pages=len(pages),
    )

    return {"pages": pages, "total_pages": len(pages)}
