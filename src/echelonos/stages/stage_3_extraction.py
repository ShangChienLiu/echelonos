"""Stage 3: Obligation Extraction + Verification (Dual-LLM).

Extracts contractual obligations from raw document text using GPT-4o with
structured output, then verifies each extraction through a multi-layer
verification pipeline:

1. **Grounding check** -- mechanical substring match of the cited source clause
   against the original document text.
2. **Claude cross-verification** -- an independent LLM (Claude) reviews whether
   the extracted obligation faithfully represents the source material.
3. **Chain-of-Verification (CoVe)** -- for low-confidence extractions
   (confidence < 0.80), GPT-4o generates verification questions, re-reads the
   document to answer them independently, then compares with the original
   extraction.

Each obligation is ultimately marked **VERIFIED** or **UNVERIFIED** based on
the combined results of these checks.
"""

from __future__ import annotations

import json

import structlog
from pydantic import BaseModel

from echelonos.config import settings
from echelonos.llm.claude_client import get_anthropic_client
from echelonos.llm.openai_client import get_openai_client

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

OBLIGATION_TYPES: list[str] = [
    "Delivery",
    "Financial",
    "Compliance",
    "SLA",
    "Confidentiality",
    "Termination",
    "Indemnification",
    "Governance",
]


class Obligation(BaseModel):
    """A single contractual obligation extracted from a document."""

    obligation_text: str
    obligation_type: str  # one of OBLIGATION_TYPES
    responsible_party: str
    counterparty: str
    frequency: str | None = None
    deadline: str | None = None
    source_clause: str
    source_page: int
    confidence: float


class ExtractionResult(BaseModel):
    """Structured result from the extraction step."""

    obligations: list[Obligation]
    party_roles: dict[str, str]  # e.g. {"Vendor": "CDW Government LLC"}


# ---------------------------------------------------------------------------
# Party-role extraction
# ---------------------------------------------------------------------------

_PARTY_ROLES_SYSTEM_PROMPT = (
    "You are a legal document analyst. Extract the party roles from the "
    "contract text below.\n\n"
    "Identify each party mentioned and their contractual role. Look for "
    "patterns like:\n"
    '- "CDW Government LLC, hereinafter \'Vendor\'"\n'
    '- "The State of California (\'Client\')"\n'
    '- "ABC Corp (the \'Contractor\')"\n\n'
    "Return a JSON object mapping role names to full legal entity names.\n"
    'Example: {"Vendor": "CDW Government LLC", "Client": "State of California"}'
)


class _PartyRolesResponse(BaseModel):
    """Structured response for party role extraction."""

    party_roles: dict[str, str]


def extract_party_roles(
    text: str,
    openai_client=None,
) -> dict[str, str]:
    """Extract party-role mappings from contract text using GPT-4o.

    Parameters
    ----------
    text:
        Raw contract text.
    openai_client:
        Optional pre-configured OpenAI client (useful for testing).

    Returns
    -------
    dict mapping role labels to full legal entity names.
    """
    log.info("extracting_party_roles")

    client = openai_client or get_openai_client()
    result = client.beta.chat.completions.parse(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": _PARTY_ROLES_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format=_PartyRolesResponse,
    )
    parsed: _PartyRolesResponse = result.choices[0].message.parsed
    party_roles = parsed.party_roles

    log.info("party_roles_extracted", roles=party_roles)
    return party_roles


# ---------------------------------------------------------------------------
# Obligation extraction
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM_PROMPT = (
    "You are a contract obligation extractor. Analyse the following contract "
    "text and extract every contractual obligation.\n\n"
    "For each obligation provide:\n"
    "- obligation_text: a concise summary of the obligation\n"
    "- obligation_type: one of {types}\n"
    "- responsible_party: the party that must fulfil the obligation (use the "
    "role label)\n"
    "- counterparty: the party that benefits from the obligation (use the "
    "role label)\n"
    "- frequency: how often the obligation must be fulfilled (or null)\n"
    "- deadline: when the obligation must be fulfilled (or null)\n"
    "- source_clause: the EXACT verbatim clause text from the document that "
    "establishes this obligation -- copy it character-for-character\n"
    "- source_page: the page number where this clause appears (1-indexed)\n"
    "- confidence: your confidence that this is a real obligation (0.0-1.0)\n\n"
    "Known party roles:\n{roles}\n\n"
    "Return a list of obligations in the specified structured format."
).format(types=", ".join(OBLIGATION_TYPES), roles="{roles_placeholder}")


class _ExtractionResponse(BaseModel):
    """Structured response wrapping a list of obligations."""

    obligations: list[Obligation]


def extract_obligations(
    text: str,
    party_roles: dict[str, str],
    openai_client=None,
) -> ExtractionResult:
    """Extract obligations from contract text using GPT-4o structured output.

    Parameters
    ----------
    text:
        Raw contract text.
    party_roles:
        Previously extracted role-to-entity mapping.
    openai_client:
        Optional pre-configured OpenAI client.

    Returns
    -------
    ExtractionResult containing the list of obligations and party roles.
    """
    log.info("extracting_obligations", num_roles=len(party_roles))

    roles_str = "\n".join(f"  {role}: {entity}" for role, entity in party_roles.items())
    system_prompt = _EXTRACTION_SYSTEM_PROMPT.replace("{roles_placeholder}", roles_str)

    client = openai_client or get_openai_client()
    result = client.beta.chat.completions.parse(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        response_format=_ExtractionResponse,
    )
    parsed: _ExtractionResponse = result.choices[0].message.parsed
    obligations = parsed.obligations

    log.info("obligations_extracted", count=len(obligations))
    return ExtractionResult(obligations=obligations, party_roles=party_roles)


# ---------------------------------------------------------------------------
# Verification: grounding check
# ---------------------------------------------------------------------------


def verify_grounding(obligation: Obligation, raw_text: str) -> bool:
    """Mechanical substring check -- does the cited clause exist in the text?

    Parameters
    ----------
    obligation:
        The obligation whose ``source_clause`` will be checked.
    raw_text:
        The full original document text.

    Returns
    -------
    True if the source_clause appears verbatim in raw_text, False otherwise.
    """
    grounded = obligation.source_clause in raw_text
    log.info(
        "grounding_check",
        obligation=obligation.obligation_text[:80],
        grounded=grounded,
    )
    return grounded


# ---------------------------------------------------------------------------
# Verification: Claude cross-verification
# ---------------------------------------------------------------------------


def verify_with_claude(
    obligation: Obligation,
    raw_text: str,
    anthropic_client=None,
) -> dict:
    """Send the obligation to Claude for independent verification.

    Parameters
    ----------
    obligation:
        The obligation to verify.
    raw_text:
        The full original document text.
    anthropic_client:
        Optional pre-configured Anthropic client.

    Returns
    -------
    dict with keys ``verified`` (bool), ``confidence`` (float), ``reason`` (str).
    """
    log.info("claude_verification_start", obligation=obligation.obligation_text[:80])

    client = anthropic_client or get_anthropic_client()
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": (
                    "You are verifying an obligation extracted from a contract "
                    "by another AI.\n\n"
                    f"Extracted obligation: {obligation.obligation_text}\n"
                    f"Obligation type: {obligation.obligation_type}\n"
                    f"Responsible party: {obligation.responsible_party}\n"
                    f"Counterparty: {obligation.counterparty}\n"
                    f"Cited source clause: {obligation.source_clause}\n\n"
                    f"Original document text:\n{raw_text}\n\n"
                    "Verify:\n"
                    "1. Does the source clause exist verbatim in the document?\n"
                    "2. Does the obligation accurately reflect the clause?\n"
                    "3. Is the obligation type correct?\n\n"
                    "Respond with JSON only: "
                    '{"verified": bool, "confidence": float, "reason": str}'
                ),
            }
        ],
    )

    # Parse Claude's response text as JSON.
    response_text = response.content[0].text
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        log.warning(
            "claude_response_not_json",
            response_text=response_text[:200],
        )
        result = {"verified": False, "confidence": 0.0, "reason": "Failed to parse Claude response"}

    log.info(
        "claude_verification_complete",
        verified=result.get("verified"),
        confidence=result.get("confidence"),
    )
    return result


# ---------------------------------------------------------------------------
# Verification: Chain-of-Verification (CoVe)
# ---------------------------------------------------------------------------

_COVE_QUESTIONS_SYSTEM_PROMPT = (
    "You are a contract verification specialist. Given an extracted obligation "
    "and its source clause, generate 3-5 specific verification questions that "
    "can be answered by re-reading the original document.\n\n"
    "The questions should test whether the extraction is accurate, complete, "
    "and correctly attributed.\n\n"
    "Return a JSON object: {\"questions\": [\"question1\", \"question2\", ...]}"
)

_COVE_ANSWERS_SYSTEM_PROMPT = (
    "You are a contract analyst. Answer each question below ONLY using the "
    "provided document text. If the answer cannot be found in the text, say "
    "\"NOT FOUND\".\n\n"
    "Return a JSON object: {\"answers\": [\"answer1\", \"answer2\", ...]}"
)


class _CoVeQuestionsResponse(BaseModel):
    """Structured response for CoVe question generation."""

    questions: list[str]


class _CoVeAnswersResponse(BaseModel):
    """Structured response for CoVe answer generation."""

    answers: list[str]


def run_cove(
    obligation: Obligation,
    raw_text: str,
    openai_client=None,
) -> dict:
    """Run Chain-of-Verification for low-confidence extractions.

    Only intended for obligations with ``confidence < 0.80``.

    Steps:
        1. Generate verification questions about the obligation.
        2. LLM re-reads the document to answer each question independently.
        3. Compare answers with the original extraction.

    Parameters
    ----------
    obligation:
        The obligation to verify via CoVe.
    raw_text:
        The full original document text.
    openai_client:
        Optional pre-configured OpenAI client.

    Returns
    -------
    dict with keys ``cove_passed`` (bool), ``questions`` (list), ``answers`` (list).
    """
    log.info("cove_start", obligation=obligation.obligation_text[:80])

    client = openai_client or get_openai_client()

    # Step 1: Generate verification questions.
    questions_user_prompt = (
        f"Obligation: {obligation.obligation_text}\n"
        f"Type: {obligation.obligation_type}\n"
        f"Responsible party: {obligation.responsible_party}\n"
        f"Counterparty: {obligation.counterparty}\n"
        f"Source clause: {obligation.source_clause}\n"
    )

    questions_result = client.beta.chat.completions.parse(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": _COVE_QUESTIONS_SYSTEM_PROMPT},
            {"role": "user", "content": questions_user_prompt},
        ],
        response_format=_CoVeQuestionsResponse,
    )
    questions_parsed: _CoVeQuestionsResponse = questions_result.choices[0].message.parsed
    questions = questions_parsed.questions

    # Step 2: Re-read document to answer questions independently.
    answers_user_prompt = (
        f"Document text:\n{raw_text}\n\n"
        f"Questions:\n" + "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    )

    answers_result = client.beta.chat.completions.parse(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": _COVE_ANSWERS_SYSTEM_PROMPT},
            {"role": "user", "content": answers_user_prompt},
        ],
        response_format=_CoVeAnswersResponse,
    )
    answers_parsed: _CoVeAnswersResponse = answers_result.choices[0].message.parsed
    answers = answers_parsed.answers

    # Step 3: Compare -- if any answer is "NOT FOUND" or contradicts the
    # original extraction, the CoVe check fails.
    not_found_count = sum(1 for a in answers if "NOT FOUND" in a.upper())
    cove_passed = not_found_count == 0

    log.info(
        "cove_complete",
        cove_passed=cove_passed,
        num_questions=len(questions),
        not_found_count=not_found_count,
    )
    return {
        "cove_passed": cove_passed,
        "questions": questions,
        "answers": answers,
    }


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def extract_and_verify(
    text: str,
    openai_client=None,
    anthropic_client=None,
) -> list[dict]:
    """End-to-end obligation extraction and verification pipeline.

    Steps:
        1. Extract party roles from the document.
        2. Extract obligations using GPT-4o structured output.
        3. For each obligation:
           a. Grounding check (substring match).
           b. Claude cross-verification.
           c. If confidence < 0.80, run Chain-of-Verification.
        4. Mark each obligation as **VERIFIED** or **UNVERIFIED**.

    Parameters
    ----------
    text:
        Raw contract document text.
    openai_client:
        Optional pre-configured OpenAI client.
    anthropic_client:
        Optional pre-configured Anthropic client.

    Returns
    -------
    list of dicts, each containing:
        - obligation: the Obligation model dict
        - grounding: bool
        - claude_verification: dict with verified/confidence/reason
        - cove: dict or None (only present if confidence < 0.80)
        - status: "VERIFIED" or "UNVERIFIED"
    """
    log.info("pipeline_start", text_length=len(text))

    # Step 1: Extract party roles.
    party_roles = extract_party_roles(text, openai_client=openai_client)

    # Step 2: Extract obligations.
    extraction = extract_obligations(
        text, party_roles, openai_client=openai_client
    )

    results: list[dict] = []

    for obligation in extraction.obligations:
        log.info(
            "verifying_obligation",
            obligation=obligation.obligation_text[:80],
            confidence=obligation.confidence,
        )

        # Step 3a: Grounding check.
        grounded = verify_grounding(obligation, text)

        # Step 3b: Claude cross-verification.
        claude_result = verify_with_claude(
            obligation, text, anthropic_client=anthropic_client
        )

        # Step 3c: CoVe for low-confidence extractions.
        cove_result = None
        if obligation.confidence < 0.80:
            cove_result = run_cove(obligation, text, openai_client=openai_client)

        # Determine final status.
        claude_verified = claude_result.get("verified", False)
        cove_ok = cove_result is None or cove_result.get("cove_passed", False)

        if grounded and claude_verified and cove_ok:
            status = "VERIFIED"
        else:
            status = "UNVERIFIED"

        entry = {
            "obligation": obligation.model_dump(),
            "grounding": grounded,
            "claude_verification": claude_result,
            "cove": cove_result,
            "status": status,
        }
        results.append(entry)

        log.info(
            "obligation_verified",
            obligation=obligation.obligation_text[:80],
            status=status,
            grounded=grounded,
            claude_verified=claude_verified,
            cove_passed=cove_result.get("cove_passed") if cove_result else None,
        )

    log.info(
        "pipeline_complete",
        total=len(results),
        verified=sum(1 for r in results if r["status"] == "VERIFIED"),
        unverified=sum(1 for r in results if r["status"] == "UNVERIFIED"),
    )
    return results
