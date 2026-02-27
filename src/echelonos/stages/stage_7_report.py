"""Stage 7: Report Generation (Obligation Matrix + Flag Report).

Builds the final deliverable report from extracted obligations, document
metadata, and linking results.  All functions are pure -- they accept and
return plain dicts / Pydantic models so that the Prefect flow layer is
responsible for all database I/O.

The report consists of three sections:
  1. **Obligation Matrix** -- per-vendor obligation table sorted by status,
     type, and party, with formatted source references.
  2. **Flag Report** -- actionable flags for unverified, unlinked, ambiguous,
     unresolved, or low-confidence items.
  3. **Summary** -- aggregate counts by obligation type, status, responsible
     party, and flag severity.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import BaseModel

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Sort-order constants
# ---------------------------------------------------------------------------

_STATUS_ORDER: dict[str, int] = {
    "ACTIVE": 0,
    "UNRESOLVED": 1,
    "SUPERSEDED": 2,
    "TERMINATED": 3,
}

_SEVERITY_FOR_FLAG: dict[str, str] = {
    "UNVERIFIED": "RED",
    "AMBIGUOUS": "ORANGE",
    "UNLINKED": "YELLOW",
    "UNRESOLVED": "YELLOW",
    "LOW_CONFIDENCE": "WHITE",
}

# Confidence threshold below which we flag an obligation.
_LOW_CONFIDENCE_THRESHOLD: float = 0.80


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ObligationRow(BaseModel):
    """A single row in the obligation matrix table."""

    number: int
    obligation_text: str
    obligation_type: str
    responsible_party: str
    counterparty: str
    source: str  # e.g. "SOW S4.2 (Amd #2 modified)"
    status: str  # ACTIVE | SUPERSEDED | UNRESOLVED | TERMINATED
    frequency: str | None = None
    deadline: str | None = None
    confidence: float
    source_clause: str | None = None
    source_page: int | None = None
    doc_filename: str | None = None
    amendment_history: list[dict] | None = None


class FlagItem(BaseModel):
    """A single entry in the flag report."""

    flag_type: str  # UNVERIFIED | UNLINKED | AMBIGUOUS | UNRESOLVED | LOW_CONFIDENCE
    severity: str  # RED | ORANGE | YELLOW | WHITE
    entity_type: str  # "obligation" | "document"
    entity_id: str
    message: str


class ObligationReport(BaseModel):
    """Complete report combining the obligation matrix, flags, and summary."""

    org_name: str
    generated_at: str  # ISO timestamp
    total_obligations: int
    active_obligations: int
    superseded_obligations: int
    unresolved_obligations: int
    obligations: list[ObligationRow]
    flags: list[FlagItem]
    summary: dict  # counts by type, by status, by party


# ---------------------------------------------------------------------------
# Source-reference formatting
# ---------------------------------------------------------------------------


def _format_source(
    obligation: dict[str, Any],
    documents: dict[str, dict[str, Any]],
    links: list[dict[str, Any]],
) -> str:
    """Format the source reference for an obligation row.

    Produces strings like ``"SOW S4.2"`` or ``"SOW S4.2 (Amd #2 modified)"``.

    Parameters
    ----------
    obligation:
        The obligation dict (must have ``doc_id`` and optionally
        ``source_clause``).
    documents:
        Mapping of doc_id -> document dict.  Each document should have at
        least ``doc_type``.
    links:
        List of link dicts from Stage 4, each with ``child_doc_id``,
        ``parent_doc_id``, and ``status``.
    """
    doc_id = obligation.get("doc_id", "")
    doc = documents.get(str(doc_id), {})
    doc_type = doc.get("doc_type", "Unknown")
    clause = obligation.get("source_clause", "")

    # Build the base: "DOC_TYPE Ssection"
    section = _extract_section_ref(clause)
    if section:
        base = f"{doc_type} {section}"
    else:
        base = doc_type

    # Check whether this document is an amendment that modifies a parent.
    amendment_suffix = _get_amendment_suffix(str(doc_id), doc, links, documents)
    if amendment_suffix:
        return f"{base} ({amendment_suffix})"
    return base


def _extract_section_ref(clause: str) -> str:
    """Extract a section reference like 'S4.2' from a source clause string."""
    if not clause:
        return ""

    # Look for patterns like "Section 4.2", "SS4.2", "ss4.2", "Art. 3"
    patterns = [
        r"[Ss]ection\s+([\d]+(?:\.[\d]+)*)",
        r"\u00a7\s*([\d]+(?:\.[\d]+)*)",  # Unicode section sign
        r"[Aa]rt(?:icle)?\.?\s*([\d]+(?:\.[\d]+)*)",
    ]
    for pat in patterns:
        m = re.search(pat, clause)
        if m:
            return f"\u00a7{m.group(1)}"

    return ""


def _get_amendment_suffix(
    doc_id: str,
    doc: dict[str, Any],
    links: list[dict[str, Any]],
    documents: dict[str, dict[str, Any]],
) -> str:
    """If *doc* is an amendment, return a suffix like ``'Amd #2 modified'``.

    We look at the linking results to find whether *doc_id* appears as a child
    document that links to a parent (meaning it is an amendment or addendum
    modifying the parent).
    """
    doc_type = doc.get("doc_type", "")
    if doc_type not in ("Amendment", "Addendum"):
        return ""

    # Find the link entry for this child doc.
    for link in links:
        if str(link.get("child_doc_id", "")) == doc_id:
            if link.get("status") == "LINKED":
                # Determine the amendment number.  We count how many
                # amendments share the same parent, ordered by their
                # position in the links list.
                parent_id = str(link.get("parent_doc_id", ""))
                siblings = [
                    lk for lk in links
                    if str(lk.get("parent_doc_id", "")) == parent_id
                    and lk.get("status") == "LINKED"
                ]
                # Find this doc's position among its siblings.
                for idx, sib in enumerate(siblings, start=1):
                    if str(sib.get("child_doc_id", "")) == doc_id:
                        return f"Amd #{idx} modified"
                return "Amd modified"

    return ""


# ---------------------------------------------------------------------------
# Obligation matrix builder
# ---------------------------------------------------------------------------


def build_obligation_matrix(
    obligations: list[dict[str, Any]],
    documents: dict[str, dict[str, Any]],
    links: list[dict[str, Any]],
) -> list[ObligationRow]:
    """Build the per-vendor obligation table.

    Parameters
    ----------
    obligations:
        List of obligation dicts (matching the Obligation DB model schema).
    documents:
        Mapping of doc_id -> document dict.
    links:
        List of link dicts from Stage 4.

    Returns
    -------
    list[ObligationRow] sorted by status (ACTIVE first), then obligation
    type, then responsible party.  Rows are numbered sequentially.
    """
    log.info("building_obligation_matrix", num_obligations=len(obligations))

    rows: list[ObligationRow] = []
    for obl in obligations:
        source = _format_source(obl, documents, links)
        # Resolve doc_filename from documents lookup.
        doc_id = obl.get("doc_id", "")
        doc = documents.get(str(doc_id), {})
        doc_filename = doc.get("filename")
        row = ObligationRow(
            number=0,  # placeholder; numbered after sorting
            obligation_text=obl.get("obligation_text", ""),
            obligation_type=obl.get("obligation_type", "Unknown"),
            responsible_party=obl.get("responsible_party", "Unknown"),
            counterparty=obl.get("counterparty", "Unknown"),
            source=source,
            status=obl.get("status", "ACTIVE"),
            frequency=obl.get("frequency"),
            deadline=obl.get("deadline"),
            confidence=obl.get("confidence", 0.0),
            source_clause=obl.get("source_clause"),
            source_page=obl.get("source_page"),
            doc_filename=doc_filename,
            amendment_history=obl.get("amendment_history"),
        )
        rows.append(row)

    # Sort: ACTIVE first, then by type, then by party.
    rows.sort(
        key=lambda r: (
            _STATUS_ORDER.get(r.status, 99),
            r.obligation_type,
            r.responsible_party,
        )
    )

    # Number rows sequentially.
    for idx, row in enumerate(rows, start=1):
        row.number = idx

    log.info("obligation_matrix_built", total_rows=len(rows))
    return rows


# ---------------------------------------------------------------------------
# Flag report builder
# ---------------------------------------------------------------------------


def build_flag_report(
    obligations: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> list[FlagItem]:
    """Generate actionable flags from obligations, documents, and links.

    Flag types and severities:

    * **UNVERIFIED** (RED) -- source clause not grounded or verification
      failed.
    * **UNLINKED** (YELLOW) -- amendment / addendum parent not found.
    * **AMBIGUOUS** (ORANGE) -- multiple parent candidates for a document.
    * **UNRESOLVED** (YELLOW) -- obligations from documents that are
      unlinked.
    * **LOW_CONFIDENCE** (WHITE) -- extraction confidence below 0.80.

    Parameters
    ----------
    obligations:
        List of obligation dicts.
    documents:
        List of document dicts.
    links:
        List of link dicts from Stage 4.

    Returns
    -------
    list[FlagItem]
    """
    log.info("building_flag_report")

    flags: list[FlagItem] = []

    # --- Obligation-level flags -------------------------------------------

    # Build set of doc_ids that are unlinked for UNRESOLVED check.
    unlinked_doc_ids: set[str] = set()
    for link in links:
        if link.get("status") == "UNLINKED":
            unlinked_doc_ids.add(str(link.get("child_doc_id", "")))

    for obl in obligations:
        obl_id = str(obl.get("id", ""))

        # UNVERIFIED: verification result is not VERIFIED, or missing.
        verification = obl.get("verification_result") or {}
        is_verified = verification.get("verified", False) if verification else False
        status = obl.get("status", "")

        if not is_verified and verification:
            flags.append(FlagItem(
                flag_type="UNVERIFIED",
                severity="RED",
                entity_type="obligation",
                entity_id=obl_id,
                message=f"Obligation verification failed: {obl.get('obligation_text', '')[:80]}",
            ))

        # LOW_CONFIDENCE: confidence below threshold.
        confidence = obl.get("confidence", 1.0)
        if confidence < _LOW_CONFIDENCE_THRESHOLD:
            flags.append(FlagItem(
                flag_type="LOW_CONFIDENCE",
                severity="WHITE",
                entity_type="obligation",
                entity_id=obl_id,
                message=(
                    f"Low extraction confidence ({confidence:.2f}): "
                    f"{obl.get('obligation_text', '')[:80]}"
                ),
            ))

        # UNRESOLVED: obligation belongs to an unlinked document.
        doc_id = str(obl.get("doc_id", ""))
        if doc_id in unlinked_doc_ids:
            flags.append(FlagItem(
                flag_type="UNRESOLVED",
                severity="YELLOW",
                entity_type="obligation",
                entity_id=obl_id,
                message=(
                    f"Obligation from unlinked document ({doc_id}): "
                    f"{obl.get('obligation_text', '')[:80]}"
                ),
            ))

    # --- Document-level flags ---------------------------------------------

    for link in links:
        link_status = link.get("status", "")
        child_doc_id = str(link.get("child_doc_id", ""))

        if link_status == "UNLINKED":
            flags.append(FlagItem(
                flag_type="UNLINKED",
                severity="YELLOW",
                entity_type="document",
                entity_id=child_doc_id,
                message=f"Amendment/addendum parent not found for document {child_doc_id}",
            ))

        if link_status == "AMBIGUOUS":
            candidates = link.get("candidates", [])
            flags.append(FlagItem(
                flag_type="AMBIGUOUS",
                severity="ORANGE",
                entity_type="document",
                entity_id=child_doc_id,
                message=(
                    f"Multiple parent candidates ({len(candidates)}) "
                    f"for document {child_doc_id}"
                ),
            ))

    log.info("flag_report_built", total_flags=len(flags))
    return flags


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------


def build_summary(
    obligations: list[ObligationRow],
    flags: list[FlagItem],
) -> dict[str, Any]:
    """Aggregate counts by obligation type, status, responsible party, and
    flag severity.

    Parameters
    ----------
    obligations:
        The sorted obligation rows from :func:`build_obligation_matrix`.
    flags:
        The flags from :func:`build_flag_report`.

    Returns
    -------
    dict with keys ``by_type``, ``by_status``, ``by_responsible_party``,
    ``flags_by_severity``, ``flags_by_type``.
    """
    log.info("building_summary")

    by_type: dict[str, int] = dict(Counter(r.obligation_type for r in obligations))
    by_status: dict[str, int] = dict(Counter(r.status for r in obligations))
    by_responsible_party: dict[str, int] = dict(Counter(r.responsible_party for r in obligations))
    flags_by_severity: dict[str, int] = dict(Counter(f.severity for f in flags))
    flags_by_type: dict[str, int] = dict(Counter(f.flag_type for f in flags))

    summary = {
        "by_type": by_type,
        "by_status": by_status,
        "by_responsible_party": by_responsible_party,
        "flags_by_severity": flags_by_severity,
        "flags_by_type": flags_by_type,
    }

    log.info("summary_built", summary=summary)
    return summary


# ---------------------------------------------------------------------------
# Main report generator
# ---------------------------------------------------------------------------


def generate_report(
    org_name: str,
    obligations: list[dict[str, Any]],
    documents: dict[str, dict[str, Any]],
    links: list[dict[str, Any]],
) -> ObligationReport:
    """Build the complete obligation report.

    This is the primary entry point for Stage 7.  It combines the obligation
    matrix, flag report, and summary into a single :class:`ObligationReport`.

    Parameters
    ----------
    org_name:
        Name of the organization the report covers.
    obligations:
        List of obligation dicts (matching the Obligation DB model schema).
    documents:
        Mapping of doc_id -> document dict.
    links:
        List of link dicts from Stage 4.

    Returns
    -------
    ObligationReport
    """
    log.info(
        "generating_report",
        org_name=org_name,
        num_obligations=len(obligations),
        num_documents=len(documents),
        num_links=len(links),
    )

    # Build the three report sections.
    matrix = build_obligation_matrix(obligations, documents, links)
    flag_list = build_flag_report(
        obligations,
        list(documents.values()),
        links,
    )
    summary = build_summary(matrix, flag_list)

    # Compute top-level counts.
    total = len(matrix)
    active = sum(1 for r in matrix if r.status == "ACTIVE")
    superseded = sum(1 for r in matrix if r.status == "SUPERSEDED")
    unresolved = sum(1 for r in matrix if r.status == "UNRESOLVED")

    report = ObligationReport(
        org_name=org_name,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_obligations=total,
        active_obligations=active,
        superseded_obligations=superseded,
        unresolved_obligations=unresolved,
        obligations=matrix,
        flags=flag_list,
        summary=summary,
    )

    log.info(
        "report_generated",
        org_name=org_name,
        total_obligations=total,
        active=active,
        superseded=superseded,
        unresolved=unresolved,
        total_flags=len(flag_list),
    )
    return report


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------

_SEVERITY_INDICATOR: dict[str, str] = {
    "RED": "[RED]",
    "ORANGE": "[ORANGE]",
    "YELLOW": "[YELLOW]",
    "WHITE": "[WHITE]",
}


def export_to_markdown(report: ObligationReport) -> str:
    """Export the report as a formatted Markdown string.

    Includes:
      - Header with org name and timestamp
      - Obligation matrix as a Markdown table
      - Flag report as a bullet list with severity indicators
      - Summary section with counts

    Parameters
    ----------
    report:
        The complete :class:`ObligationReport`.

    Returns
    -------
    str -- the rendered Markdown.
    """
    log.info("exporting_to_markdown", org_name=report.org_name)

    lines: list[str] = []

    # --- Header -----------------------------------------------------------
    lines.append(f"# Obligation Report: {report.org_name}")
    lines.append("")
    lines.append(f"Generated: {report.generated_at}")
    lines.append("")

    # --- Top-level counts -------------------------------------------------
    lines.append("## Overview")
    lines.append("")
    lines.append(f"- **Total obligations:** {report.total_obligations}")
    lines.append(f"- **Active:** {report.active_obligations}")
    lines.append(f"- **Superseded:** {report.superseded_obligations}")
    lines.append(f"- **Unresolved:** {report.unresolved_obligations}")
    lines.append("")

    # --- Obligation matrix ------------------------------------------------
    lines.append("## Obligation Matrix")
    lines.append("")

    if report.obligations:
        # Table header.
        lines.append(
            "| # | Obligation | Type | Responsible | Counterparty "
            "| Source | Status | Frequency | Deadline | Confidence |"
        )
        lines.append(
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        )
        for row in report.obligations:
            freq = row.frequency or "-"
            dl = row.deadline or "-"
            lines.append(
                f"| {row.number} "
                f"| {row.obligation_text} "
                f"| {row.obligation_type} "
                f"| {row.responsible_party} "
                f"| {row.counterparty} "
                f"| {row.source} "
                f"| {row.status} "
                f"| {freq} "
                f"| {dl} "
                f"| {row.confidence:.2f} |"
            )
    else:
        lines.append("_No obligations found._")

    lines.append("")

    # --- Flag report ------------------------------------------------------
    lines.append("## Flag Report")
    lines.append("")

    if report.flags:
        for flag in report.flags:
            indicator = _SEVERITY_INDICATOR.get(flag.severity, flag.severity)
            lines.append(
                f"- {indicator} **{flag.flag_type}** "
                f"({flag.entity_type}: {flag.entity_id}): {flag.message}"
            )
    else:
        lines.append("_No flags._")

    lines.append("")

    # --- Summary ----------------------------------------------------------
    lines.append("## Summary")
    lines.append("")

    summary = report.summary
    if summary.get("by_type"):
        lines.append("### By Type")
        lines.append("")
        for key, count in sorted(summary["by_type"].items()):
            lines.append(f"- {key}: {count}")
        lines.append("")

    if summary.get("by_status"):
        lines.append("### By Status")
        lines.append("")
        for key, count in sorted(summary["by_status"].items()):
            lines.append(f"- {key}: {count}")
        lines.append("")

    if summary.get("by_responsible_party"):
        lines.append("### By Responsible Party")
        lines.append("")
        for key, count in sorted(summary["by_responsible_party"].items()):
            lines.append(f"- {key}: {count}")
        lines.append("")

    if summary.get("flags_by_severity"):
        lines.append("### Flags by Severity")
        lines.append("")
        for key, count in sorted(summary["flags_by_severity"].items()):
            lines.append(f"- {key}: {count}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------


def export_to_json(report: ObligationReport) -> str:
    """Export the report as a JSON string.

    Parameters
    ----------
    report:
        The complete :class:`ObligationReport`.

    Returns
    -------
    str -- pretty-printed JSON.
    """
    log.info("exporting_to_json", org_name=report.org_name)
    return report.model_dump_json(indent=2)
