"""Stage 1: Document Ingestion / OCR.

Uses Azure Document Intelligence to OCR PDF documents, extracting per-page
text with page numbers and preserving table structures as markdown tables.
Includes an OCR confidence quality gate that flags low-confidence pages.
"""

from __future__ import annotations

import structlog
from azure.core.exceptions import HttpResponseError, ServiceRequestError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from echelonos.ocr.azure_client import analyze_document, get_azure_client

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# OCR Confidence thresholds
# ---------------------------------------------------------------------------

LOW_CONFIDENCE_THRESHOLD: float = 0.60
MEDIUM_CONFIDENCE_THRESHOLD: float = 0.85


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assess_confidence(pages: list[dict]) -> list[dict]:
    """Evaluate per-page OCR confidence and produce quality-gate flags.

    Returns a list of flag dicts.  Each flag has:
        - page_number (int)
        - flag_type   (str)   "LOW_OCR_QUALITY" or "MEDIUM_OCR_QUALITY"
        - message     (str)
    """
    flags: list[dict] = []
    for page in pages:
        confidence = page.get("ocr_confidence", 0.0)
        page_num = page["page_number"]

        if confidence < LOW_CONFIDENCE_THRESHOLD:
            flags.append(
                {
                    "page_number": page_num,
                    "flag_type": "LOW_OCR_QUALITY",
                    "message": (
                        f"Page {page_num} OCR confidence {confidence:.2f} "
                        f"is below the minimum threshold ({LOW_CONFIDENCE_THRESHOLD})"
                    ),
                }
            )
            log.warning(
                "low_ocr_confidence",
                page_number=page_num,
                confidence=confidence,
            )
        elif confidence < MEDIUM_CONFIDENCE_THRESHOLD:
            flags.append(
                {
                    "page_number": page_num,
                    "flag_type": "MEDIUM_OCR_QUALITY",
                    "message": (
                        f"Page {page_num} OCR confidence {confidence:.2f} "
                        f"is below the recommended threshold ({MEDIUM_CONFIDENCE_THRESHOLD})"
                    ),
                }
            )
            log.info(
                "medium_ocr_confidence",
                page_number=page_num,
                confidence=confidence,
            )

    return flags


def _build_page_result(raw_page: dict) -> dict:
    """Normalise a raw page dict from azure_client into the stage-1 schema.

    The azure_client returns pages with keys ``page_number``, ``text``,
    ``tables`` (list of markdown strings), and ``confidence``.  We merge
    tables into a single ``tables_markdown`` string and rename
    ``confidence`` to ``ocr_confidence``.
    """
    tables_md = "\n\n".join(raw_page.get("tables", []))
    return {
        "page_number": raw_page["page_number"],
        "text": raw_page.get("text", ""),
        "tables_markdown": tables_md,
        "ocr_confidence": raw_page.get("confidence", 0.0),
    }


@retry(
    retry=retry_if_exception_type((HttpResponseError, ServiceRequestError, ConnectionError, TimeoutError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _call_azure(client, file_path: str) -> dict:
    """Call Azure Document Intelligence with retry logic for transient errors."""
    return analyze_document(client, file_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest_document(
    file_path: str,
    doc_id: str,
    azure_client=None,
) -> dict:
    """Ingest a PDF document via Azure Document Intelligence OCR.

    Parameters
    ----------
    file_path:
        Path to the PDF file to ingest.
    doc_id:
        Unique identifier for the document in the pipeline.
    azure_client:
        Optional pre-configured ``DocumentIntelligenceClient``.  When
        ``None`` a new client is created from application settings.

    Returns
    -------
    dict with keys:
        - doc_id       (str)
        - pages        (list[dict])  each with page_number, text,
                       tables_markdown, ocr_confidence
        - total_pages  (int)
        - flags        (list[dict])  quality-gate flags
    """
    log.info("ingesting_document", file_path=file_path, doc_id=doc_id)

    if azure_client is None:
        azure_client = get_azure_client()

    try:
        raw_result = _call_azure(azure_client, file_path)
    except (HttpResponseError, ServiceRequestError) as exc:
        log.error(
            "azure_api_error",
            file_path=file_path,
            doc_id=doc_id,
            error=str(exc),
        )
        return {
            "doc_id": doc_id,
            "pages": [],
            "total_pages": 0,
            "flags": [
                {
                    "page_number": 0,
                    "flag_type": "OCR_ERROR",
                    "message": f"Azure Document Intelligence API error: {exc}",
                }
            ],
        }
    except Exception as exc:
        log.error(
            "ocr_unexpected_error",
            file_path=file_path,
            doc_id=doc_id,
            error=str(exc),
        )
        return {
            "doc_id": doc_id,
            "pages": [],
            "total_pages": 0,
            "flags": [
                {
                    "page_number": 0,
                    "flag_type": "OCR_ERROR",
                    "message": f"Unexpected OCR error: {exc}",
                }
            ],
        }

    # Normalise pages into the stage-1 output schema.
    pages = [_build_page_result(p) for p in raw_result.get("pages", [])]
    total_pages = raw_result.get("total_pages", len(pages))

    # Confidence quality gate.
    flags = _assess_confidence(pages)

    log.info(
        "document_ingested",
        doc_id=doc_id,
        total_pages=total_pages,
        flags_count=len(flags),
    )

    return {
        "doc_id": doc_id,
        "pages": pages,
        "total_pages": total_pages,
        "flags": flags,
    }


def get_full_text(pages: list[dict]) -> str:
    """Concatenate all page text into a single string.

    Pages are separated by a form-feed character (``\\f``) to preserve
    page boundaries in the combined output.  Table markdown is appended
    after the page text when present.

    Parameters
    ----------
    pages:
        List of page dicts as returned by ``ingest_document``.

    Returns
    -------
    str -- the combined full-text content.
    """
    sections: list[str] = []
    for page in pages:
        parts: list[str] = []
        text = page.get("text", "")
        if text:
            parts.append(text)
        tables_md = page.get("tables_markdown", "")
        if tables_md:
            parts.append(tables_md)
        sections.append("\n".join(parts))

    return "\f".join(sections)
