"""Stage 2: Document Classification.

Classifies contract documents into categories (MSA, SOW, Amendment, etc.)
using GPT-4o with structured output.  Extracts key metadata such as parties,
effective date, and parent contract references.
"""

import re

import structlog
from pydantic import BaseModel

from echelonos.config import settings
from echelonos.llm.openai_client import extract_with_structured_output, get_openai_client

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pydantic model for the classification result
# ---------------------------------------------------------------------------

VALID_DOC_TYPES = frozenset({
    "MSA",
    "SOW",
    "Amendment",
    "Addendum",
    "NDA",
    "Order Form",
    "Other",
})


class ClassificationResult(BaseModel):
    """Structured result from the document classification stage."""

    doc_type: str  # MSA | SOW | Amendment | Addendum | NDA | Order Form | Other
    parties: list[str]
    effective_date: str | None
    parent_reference_raw: str | None
    confidence: float


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

CLASSIFICATION_SYSTEM_PROMPT = """\
You are a contract classification assistant.  Given the text of a legal
document, you must determine what type of contract it is and extract key
metadata.

## Document Types

Classify the document into exactly one of the following categories:

- **MSA** -- Master Service Agreement.  An overarching contract that
  establishes the general terms governing the relationship between the
  parties.  Subsequent SOWs, Order Forms, or Addenda typically reference
  an MSA.
- **SOW** -- Statement of Work.  Describes specific deliverables, timelines,
  and fees for a particular engagement, usually executed under an MSA.
- **Amendment** -- Modifies one or more provisions of an existing contract.
  Key indicators include phrases such as "hereby amends", "modifies
  Section", "amended and restated", or explicit reference to a prior
  agreement being changed.
- **Addendum** -- Adds new terms or schedules to an existing contract
  without modifying the original terms.
- **NDA** -- Non-Disclosure Agreement.  Governs the handling of confidential
  information exchanged between the parties.
- **Order Form** -- A purchase order or order form that specifies products,
  quantities, pricing, and delivery details.
- **Other** -- Use this when the document does not fit any of the above
  categories.

## Extraction Rules

1. **doc_type** -- One of: MSA, SOW, Amendment, Addendum, NDA, Order Form, Other.
2. **parties** -- A list of party names (company or individual) identified
   in the document.  Include all signatories.
3. **effective_date** -- The effective date of the contract in ISO-8601
   format (YYYY-MM-DD) if stated, otherwise null.
4. **parent_reference_raw** -- If the document references a parent or prior
   agreement (e.g. "Master Service Agreement dated January 1, 2024"),
   include the raw reference string.  Otherwise null.
5. **confidence** -- Your confidence in the classification as a float
   between 0.0 and 1.0.

Respond ONLY with the structured JSON output matching the schema.  Do not
include any commentary outside of the JSON structure.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_document(
    text: str,
    openai_client=None,
) -> ClassificationResult:
    """Classify a contract document using GPT-4o with structured output.

    Parameters
    ----------
    text:
        The full text of the contract document to classify.
    openai_client:
        An optional pre-configured ``OpenAI`` client instance.  When *None*
        a new client is created via :func:`get_openai_client`.

    Returns
    -------
    ClassificationResult
        The classification result with doc_type, parties, effective_date,
        parent_reference_raw, and confidence.
    """
    log.info("classifying_document", text_length=len(text))

    if not text or not text.strip():
        log.warning("empty_document_text")
        return ClassificationResult(
            doc_type="UNKNOWN",
            parties=[],
            effective_date=None,
            parent_reference_raw=None,
            confidence=0.0,
        )

    client = openai_client or get_openai_client()

    result: ClassificationResult = extract_with_structured_output(
        client=client,
        system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
        user_prompt=text,
        response_format=ClassificationResult,
    )

    log.info(
        "classification_raw",
        doc_type=result.doc_type,
        confidence=result.confidence,
        parties=result.parties,
    )

    # Low-confidence guard: if the model is not sure, fall back to UNKNOWN.
    if result.confidence < 0.7:
        log.warning(
            "low_confidence_classification",
            doc_type=result.doc_type,
            confidence=result.confidence,
        )
        result = result.model_copy(update={"doc_type": "UNKNOWN"})

    return result


def classify_with_cross_check(
    text: str,
    result: ClassificationResult,
) -> ClassificationResult:
    """Apply rule-based cross-checks to an existing classification.

    This function looks for textual signals that may contradict the LLM's
    classification and adjusts accordingly.

    Parameters
    ----------
    text:
        The original document text (used for pattern matching).
    result:
        The classification result produced by :func:`classify_document`.

    Returns
    -------
    ClassificationResult
        A potentially updated classification result.  The ``doc_type`` may
        be changed and a ``parent_reference_raw`` annotation may be added.
    """
    log.info(
        "cross_checking_classification",
        original_doc_type=result.doc_type,
    )

    amendment_patterns = [
        re.compile(r"hereby\s+amends?", re.IGNORECASE),
        re.compile(r"modifies?\s+section", re.IGNORECASE),
        re.compile(r"amended\s+and\s+restated", re.IGNORECASE),
    ]

    text_signals_amendment = any(pat.search(text) for pat in amendment_patterns)

    # Cross-check 1: classified as MSA but text contains amendment language.
    if result.doc_type == "MSA" and text_signals_amendment:
        log.warning(
            "cross_check_reclassified",
            from_type="MSA",
            to_type="Amendment",
            reason="amendment_language_detected",
        )
        result = result.model_copy(update={"doc_type": "Amendment"})

    # Cross-check 2: classified as Amendment but no parent reference found.
    if result.doc_type == "Amendment" and not result.parent_reference_raw:
        log.warning(
            "cross_check_suspicious_amendment",
            reason="amendment_without_parent_reference",
        )
        # We flag by setting parent_reference_raw to a sentinel value rather
        # than changing the type, so downstream stages can handle it.
        result = result.model_copy(
            update={"parent_reference_raw": "SUSPICIOUS: no parent reference found"},
        )

    return result
