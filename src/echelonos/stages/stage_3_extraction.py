"""Stage 3: Obligation Extraction + Dual Ensemble Verification.

Extracts contractual obligations from raw document text using Claude with
structured output (tool_use), then verifies each extraction through a
multi-layer verification pipeline:

1. **Dual independent extraction** -- two separate Claude calls with different
   prompt framings extract obligations independently.
2. **Programmatic matching** -- obligations from both runs are paired by
   source_clause similarity using ``difflib.SequenceMatcher``.
3. **Agreement check** -- matched pairs are compared on obligation_type,
   responsible_party, and obligation_text similarity.
4. **Grounding check** -- mechanical substring match of the cited source clause
   against the original document text.
5. **Chain-of-Verification (CoVe)** -- for DISAGREED or SOLO extractions,
   Claude generates verification questions, re-reads the document to answer
   them independently, then compares with the original extraction.

Each obligation is ultimately marked **VERIFIED** or **UNVERIFIED** based on
the combined results of these checks.
"""

from __future__ import annotations

import difflib
from concurrent.futures import ThreadPoolExecutor, as_completed

import structlog
from pydantic import BaseModel

from echelonos.config import settings
from echelonos.llm.claude_client import extract_with_structured_output, get_anthropic_client

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
    claude_client=None,
) -> dict[str, str]:
    """Extract party-role mappings from contract text using Claude.

    Parameters
    ----------
    text:
        Raw contract text.
    claude_client:
        Optional pre-configured Anthropic client (useful for testing).

    Returns
    -------
    dict mapping role labels to full legal entity names.
    """
    log.info("extracting_party_roles")

    client = claude_client or get_anthropic_client()
    parsed: _PartyRolesResponse = extract_with_structured_output(
        client=client,
        system_prompt=_PARTY_ROLES_SYSTEM_PROMPT,
        user_prompt=text,
        response_format=_PartyRolesResponse,
    )
    party_roles = parsed.party_roles

    log.info("party_roles_extracted", roles=party_roles)
    return party_roles


# ---------------------------------------------------------------------------
# Obligation extraction (primary)
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

    obligations: list[Obligation] = []


def extract_obligations(
    text: str,
    party_roles: dict[str, str],
    claude_client=None,
) -> ExtractionResult:
    """Extract obligations from contract text using Claude structured output.

    Parameters
    ----------
    text:
        Raw contract text.
    party_roles:
        Previously extracted role-to-entity mapping.
    claude_client:
        Optional pre-configured Anthropic client.

    Returns
    -------
    ExtractionResult containing the list of obligations and party roles.
    """
    log.info("extracting_obligations", num_roles=len(party_roles))

    roles_str = "\n".join(f"  {role}: {entity}" for role, entity in party_roles.items())
    system_prompt = _EXTRACTION_SYSTEM_PROMPT.replace("{roles_placeholder}", roles_str)

    client = claude_client or get_anthropic_client()
    parsed: _ExtractionResponse = extract_with_structured_output(
        client=client,
        system_prompt=system_prompt,
        user_prompt=text,
        response_format=_ExtractionResponse,
    )
    obligations = parsed.obligations

    log.info("obligations_extracted", count=len(obligations))
    return ExtractionResult(obligations=obligations, party_roles=party_roles)


# ---------------------------------------------------------------------------
# Obligation extraction (independent -- different prompt framing)
# ---------------------------------------------------------------------------

_INDEPENDENT_EXTRACTION_SYSTEM_PROMPT = (
    "You are a legal contract reviewer. Review the following contract text "
    "and identify all binding commitments, duties, and requirements imposed "
    "on either party.\n\n"
    "For each binding commitment provide:\n"
    "- obligation_text: a concise summary of the commitment\n"
    "- obligation_type: classify as one of {types}\n"
    "- responsible_party: the party who must perform (use the role label)\n"
    "- counterparty: the party who benefits (use the role label)\n"
    "- frequency: recurrence schedule if any (or null)\n"
    "- deadline: due date or timeframe if any (or null)\n"
    "- source_clause: the EXACT verbatim text from the document that "
    "creates this commitment -- copy it character-for-character\n"
    "- source_page: the page number (1-indexed)\n"
    "- confidence: how certain you are this is a genuine binding commitment "
    "(0.0-1.0)\n\n"
    "Known party roles:\n{roles}\n\n"
    "Return the list of commitments in the specified structured format."
).format(types=", ".join(OBLIGATION_TYPES), roles="{roles_placeholder}")


def extract_obligations_independent(
    text: str,
    party_roles: dict[str, str],
    claude_client=None,
) -> ExtractionResult:
    """Independent obligation extraction with a different prompt framing.

    Uses alternative wording ("binding commitments" vs "obligations") to
    avoid anchoring bias.  Does NOT receive the primary extraction results.

    Parameters
    ----------
    text:
        Raw contract text.
    party_roles:
        Previously extracted role-to-entity mapping.
    claude_client:
        Optional pre-configured Anthropic client.

    Returns
    -------
    ExtractionResult containing the independently extracted obligations.
    """
    log.info("extracting_obligations_independent", num_roles=len(party_roles))

    roles_str = "\n".join(f"  {role}: {entity}" for role, entity in party_roles.items())
    system_prompt = _INDEPENDENT_EXTRACTION_SYSTEM_PROMPT.replace("{roles_placeholder}", roles_str)

    client = claude_client or get_anthropic_client()
    parsed: _ExtractionResponse = extract_with_structured_output(
        client=client,
        system_prompt=system_prompt,
        user_prompt=text,
        response_format=_ExtractionResponse,
    )
    obligations = parsed.obligations

    log.info("obligations_extracted_independent", count=len(obligations))
    return ExtractionResult(obligations=obligations, party_roles=party_roles)


# ---------------------------------------------------------------------------
# Matching: pair obligations from both extractions
# ---------------------------------------------------------------------------


def match_extractions(
    primary: list[Obligation],
    independent: list[Obligation],
    threshold: float = 0.7,
) -> list[tuple[Obligation, Obligation | None]]:
    """Pair obligations from both extraction runs by source_clause similarity.

    Parameters
    ----------
    primary:
        Obligations from the primary extraction.
    independent:
        Obligations from the independent extraction.
    threshold:
        Minimum SequenceMatcher ratio to consider a match.

    Returns
    -------
    List of (primary_obligation, matched_independent_or_None) tuples,
    followed by (independent_only, None) tuples marked as SOLO from the
    independent side (with the independent obligation in position 0 and
    None in position 1).
    """
    used_independent: set[int] = set()
    pairs: list[tuple[Obligation, Obligation | None]] = []

    for p_obl in primary:
        best_idx: int | None = None
        best_ratio = 0.0

        for i, ind_obl in enumerate(independent):
            if i in used_independent:
                continue
            ratio = difflib.SequenceMatcher(
                None, p_obl.source_clause, ind_obl.source_clause
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = i

        if best_idx is not None and best_ratio >= threshold:
            pairs.append((p_obl, independent[best_idx]))
            used_independent.add(best_idx)
        else:
            pairs.append((p_obl, None))

    # Unmatched independent obligations (SOLO from independent side).
    for i, ind_obl in enumerate(independent):
        if i not in used_independent:
            pairs.append((ind_obl, None))

    return pairs


# ---------------------------------------------------------------------------
# Agreement check
# ---------------------------------------------------------------------------


def check_agreement(
    primary: Obligation,
    independent: Obligation,
    text_threshold: float = 0.6,
) -> bool:
    """Check whether two matched obligations agree.

    Two obligations "agree" if:
    - Same obligation_type
    - Same responsible_party
    - obligation_text similarity (SequenceMatcher ratio) > text_threshold

    Parameters
    ----------
    primary:
        Obligation from the primary extraction.
    independent:
        Obligation from the independent extraction.
    text_threshold:
        Minimum similarity ratio for obligation_text.

    Returns
    -------
    True if the two obligations agree.
    """
    if primary.obligation_type != independent.obligation_type:
        return False
    if primary.responsible_party != independent.responsible_party:
        return False

    text_ratio = difflib.SequenceMatcher(
        None, primary.obligation_text, independent.obligation_text
    ).ratio()
    return text_ratio > text_threshold


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
    claude_client=None,
) -> dict:
    """Run Chain-of-Verification for disputed or solo extractions.

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
    claude_client:
        Optional pre-configured Anthropic client.

    Returns
    -------
    dict with keys ``cove_passed`` (bool), ``questions`` (list), ``answers`` (list).
    """
    log.info("cove_start", obligation=obligation.obligation_text[:80])

    client = claude_client or get_anthropic_client()

    # Step 1: Generate verification questions.
    questions_user_prompt = (
        f"Obligation: {obligation.obligation_text}\n"
        f"Type: {obligation.obligation_type}\n"
        f"Responsible party: {obligation.responsible_party}\n"
        f"Counterparty: {obligation.counterparty}\n"
        f"Source clause: {obligation.source_clause}\n"
    )

    questions_parsed: _CoVeQuestionsResponse = extract_with_structured_output(
        client=client,
        system_prompt=_COVE_QUESTIONS_SYSTEM_PROMPT,
        user_prompt=questions_user_prompt,
        response_format=_CoVeQuestionsResponse,
    )
    questions = questions_parsed.questions

    # Step 2: Re-read document to answer questions independently.
    answers_user_prompt = (
        f"Document text:\n{raw_text}\n\n"
        f"Questions:\n" + "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    )

    answers_parsed: _CoVeAnswersResponse = extract_with_structured_output(
        client=client,
        system_prompt=_COVE_ANSWERS_SYSTEM_PROMPT,
        user_prompt=answers_user_prompt,
        response_format=_CoVeAnswersResponse,
    )
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
    claude_client=None,
) -> list[dict]:
    """End-to-end obligation extraction and dual-ensemble verification pipeline.

    Steps:
        1. Extract party roles from the document.
        2. Run two independent obligation extractions with different prompts.
        3. Programmatically match obligations from both extractions.
        4. For each matched pair:
           a. Determine agreement status (AGREED / DISAGREED / SOLO).
           b. Grounding check (substring match).
           c. For AGREED + grounded → VERIFIED.
           d. For DISAGREED / SOLO → grounding + CoVe arbitration.
        5. Mark each obligation as **VERIFIED** or **UNVERIFIED**.

    Parameters
    ----------
    text:
        Raw contract document text.
    claude_client:
        Optional pre-configured Anthropic client.

    Returns
    -------
    list of dicts, each containing:
        - obligation: the Obligation model dict
        - grounding: bool
        - ensemble: dict with agreement/primary_extraction/independent_extraction
        - cove: dict or None (only for DISAGREED/SOLO)
        - status: "VERIFIED" or "UNVERIFIED"
    """
    log.info("pipeline_start", text_length=len(text))

    # Step 1: Extract party roles.
    party_roles = extract_party_roles(text, claude_client=claude_client)

    # Step 2: Run both extractions independently (in parallel).
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_primary = pool.submit(
            extract_obligations, text, party_roles, claude_client=claude_client
        )
        f_independent = pool.submit(
            extract_obligations_independent, text, party_roles, claude_client=claude_client
        )
        primary_extraction = f_primary.result()
        independent_extraction = f_independent.result()

    # Step 3: Match obligations from both extractions.
    pairs = match_extractions(
        primary_extraction.obligations,
        independent_extraction.obligations,
    )

    # First pass: determine agreement + grounding for all pairs, collect CoVe work.
    pair_info: list[dict] = []
    cove_work: list[tuple[int, Obligation]] = []  # (index, obligation)

    for idx, (primary_obl, independent_obl) in enumerate(pairs):
        log.info(
            "verifying_obligation",
            obligation=primary_obl.obligation_text[:80],
            has_match=independent_obl is not None,
        )

        if independent_obl is None:
            agreement = "SOLO"
        elif check_agreement(primary_obl, independent_obl):
            agreement = "AGREED"
        else:
            agreement = "DISAGREED"

        grounded = verify_grounding(primary_obl, text)

        pair_info.append({
            "primary_obl": primary_obl,
            "independent_obl": independent_obl,
            "agreement": agreement,
            "grounded": grounded,
            "cove_result": None,
        })

        if agreement in ("DISAGREED", "SOLO"):
            cove_work.append((idx, primary_obl))

    # Parallel CoVe arbitration for all DISAGREED / SOLO obligations.
    if cove_work:
        max_cove = settings.stage3_max_cove_workers
        with ThreadPoolExecutor(max_workers=max_cove) as pool:
            futures = {
                pool.submit(run_cove, obl, text, claude_client=claude_client): idx
                for idx, obl in cove_work
            }
            for future in as_completed(futures):
                idx = futures[future]
                pair_info[idx]["cove_result"] = future.result()

    # Assemble final results.
    results: list[dict] = []
    for info in pair_info:
        primary_obl = info["primary_obl"]
        independent_obl = info["independent_obl"]
        agreement = info["agreement"]
        grounded = info["grounded"]
        cove_result = info["cove_result"]

        if agreement == "AGREED" and grounded:
            status = "VERIFIED"
        elif agreement in ("DISAGREED", "SOLO"):
            cove_ok = cove_result is not None and cove_result.get("cove_passed", False)
            if grounded and cove_ok:
                status = "VERIFIED"
            else:
                status = "UNVERIFIED"
        else:
            status = "UNVERIFIED"

        entry = {
            "obligation": primary_obl.model_dump(),
            "grounding": grounded,
            "ensemble": {
                "agreement": agreement,
                "primary_extraction": primary_obl.model_dump(),
                "independent_extraction": independent_obl.model_dump() if independent_obl else None,
            },
            "cove": cove_result,
            "status": status,
        }
        results.append(entry)

        log.info(
            "obligation_verified",
            obligation=primary_obl.obligation_text[:80],
            status=status,
            grounded=grounded,
            agreement=agreement,
            cove_passed=cove_result.get("cove_passed") if cove_result else None,
        )

    log.info(
        "pipeline_complete",
        total=len(results),
        verified=sum(1 for r in results if r["status"] == "VERIFIED"),
        unverified=sum(1 for r in results if r["status"] == "UNVERIFIED"),
    )
    return results
