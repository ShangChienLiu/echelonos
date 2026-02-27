"""Stage 4: Document Linking (SQL-based, no LLM).

Links child documents (Amendments, Addendums, SOWs) to their parent contracts
by parsing parent reference strings and matching against the organization's
document corpus.  All functions are pure -- they accept and return plain dicts
so that the Prefect flow layer is responsible for all database I/O.

Matching strategy:
  1. Parse the ``parent_reference_raw`` string from the child document.
  2. Compare parsed components (doc_type, date, parties) against every
     candidate document in the same organization.
  3. Exactly one match  -> LINKED
     Zero matches        -> UNLINKED  (dangling reference)
     Multiple matches    -> AMBIGUOUS (requires human review)
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import structlog
from dateutil import parser as dateutil_parser

log = structlog.get_logger(__name__)

# Document types that reference a parent contract.
LINKABLE_DOC_TYPES: set[str] = {"Amendment", "Addendum", "SOW"}

# Regex for extracting a leading doc-type token from a reference string.
# e.g. "MSA dated ..."  or  "Master Services Agreement dated ..."
_DOC_TYPE_PATTERN = re.compile(
    r"^(MSA|NDA|SOW|Master Services Agreement|Non-Disclosure Agreement|"
    r"Statement of Work|Order Form|Agreement|Contract)\b",
    re.IGNORECASE,
)

# Regex for "between <Party1> and <Party2>" fragments.
_PARTIES_PATTERN = re.compile(
    r"\bbetween\s+(.+?)\s+and\s+(.+?)(?:\s+dated\b|\s+effective\b|$)",
    re.IGNORECASE,
)

# Known abbreviation -> canonical doc type mapping.
_DOC_TYPE_ALIASES: dict[str, str] = {
    "msa": "MSA",
    "master services agreement": "MSA",
    "nda": "NDA",
    "non-disclosure agreement": "NDA",
    "sow": "SOW",
    "statement of work": "SOW",
    "order form": "Order Form",
    "agreement": None,  # generic -- keep None
    "contract": None,
}


# ---------------------------------------------------------------------------
# Reference parsing
# ---------------------------------------------------------------------------


def parse_parent_reference(reference_raw: str) -> dict[str, Any]:
    """Parse a free-text parent-reference string into structured components.

    Parameters
    ----------
    reference_raw:
        The raw reference text extracted from a child document, e.g.
        ``"MSA dated January 10, 2023"`` or
        ``"Agreement between CDW and Acme dated 2023-01-10"``.

    Returns
    -------
    dict with keys:
        - ``doc_type`` (str | None) -- canonical doc type or None
        - ``date``     (str | None) -- ISO-8601 date string or None
        - ``parties``  (list[str])  -- extracted party names (may be empty)
    """
    log.debug("parsing_parent_reference", reference_raw=reference_raw)
    result: dict[str, Any] = {"doc_type": None, "date": None, "parties": []}

    if not reference_raw or not reference_raw.strip():
        return result

    text = reference_raw.strip()

    # --- Extract doc type ---------------------------------------------------
    m = _DOC_TYPE_PATTERN.match(text)
    if m:
        raw_type = m.group(1).strip().lower()
        result["doc_type"] = _DOC_TYPE_ALIASES.get(raw_type, raw_type.upper())

    # --- Extract parties ----------------------------------------------------
    pm = _PARTIES_PATTERN.search(text)
    if pm:
        p1 = pm.group(1).strip().strip(",").strip()
        p2 = pm.group(2).strip().strip(",").strip()
        result["parties"] = [p1, p2]

    # --- Extract date -------------------------------------------------------
    result["date"] = _extract_date(text)

    log.debug("parsed_parent_reference", result=result)
    return result


def _extract_date(text: str) -> str | None:
    """Try to pull a date from *text* using ``dateutil.parser``.

    Looks first for an explicit ``dated <date>`` or ``effective <date>``
    phrase.  Falls back to a best-effort parse of the full string.

    Returns an ISO-8601 date string (``YYYY-MM-DD``) or ``None``.
    """
    # Try explicit "dated ..." or "effective ..." suffix first.
    for keyword in ("dated", "effective"):
        pattern = re.compile(
            rf"\b{keyword}\s+(.+?)$",
            re.IGNORECASE,
        )
        m = pattern.search(text)
        if m:
            candidate = m.group(1).strip()
            parsed = _try_parse_date(candidate)
            if parsed:
                return parsed

    # Fallback: attempt to find any date-like substring.
    # Look for common date patterns in the string.
    date_patterns = [
        # ISO: 2023-01-10
        r"\b(\d{4}-\d{1,2}-\d{1,2})\b",
        # US: 01/10/2023 or 1/10/2023
        r"\b(\d{1,2}/\d{1,2}/\d{4})\b",
        # Long: January 10, 2023
        r"\b([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})\b",
    ]
    for pat in date_patterns:
        m = re.search(pat, text)
        if m:
            parsed = _try_parse_date(m.group(1))
            if parsed:
                return parsed

    return None


def _try_parse_date(text: str) -> str | None:
    """Attempt to parse *text* as a date.  Return ``YYYY-MM-DD`` or None."""
    try:
        dt = dateutil_parser.parse(text, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------


def _normalize(s: str) -> str:
    """Lowercase and strip extra whitespace for comparison."""
    return " ".join(s.lower().split())


def _parties_overlap(child_parties: list[str], doc_parties: list[str]) -> bool:
    """Return True if *any* child party matches a document party.

    Comparison is case-insensitive and whitespace-normalized.
    """
    if not child_parties or not doc_parties:
        return False
    child_set = {_normalize(p) for p in child_parties}
    doc_set = {_normalize(p) for p in doc_parties}
    return bool(child_set & doc_set)


def _dates_match(parsed_date: str | None, doc_effective_date: str | None) -> bool:
    """Return True if the two date strings resolve to the same calendar day.

    Both inputs should be ISO-8601 date strings (``YYYY-MM-DD``), or
    datetime-like strings that ``dateutil`` can parse.
    """
    if parsed_date is None or doc_effective_date is None:
        return False
    try:
        d1 = dateutil_parser.parse(parsed_date).date()
        d2 = dateutil_parser.parse(str(doc_effective_date)).date()
        return d1 == d2
    except (ValueError, OverflowError, TypeError):
        return False


def _doc_type_matches(parsed_type: str | None, doc_type: str | None) -> bool:
    """Return True if the parsed type from the reference matches the
    candidate document's type.

    If the parsed type is ``None`` (e.g. the reference just said "Agreement"
    without a specific type), we treat it as a wildcard and return True for
    any document.
    """
    if parsed_type is None:
        # Generic reference -- cannot filter by type.
        return True
    if doc_type is None:
        return False
    return _normalize(parsed_type) == _normalize(doc_type)


# ---------------------------------------------------------------------------
# Core matching
# ---------------------------------------------------------------------------


def find_parent_document(
    child_doc: dict[str, Any],
    org_documents: list[dict[str, Any]],
) -> dict[str, Any]:
    """Find the parent document for a child document within an organization.

    Parameters
    ----------
    child_doc:
        Dict with at least ``parent_reference_raw`` and ``id``.
    org_documents:
        All documents in the same organization.  Each dict should have
        ``id``, ``doc_type``, ``effective_date``, and ``parties``.

    Returns
    -------
    dict with:
        - ``status``       -- ``"LINKED"`` | ``"UNLINKED"`` | ``"AMBIGUOUS"``
        - ``parent_doc_id`` -- id of matched parent, or None
        - ``candidates``   -- list of candidate dicts (for AMBIGUOUS)
        - ``child_doc_id`` -- echoed back for convenience
    """
    child_id = child_doc.get("id")
    raw_ref = child_doc.get("parent_reference_raw", "")

    log.info(
        "finding_parent_document",
        child_doc_id=child_id,
        parent_reference_raw=raw_ref,
    )

    parsed = parse_parent_reference(raw_ref)
    candidates: list[dict[str, Any]] = []

    for doc in org_documents:
        # Skip the child itself.
        if doc.get("id") == child_id:
            continue

        # --- Matching criteria ------------------------------------------------
        type_ok = _doc_type_matches(parsed["doc_type"], doc.get("doc_type"))
        date_ok = _dates_match(parsed["date"], doc.get("effective_date"))
        parties_ok = _parties_overlap(
            parsed["parties"], doc.get("parties") or []
        )

        # Scoring: require date match as the primary signal, then refine with
        # type and parties when available.
        if not date_ok:
            continue

        # If we have both type and parties from the reference, require at
        # least one of them to match (in addition to date).
        if parsed["doc_type"] and parsed["parties"]:
            if type_ok or parties_ok:
                candidates.append(doc)
        elif parsed["doc_type"]:
            if type_ok:
                candidates.append(doc)
        elif parsed["parties"]:
            if parties_ok:
                candidates.append(doc)
        else:
            # Only date was parsed -- accept any date match.
            candidates.append(doc)

    # --- Determine link status ------------------------------------------------
    if len(candidates) == 1:
        log.info(
            "parent_linked",
            child_doc_id=child_id,
            parent_doc_id=candidates[0]["id"],
        )
        return {
            "status": "LINKED",
            "parent_doc_id": candidates[0]["id"],
            "candidates": [_candidate_summary(c) for c in candidates],
            "child_doc_id": child_id,
        }
    elif len(candidates) == 0:
        log.warning("parent_unlinked", child_doc_id=child_id)
        return {
            "status": "UNLINKED",
            "parent_doc_id": None,
            "candidates": [],
            "child_doc_id": child_id,
        }
    else:
        log.warning(
            "parent_ambiguous",
            child_doc_id=child_id,
            candidate_count=len(candidates),
        )
        return {
            "status": "AMBIGUOUS",
            "parent_doc_id": None,
            "candidates": [_candidate_summary(c) for c in candidates],
            "child_doc_id": child_id,
        }


def _candidate_summary(doc: dict[str, Any]) -> dict[str, Any]:
    """Build a slim summary dict for a candidate document."""
    return {
        "id": doc.get("id"),
        "doc_type": doc.get("doc_type"),
        "effective_date": doc.get("effective_date"),
        "parties": doc.get("parties"),
    }


# ---------------------------------------------------------------------------
# Batch linking
# ---------------------------------------------------------------------------


def link_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Process all documents that need linking.

    Only documents whose ``doc_type`` is in :data:`LINKABLE_DOC_TYPES` and
    that have a non-empty ``parent_reference_raw`` are processed.

    Parameters
    ----------
    documents:
        Full list of documents in an organization.  Each dict should have
        at least ``id``, ``doc_type``, ``parent_reference_raw``,
        ``org_id``, ``effective_date``, and ``parties``.

    Returns
    -------
    list[dict] -- one linking result per processed child document.
    """
    log.info("link_documents_start", total_documents=len(documents))

    # Group documents by org_id.
    orgs: dict[str, list[dict[str, Any]]] = {}
    for doc in documents:
        org_id = doc.get("org_id", "default")
        orgs.setdefault(str(org_id), []).append(doc)

    results: list[dict[str, Any]] = []

    for org_id, org_docs in orgs.items():
        for doc in org_docs:
            doc_type = doc.get("doc_type", "")
            ref_raw = doc.get("parent_reference_raw")

            if doc_type not in LINKABLE_DOC_TYPES:
                continue
            if not ref_raw or not ref_raw.strip():
                continue

            result = find_parent_document(doc, org_docs)
            results.append(result)

    log.info(
        "link_documents_complete",
        processed=len(results),
        linked=sum(1 for r in results if r["status"] == "LINKED"),
        unlinked=sum(1 for r in results if r["status"] == "UNLINKED"),
        ambiguous=sum(1 for r in results if r["status"] == "AMBIGUOUS"),
    )
    return results


# ---------------------------------------------------------------------------
# Backfill dangling references
# ---------------------------------------------------------------------------


def backfill_dangling_references(
    new_doc: dict[str, Any],
    dangling_refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Check whether *new_doc* resolves any existing dangling references.

    When a new document is ingested (e.g. a late-arriving MSA), we re-run the
    matching logic for every dangling reference to see if the new document is
    the missing parent.

    Parameters
    ----------
    new_doc:
        The newly ingested document dict (``id``, ``doc_type``,
        ``effective_date``, ``parties``).
    dangling_refs:
        List of dangling reference dicts, each with at least
        ``doc_id`` (the child that has the dangling ref),
        ``reference_text`` (the raw reference string), and ``id``.

    Returns
    -------
    list[dict] -- one entry per resolved reference, each containing:
        - ``dangling_ref_id`` -- id of the resolved DanglingReference row
        - ``child_doc_id``    -- the child document that was waiting
        - ``parent_doc_id``   -- the newly matched parent (== new_doc id)
        - ``status``          -- ``"LINKED"``
    """
    log.info(
        "backfill_dangling_start",
        new_doc_id=new_doc.get("id"),
        dangling_count=len(dangling_refs),
    )

    resolved: list[dict[str, Any]] = []

    for ref in dangling_refs:
        raw_text = ref.get("reference_text", "")
        if not raw_text:
            continue

        parsed = parse_parent_reference(raw_text)

        type_ok = _doc_type_matches(parsed["doc_type"], new_doc.get("doc_type"))
        date_ok = _dates_match(parsed["date"], new_doc.get("effective_date"))
        parties_ok = _parties_overlap(
            parsed["parties"], new_doc.get("parties") or []
        )

        # Apply the same matching logic as find_parent_document.
        matched = False
        if date_ok:
            if parsed["doc_type"] and parsed["parties"]:
                matched = type_ok or parties_ok
            elif parsed["doc_type"]:
                matched = type_ok
            elif parsed["parties"]:
                matched = parties_ok
            else:
                matched = True  # Only date matched.

        if matched:
            log.info(
                "dangling_reference_resolved",
                dangling_ref_id=ref.get("id"),
                child_doc_id=ref.get("doc_id"),
                parent_doc_id=new_doc.get("id"),
            )
            resolved.append(
                {
                    "dangling_ref_id": ref.get("id"),
                    "child_doc_id": ref.get("doc_id"),
                    "parent_doc_id": new_doc.get("id"),
                    "status": "LINKED",
                }
            )

    log.info(
        "backfill_dangling_complete",
        new_doc_id=new_doc.get("id"),
        resolved_count=len(resolved),
    )
    return resolved
