"""Stage 0b: File Deduplication via 4-Layer Hash Pipeline.

Layer 1 - File Hash (SHA-256): Hash raw bytes to catch exact copies.
Layer 2 - Content Hash: Extract text, normalize, hash to catch format variants.
Layer 3 - SimHash (64-bit): Fingerprint comparison with Hamming distance <= 1 for near-duplicates.
Layer 4 - Structural Fingerprint: Hash of (doc_type + date + parties) protects amendments/SOWs.
"""

from __future__ import annotations

import hashlib
import re
import string

import structlog
from simhash import Simhash

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Layer 1: File Hash (SHA-256 of raw bytes)
# ---------------------------------------------------------------------------


def compute_file_hash(file_path: str) -> str:
    """Compute SHA-256 hash of the raw file bytes.

    Catches exact copies regardless of filename.
    """
    sha = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------


def extract_text(file_path: str) -> str:
    """Extract plain text from a PDF or DOCX file.

    File type is detected by extension.  Returns an empty string when
    extraction fails or the format is unsupported.
    """
    lower = file_path.lower()
    if lower.endswith(".pdf"):
        return _extract_pdf_text(file_path)
    elif lower.endswith(".docx"):
        return _extract_docx_text(file_path)
    else:
        logger.warning("extract_text.unsupported_format", file_path=file_path)
        return ""


def _extract_pdf_text(file_path: str) -> str:
    """Extract text from a PDF using pypdf."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(file_path)
        pages_text: list[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)
        return "\n".join(pages_text)
    except Exception:
        logger.exception("extract_text.pdf_error", file_path=file_path)
        return ""


def _extract_docx_text(file_path: str) -> str:
    """Extract text from a DOCX using python-docx."""
    from docx import Document

    try:
        doc = Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception:
        logger.exception("extract_text.docx_error", file_path=file_path)
        return ""


# ---------------------------------------------------------------------------
# Layer 2: Content Hash (normalized text -> SHA-256)
# ---------------------------------------------------------------------------

_PUNCTUATION_TABLE = str.maketrans("", "", string.punctuation)


def _normalize_text(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation."""
    text = text.lower()
    text = text.translate(_PUNCTUATION_TABLE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def compute_content_hash(text: str) -> str:
    """Normalize *text* then return its SHA-256 hex digest.

    Catches copies that differ only in formatting or file type.
    """
    normalized = _normalize_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Layer 3: SimHash (64-bit locality-sensitive hash)
# ---------------------------------------------------------------------------


def compute_simhash(text: str) -> int:
    """Return a 64-bit SimHash fingerprint of *text*.

    The fingerprint is built from whitespace-delimited tokens of the
    normalized text so that minor edits produce a nearby hash.
    """
    normalized = _normalize_text(text)
    tokens = normalized.split()
    if not tokens:
        return Simhash("").value
    return Simhash(tokens).value


def hamming_distance(hash1: int, hash2: int) -> int:
    """Count the number of differing bits between two integers."""
    return bin(hash1 ^ hash2).count("1")


# ---------------------------------------------------------------------------
# Layer 4: Structural Fingerprint
# ---------------------------------------------------------------------------


def compute_structural_fingerprint(
    doc_type: str,
    date: str,
    parties: list[str],
) -> str:
    """Hash (doc_type, date, sorted parties) to fingerprint contract identity.

    Two documents that share the same structural fingerprint describe the
    *same* contractual instrument.  Documents with different fingerprints
    (e.g. an amendment vs. the base contract) are never treated as duplicates
    even when their text is very similar.
    """
    sorted_parties = sorted(p.strip().lower() for p in parties)
    payload = f"{doc_type.strip().lower()}|{date.strip()}|{'|'.join(sorted_parties)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Main deduplication entry point
# ---------------------------------------------------------------------------

SIMHASH_THRESHOLD = 1  # Maximum Hamming distance to consider near-duplicate
MIN_TEXT_LENGTH = 50  # Minimum chars of extracted text to run Layer 2/3


def deduplicate_files(files: list[dict]) -> list[dict]:
    """Run the 4-layer dedup pipeline over *files* and return unique entries.

    Parameters
    ----------
    files:
        Each dict must contain at least ``{"file_path": str, "status": "VALID"}``.
        Optionally ``doc_type``, ``date``, and ``parties`` for Layer 4 protection.

    Returns
    -------
    list[dict]
        The unique (non-duplicate) file entries, enriched with fingerprint
        data.  Duplicate entries receive ``is_duplicate``, ``duplicate_of``,
        and ``dedup_layer`` fields but are **excluded** from the returned list.
    """
    if not files:
        return []

    # Accumulators keyed by hash/fingerprint -> first file_path seen
    seen_file_hashes: dict[str, str] = {}
    seen_content_hashes: dict[str, str] = {}
    # For SimHash we need to compare pairwise against all kept entries
    kept_simhashes: list[tuple[str, int, str]] = []  # (file_path, simhash, structural_fp)

    unique: list[dict] = []

    for entry in files:
        fp = entry["file_path"]
        log = logger.bind(file_path=fp)

        # --- Compute fingerprints ----------------------------------------
        file_hash = compute_file_hash(fp)
        entry["file_hash"] = file_hash

        text = extract_text(fp)
        has_text = len(text.strip()) >= MIN_TEXT_LENGTH
        content_hash = compute_content_hash(text)
        entry["content_hash"] = content_hash

        sim_hash = compute_simhash(text)
        entry["simhash"] = sim_hash

        doc_type = entry.get("doc_type", "")
        date = entry.get("date", "")
        parties = entry.get("parties", [])
        structural_fp = compute_structural_fingerprint(doc_type, date, parties)
        entry["structural_fingerprint"] = structural_fp

        # --- Layer 1: exact file hash -----------------------------------
        if file_hash in seen_file_hashes:
            candidate = seen_file_hashes[file_hash]
            # Layer 4 guard: different structural fingerprint -> keep
            if not _structural_match(structural_fp, candidate, kept_simhashes):
                log.info("dedup.layer4_protected", layer=1, candidate=candidate)
            else:
                entry["is_duplicate"] = True
                entry["duplicate_of"] = candidate
                entry["dedup_layer"] = 1
                log.info("dedup.duplicate_found", layer=1, duplicate_of=candidate)
                continue

        # --- Layer 2: content hash --------------------------------------
        # Skip when no text was extracted (e.g. scanned PDFs, images) to
        # avoid treating every no-text file as a duplicate of the first.
        if has_text and content_hash in seen_content_hashes:
            candidate = seen_content_hashes[content_hash]
            if not _structural_match(structural_fp, candidate, kept_simhashes):
                log.info("dedup.layer4_protected", layer=2, candidate=candidate)
            else:
                entry["is_duplicate"] = True
                entry["duplicate_of"] = candidate
                entry["dedup_layer"] = 2
                log.info("dedup.duplicate_found", layer=2, duplicate_of=candidate)
                continue

        # --- Layer 3: SimHash (near-duplicate) --------------------------
        # Skip when no text was extracted — SimHash of empty text is
        # identical for all no-text files, causing false near-duplicates.
        simhash_match = (
            _find_simhash_match(sim_hash, structural_fp, kept_simhashes)
            if has_text
            else None
        )
        if simhash_match is not None:
            entry["is_duplicate"] = True
            entry["duplicate_of"] = simhash_match
            entry["dedup_layer"] = 3
            log.info("dedup.duplicate_found", layer=3, duplicate_of=simhash_match)
            continue

        # --- File is unique: record it -----------------------------------
        seen_file_hashes[file_hash] = fp
        if has_text:
            seen_content_hashes[content_hash] = fp
        kept_simhashes.append((fp, sim_hash, structural_fp))
        entry["is_duplicate"] = False
        unique.append(entry)
        log.info("dedup.unique", file_path=fp)

    logger.info(
        "dedup.complete",
        total=len(files),
        unique=len(unique),
        duplicates=len(files) - len(unique),
    )
    return unique


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _structural_match(
    fp_candidate: str,
    original_path: str,
    kept: list[tuple[str, int, str]],
) -> bool:
    """Return True if *fp_candidate* matches the structural fingerprint of
    the entry whose file_path is *original_path* inside *kept*.

    When either side has an empty structural fingerprint (no metadata
    supplied), we treat them as matching -- i.e. we do NOT protect, because
    there is insufficient metadata to distinguish them.
    """
    for path, _, sfp in kept:
        if path == original_path:
            # If either fingerprint is the hash of empty metadata, treat as match
            empty_fp = compute_structural_fingerprint("", "", [])
            if sfp == empty_fp or fp_candidate == empty_fp:
                return True
            return sfp == fp_candidate
    # Original not yet in kept (shouldn't happen for L1/L2); treat as match
    return True


def _find_simhash_match(
    sim_hash: int,
    structural_fp: str,
    kept: list[tuple[str, int, str]],
) -> str | None:
    """Return the file_path of the first kept entry whose SimHash is within
    *SIMHASH_THRESHOLD* Hamming distance **and** whose structural fingerprint
    matches.  Returns ``None`` when no match is found.
    """
    empty_fp = compute_structural_fingerprint("", "", [])
    for path, kept_hash, kept_sfp in kept:
        if hamming_distance(sim_hash, kept_hash) <= SIMHASH_THRESHOLD:
            # Layer 4 guard
            if kept_sfp == empty_fp or structural_fp == empty_fp:
                return path
            if kept_sfp == structural_fp:
                return path
            # Different structural fingerprint -> skip this candidate
    return None
