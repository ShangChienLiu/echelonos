"""Stage 5: Amendment Resolution (Chain Walking).

Resolves how amendments affect obligations extracted from base contracts (MSAs).
Builds chronological chains from MSA -> Amendment #1 -> Amendment #2 -> ... and
walks each chain to determine whether each original obligation is ACTIVE,
SUPERSEDED, or TERMINATED by subsequent amendments.

Resolution strategy:
  1. Build ordered amendment chains from document link records.
  2. For each chain, start with the MSA obligations.
  3. Walk amendments chronologically, comparing each amendment's obligations
     against the current set of active obligations.
  4. Use LLM clause comparison to determine if an amendment REPLACES, MODIFIES,
     leaves UNCHANGED, or DELETEs an original obligation.
  5. Produce a final obligation list with statuses and amendment history.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import structlog
from pydantic import BaseModel

from echelonos.config import settings
from echelonos.llm.claude_client import extract_with_structured_output, get_anthropic_client

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ResolutionResult(BaseModel):
    """Result of comparing an original clause against an amendment clause."""

    action: str  # "REPLACE" | "MODIFY" | "UNCHANGED" | "DELETE"
    original_clause: str
    amendment_clause: str
    reasoning: str
    confidence: float


# ---------------------------------------------------------------------------
# Internal response model for structured LLM output
# ---------------------------------------------------------------------------


class _ComparisonResponse(BaseModel):
    """Structured response from the LLM clause comparison."""

    action: str
    reasoning: str
    confidence: float


# ---------------------------------------------------------------------------
# System prompt for clause comparison
# ---------------------------------------------------------------------------

_CLAUSE_COMPARISON_SYSTEM_PROMPT = (
    "Compare these two contract clauses. Does the amendment clause REPLACE, "
    "MODIFY, or leave UNCHANGED the original? If it explicitly deletes, say "
    "DELETE.\n\n"
    "Definitions:\n"
    "- REPLACE: The amendment clause entirely supersedes the original clause. "
    "The original is no longer in effect.\n"
    "- MODIFY: The amendment clause changes part of the original clause but "
    "does not fully replace it. Both clauses partially apply.\n"
    "- UNCHANGED: The amendment clause does not affect the original clause.\n"
    "- DELETE: The amendment clause explicitly removes or voids the original "
    "clause with no replacement.\n\n"
    "Return your assessment as structured output with:\n"
    "- action: one of REPLACE, MODIFY, UNCHANGED, DELETE\n"
    "- reasoning: brief explanation of your determination\n"
    "- confidence: your confidence in this assessment (0.0-1.0)"
)


# ---------------------------------------------------------------------------
# Chain building
# ---------------------------------------------------------------------------


def build_amendment_chain(doc_links: list[dict]) -> list[list[str]]:
    """Build ordered amendment chains from document link records.

    Given a list of link records (each with ``child_doc_id``,
    ``parent_doc_id``, and ``status``), construct chains that start at the
    root MSA and proceed through amendments in chronological order.

    Parameters
    ----------
    doc_links:
        Link records produced by Stage 4.  Each dict should have at least:
        - ``child_doc_id`` (str)
        - ``parent_doc_id`` (str)
        - ``status`` (str) -- only ``"LINKED"`` records are used.

    Returns
    -------
    list[list[str]] -- each inner list is an ordered chain of doc_ids from
    the root document to its final amendment.
    """
    log.info("building_amendment_chains", num_links=len(doc_links))

    # Filter to LINKED records only.
    linked = [lk for lk in doc_links if lk.get("status") == "LINKED"]

    # Build parent -> children mapping.
    children_of: dict[str, list[str]] = defaultdict(list)
    all_children: set[str] = set()

    for lk in linked:
        parent_id = lk["parent_doc_id"]
        child_id = lk["child_doc_id"]
        children_of[parent_id].append(child_id)
        all_children.add(child_id)

    # Root documents are those that appear as parents but never as children.
    all_parents = set(children_of.keys())
    roots = all_parents - all_children

    if not roots:
        log.warning("no_root_documents_found")
        return []

    # Walk from each root to build chains via DFS.
    chains: list[list[str]] = []

    def _walk(doc_id: str, current_chain: list[str]) -> None:
        current_chain.append(doc_id)
        kids = children_of.get(doc_id, [])
        if not kids:
            # Leaf node -- this chain is complete.
            chains.append(list(current_chain))
        else:
            for kid in kids:
                _walk(kid, current_chain)
        current_chain.pop()

    for root_id in sorted(roots):
        _walk(root_id, [])

    log.info(
        "amendment_chains_built",
        num_chains=len(chains),
        chain_lengths=[len(c) for c in chains],
    )
    return chains


# ---------------------------------------------------------------------------
# Clause comparison (LLM)
# ---------------------------------------------------------------------------


def compare_clauses(
    original_clause: str,
    amendment_clause: str,
    claude_client: Any = None,
) -> ResolutionResult:
    """Compare an original clause against an amendment clause using LLM.

    The LLM sees ONLY the two clauses and determines whether the amendment
    REPLACES, MODIFIES, leaves UNCHANGED, or DELETEs the original.

    Parameters
    ----------
    original_clause:
        The source clause text from the original (or previously resolved)
        obligation.
    amendment_clause:
        The source clause text from the amendment.
    claude_client:
        Optional pre-configured Anthropic client (useful for testing).

    Returns
    -------
    ResolutionResult with the comparison outcome.
    """
    log.info(
        "comparing_clauses",
        original_len=len(original_clause),
        amendment_len=len(amendment_clause),
    )

    client = claude_client or get_anthropic_client()

    user_prompt = (
        f"Original clause:\n{original_clause}\n\n"
        f"Amendment clause:\n{amendment_clause}"
    )

    parsed: _ComparisonResponse = extract_with_structured_output(
        client=client,
        system_prompt=_CLAUSE_COMPARISON_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_format=_ComparisonResponse,
    )

    resolution = ResolutionResult(
        action=parsed.action,
        original_clause=original_clause,
        amendment_clause=amendment_clause,
        reasoning=parsed.reasoning,
        confidence=parsed.confidence,
    )

    log.info(
        "clause_comparison_complete",
        action=resolution.action,
        confidence=resolution.confidence,
    )
    return resolution


# ---------------------------------------------------------------------------
# Single obligation resolution
# ---------------------------------------------------------------------------


def _clauses_potentially_related(
    original_text: str,
    amendment_text: str,
) -> bool:
    """Quick heuristic check for whether two obligation texts might be about
    the same subject matter.

    Uses keyword overlap as a cheap pre-filter before calling the LLM.
    """
    # Normalize and tokenise.
    stop_words = {
        "the", "a", "an", "and", "or", "of", "to", "in", "for", "on",
        "with", "by", "is", "are", "shall", "will", "must", "may", "that",
        "this", "from", "at", "be", "not", "as", "it", "its", "any", "all",
    }
    orig_words = {
        w.lower() for w in original_text.split() if w.lower() not in stop_words
    }
    amend_words = {
        w.lower() for w in amendment_text.split() if w.lower() not in stop_words
    }

    if not orig_words or not amend_words:
        return False

    overlap = orig_words & amend_words
    # If at least 20% of the smaller set overlaps, consider them related.
    min_size = min(len(orig_words), len(amend_words))
    return len(overlap) / min_size >= 0.20


def resolve_obligation(
    obligation: dict,
    amendment_obligations: list[dict],
    claude_client: Any = None,
) -> dict:
    """Resolve a single original obligation against amendment obligations.

    Walks through ``amendment_obligations`` in the order given (which should
    be chronological) and checks each to see if it supersedes, modifies, or
    deletes the original.

    Parameters
    ----------
    obligation:
        A single obligation dict (from extraction).  Expected keys include
        at least ``obligation_text``, ``source_clause``, and optionally
        ``obligation_type``.
    amendment_obligations:
        List of obligation dicts from amendment document(s), in
        chronological order.
    claude_client:
        Optional pre-configured Anthropic client.

    Returns
    -------
    dict -- the original obligation enriched with:
        - ``status``: ``"ACTIVE"`` | ``"SUPERSEDED"`` | ``"TERMINATED"``
        - ``amendment_history``: list of resolution records
    """
    log.info(
        "resolving_obligation",
        obligation=obligation.get("obligation_text", "")[:80],
        num_amendments=len(amendment_obligations),
    )

    history: list[dict] = []
    current_status = "ACTIVE"

    for amend_obl in amendment_obligations:
        # Skip if already terminated -- no further amendments matter.
        if current_status == "TERMINATED":
            break

        # Quick heuristic: skip comparison if clauses are clearly unrelated.
        # Always compare if obligation types match (e.g. both "SLA").
        orig_text = obligation.get("obligation_text", "")
        amend_text = amend_obl.get("obligation_text", "")
        same_type = (
            obligation.get("obligation_type")
            and obligation.get("obligation_type") == amend_obl.get("obligation_type")
        )
        if not same_type and not _clauses_potentially_related(orig_text, amend_text):
            continue

        # LLM comparison.
        resolution = compare_clauses(
            original_clause=obligation.get("source_clause", ""),
            amendment_clause=amend_obl.get("source_clause", ""),
            claude_client=claude_client,
        )

        record = {
            "amendment_obligation_text": amend_obl.get("obligation_text", ""),
            "amendment_source_clause": amend_obl.get("source_clause", ""),
            "action": resolution.action,
            "reasoning": resolution.reasoning,
            "confidence": resolution.confidence,
            "doc_id": amend_obl.get("_source_doc_id"),
            "doc_filename": amend_obl.get("_source_doc_filename"),
            "amendment_number": amend_obl.get("_amendment_number"),
        }
        history.append(record)

        if resolution.action == "REPLACE":
            current_status = "SUPERSEDED"
            log.info(
                "obligation_superseded",
                obligation=orig_text[:80],
                by=amend_text[:80],
            )
        elif resolution.action == "DELETE":
            current_status = "TERMINATED"
            log.info(
                "obligation_terminated",
                obligation=orig_text[:80],
                by=amend_text[:80],
            )
        elif resolution.action == "MODIFY":
            # Modified obligations stay ACTIVE but their history records the
            # modification.
            log.info(
                "obligation_modified",
                obligation=orig_text[:80],
                by=amend_text[:80],
            )

    resolved = dict(obligation)
    resolved["status"] = current_status
    resolved["amendment_history"] = history

    log.info(
        "obligation_resolved",
        obligation=obligation.get("obligation_text", "")[:80],
        status=current_status,
        history_length=len(history),
    )
    return resolved


# ---------------------------------------------------------------------------
# Chain resolution
# ---------------------------------------------------------------------------


def _get_doc_obligations(doc_id: str, documents: list[dict]) -> list[dict]:
    """Extract obligations for a given document from the documents list."""
    for doc in documents:
        if doc.get("doc_id") == doc_id or doc.get("id") == doc_id:
            return doc.get("obligations", [])
    return []


def resolve_amendment_chain(
    chain_docs: list[dict],
    claude_client: Any = None,
) -> list[dict]:
    """Resolve one full amendment chain.

    The first document in ``chain_docs`` is the MSA; subsequent entries are
    amendments in chronological order.  Each document dict should have at
    least ``doc_id`` (or ``id``) and ``obligations`` (list of obligation
    dicts).

    Parameters
    ----------
    chain_docs:
        Ordered list of document dicts (MSA first, then amendments).
    claude_client:
        Optional pre-configured Anthropic client.

    Returns
    -------
    list[dict] -- all obligations from the chain with final statuses.
    """
    if not chain_docs:
        return []

    log.info(
        "resolving_amendment_chain",
        chain_length=len(chain_docs),
        doc_ids=[d.get("doc_id") or d.get("id") for d in chain_docs],
    )

    msa_doc = chain_docs[0]
    msa_obligations = msa_doc.get("obligations", [])

    # Collect all amendment obligations in chronological order,
    # tagging each with document metadata for history tracking.
    amendment_obligations: list[dict] = []
    for amend_idx, amend_doc in enumerate(chain_docs[1:], start=1):
        doc_id = amend_doc.get("doc_id") or amend_doc.get("id")
        doc_filename = amend_doc.get("filename")
        for obl in amend_doc.get("obligations", []):
            tagged = dict(obl)
            tagged["_source_doc_id"] = doc_id
            tagged["_source_doc_filename"] = doc_filename
            tagged["_amendment_number"] = amend_idx
            amendment_obligations.append(tagged)

    # Resolve each MSA obligation against the full amendment chain.
    resolved: list[dict] = []
    for obl in msa_obligations:
        resolved_obl = resolve_obligation(
            obl,
            amendment_obligations,
            claude_client=claude_client,
        )
        resolved_obl["source_doc_id"] = msa_doc.get("doc_id") or msa_doc.get("id")
        resolved.append(resolved_obl)

    # Also include amendment obligations themselves (they are ACTIVE by
    # definition since they represent the latest version).
    for amend_doc in chain_docs[1:]:
        doc_id = amend_doc.get("doc_id") or amend_doc.get("id")
        for obl in amend_doc.get("obligations", []):
            amend_entry = dict(obl)
            amend_entry["status"] = "ACTIVE"
            amend_entry["amendment_history"] = []
            amend_entry["source_doc_id"] = doc_id
            resolved.append(amend_entry)

    log.info(
        "amendment_chain_resolved",
        total_obligations=len(resolved),
        active=sum(1 for r in resolved if r["status"] == "ACTIVE"),
        superseded=sum(1 for r in resolved if r["status"] == "SUPERSEDED"),
        terminated=sum(1 for r in resolved if r["status"] == "TERMINATED"),
    )
    return resolved


# ---------------------------------------------------------------------------
# Public API -- main entry point
# ---------------------------------------------------------------------------


def resolve_all(
    documents: list[dict],
    links: list[dict],
    claude_client: Any = None,
) -> list[dict]:
    """Resolve amendment chains across all documents.

    This is the main entry point for Stage 5.  It builds amendment chains
    from the link records, resolves each chain, and returns all obligations
    with updated statuses.

    Documents that are not part of any chain (UNLINKED) retain their
    obligations with status ``"UNRESOLVED"``.

    Parameters
    ----------
    documents:
        All documents with their extracted obligations.  Each dict should
        have ``doc_id`` (or ``id``), ``doc_type``, and ``obligations``
        (list of obligation dicts).
    links:
        Link records from Stage 4.  Each dict should have ``child_doc_id``,
        ``parent_doc_id``, and ``status``.
    claude_client:
        Optional pre-configured Anthropic client.

    Returns
    -------
    list[dict] -- all obligations with updated statuses:
        - ``"ACTIVE"`` -- obligation is currently in force
        - ``"SUPERSEDED"`` -- replaced by an amendment
        - ``"TERMINATED"`` -- explicitly deleted by an amendment
        - ``"UNRESOLVED"`` -- document not linked; resolution not possible
    """
    log.info(
        "resolve_all_start",
        num_documents=len(documents),
        num_links=len(links),
    )

    # Build chains.
    chains = build_amendment_chain(links)

    # Build a quick lookup from doc_id to document dict.
    doc_lookup: dict[str, dict] = {}
    for doc in documents:
        doc_id = doc.get("doc_id") or doc.get("id")
        if doc_id:
            doc_lookup[doc_id] = doc

    # Track which documents are part of any chain.
    linked_doc_ids: set[str] = set()
    for chain in chains:
        linked_doc_ids.update(chain)

    all_obligations: list[dict] = []

    # Resolve each chain.
    for chain in chains:
        chain_docs = []
        for doc_id in chain:
            doc = doc_lookup.get(doc_id)
            if doc:
                chain_docs.append(doc)
            else:
                log.warning("document_not_found_in_lookup", doc_id=doc_id)

        if chain_docs:
            resolved = resolve_amendment_chain(
                chain_docs,
                claude_client=claude_client,
            )
            all_obligations.extend(resolved)

    # Handle unlinked documents -- their obligations stay UNRESOLVED.
    for doc in documents:
        doc_id = doc.get("doc_id") or doc.get("id")
        if doc_id not in linked_doc_ids:
            log.info("unlinked_document_skipped", doc_id=doc_id)
            for obl in doc.get("obligations", []):
                entry = dict(obl)
                entry["status"] = "UNRESOLVED"
                entry["amendment_history"] = []
                entry["source_doc_id"] = doc_id
                all_obligations.append(entry)

    log.info(
        "resolve_all_complete",
        total_obligations=len(all_obligations),
        active=sum(1 for o in all_obligations if o["status"] == "ACTIVE"),
        superseded=sum(1 for o in all_obligations if o["status"] == "SUPERSEDED"),
        terminated=sum(1 for o in all_obligations if o["status"] == "TERMINATED"),
        unresolved=sum(1 for o in all_obligations if o["status"] == "UNRESOLVED"),
    )
    return all_obligations
