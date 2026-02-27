"""Stage 0b: File Deduplication via 4-Layer Hash Pipeline.

Layer 1 - File Hash (SHA-256): Hash raw bytes to catch exact copies.
Layer 2 - Content Hash: Extract text, normalize, hash to catch format variants.
Layer 3 - MinHash + LSH: Jaccard similarity via MinHashLSH index for near-duplicates.
Layer 4 - Identity Tokens + Structural Fingerprint: Protects amendments/SOWs and
          template-based documents with different PO numbers/amounts/dates.
"""

from __future__ import annotations

import hashlib
import re
import string

import structlog
from datasketch import MinHash, MinHashLSH

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
# Layer 3: MinHash + LSH (locality-sensitive hashing for Jaccard similarity)
# ---------------------------------------------------------------------------

MINHASH_THRESHOLD = 0.85  # Minimum Jaccard similarity to consider near-duplicate
MINHASH_NUM_PERM = 128  # Number of permutations for MinHash


def compute_minhash(text: str, num_perm: int = MINHASH_NUM_PERM) -> MinHash:
    """Return a MinHash fingerprint of *text*.

    The fingerprint is built from whitespace-delimited tokens of the
    normalized text.  MinHash is set-based, so word order does not affect
    the result — only the set of unique tokens matters.
    """
    mh = MinHash(num_perm=num_perm)
    normalized = _normalize_text(text)
    tokens = normalized.split()
    for token in tokens:
        mh.update(token.encode("utf-8"))
    return mh


# ---------------------------------------------------------------------------
# Identity tokens (lightweight pre-classification)
# ---------------------------------------------------------------------------

_RE_LONG_NUMBERS = re.compile(r"\b\d{5,}\b")
_RE_DOLLAR_AMOUNTS = re.compile(r"\$[\d,]+\.?\d*")
_RE_DATES = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")


def extract_identity_tokens(text: str) -> str:
    """Extract identifying numbers, dollar amounts, and dates from *text*.

    Returns a canonical pipe-separated string of sorted unique tokens.
    Documents that share a template (same vendor, same doc type) but differ
    in PO number, amount, or date will produce different identity tokens,
    allowing Layer 4 to protect them from false MinHash matches.
    """
    numbers = _RE_LONG_NUMBERS.findall(text)
    amounts = _RE_DOLLAR_AMOUNTS.findall(text)
    dates = _RE_DATES.findall(text)
    tokens = sorted(set(numbers + amounts + dates))
    return "|".join(tokens)


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

MIN_TEXT_LENGTH = 50  # Minimum chars of extracted text to run Layer 2/3


def deduplicate_files(
    files: list[dict],
    *,
    minhash_threshold: float = MINHASH_THRESHOLD,
    num_perm: int = MINHASH_NUM_PERM,
) -> list[dict]:
    """Run the 4-layer dedup pipeline over *files* and return unique entries.

    Parameters
    ----------
    files:
        Each dict must contain at least ``{"file_path": str, "status": "VALID"}``.
        Optionally ``doc_type``, ``date``, and ``parties`` for Layer 4 protection.
    minhash_threshold:
        Minimum Jaccard similarity for Layer 3 near-duplicate detection.
    num_perm:
        Number of permutations for MinHash signatures.

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

    # MinHash LSH index for O(1) near-duplicate lookups
    lsh = MinHashLSH(threshold=minhash_threshold, num_perm=num_perm)
    # file_path -> (structural_fingerprint, identity_tokens)
    kept_metadata: dict[str, tuple[str, str]] = {}

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

        minhash = compute_minhash(text, num_perm=num_perm)
        entry["minhash_signature"] = minhash.hashvalues.tobytes().hex()

        id_tokens = extract_identity_tokens(text) if has_text else ""
        entry["identity_tokens"] = id_tokens

        doc_type = entry.get("doc_type", "")
        date = entry.get("date", "")
        parties = entry.get("parties", [])
        structural_fp = compute_structural_fingerprint(doc_type, date, parties)
        entry["structural_fingerprint"] = structural_fp

        # --- Layer 1: exact file hash -----------------------------------
        if file_hash in seen_file_hashes:
            candidate = seen_file_hashes[file_hash]
            if not _identity_match(structural_fp, id_tokens, candidate, kept_metadata):
                log.info("dedup.layer4_protected", layer=1, candidate=candidate)
            else:
                entry["is_duplicate"] = True
                entry["duplicate_of"] = candidate
                entry["dedup_layer"] = 1
                log.info("dedup.duplicate_found", layer=1, duplicate_of=candidate)
                continue

        # --- Layer 2: content hash --------------------------------------
        if has_text and content_hash in seen_content_hashes:
            candidate = seen_content_hashes[content_hash]
            if not _identity_match(structural_fp, id_tokens, candidate, kept_metadata):
                log.info("dedup.layer4_protected", layer=2, candidate=candidate)
            else:
                entry["is_duplicate"] = True
                entry["duplicate_of"] = candidate
                entry["dedup_layer"] = 2
                log.info("dedup.duplicate_found", layer=2, duplicate_of=candidate)
                continue

        # --- Layer 3: MinHash + LSH (near-duplicate) --------------------
        minhash_match = (
            _find_minhash_match(minhash, structural_fp, id_tokens, lsh, kept_metadata)
            if has_text
            else None
        )
        if minhash_match is not None:
            entry["is_duplicate"] = True
            entry["duplicate_of"] = minhash_match
            entry["dedup_layer"] = 3
            log.info("dedup.duplicate_found", layer=3, duplicate_of=minhash_match)
            continue

        # --- File is unique: record it -----------------------------------
        seen_file_hashes[file_hash] = fp
        if has_text:
            seen_content_hashes[content_hash] = fp
            lsh.insert(fp, minhash)
        kept_metadata[fp] = (structural_fp, id_tokens)
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


def _identity_match(
    structural_fp: str,
    id_tokens: str,
    original_path: str,
    kept_metadata: dict[str, tuple[str, str]],
) -> bool:
    """Return True if the candidate document matches the identity of the
    entry at *original_path* in *kept_metadata*.

    Checks two layers of identity:
    1. Structural fingerprint (doc_type + date + parties) — from metadata.
    2. Identity tokens (PO numbers, amounts, dates) — from text extraction.

    If structural metadata is available and differs, the documents are
    different (return False).  Otherwise, if identity tokens are available
    and differ, the documents are different (return False).  When neither
    source provides distinguishing information, treat as matching (return
    True) to allow dedup.
    """
    if original_path not in kept_metadata:
        return True

    kept_sfp, kept_tokens = kept_metadata[original_path]
    empty_fp = compute_structural_fingerprint("", "", [])

    # Check structural fingerprint first (strongest signal)
    if kept_sfp != empty_fp and structural_fp != empty_fp:
        return kept_sfp == structural_fp

    # Fall back to identity tokens
    if kept_tokens and id_tokens:
        return kept_tokens == id_tokens

    # No distinguishing metadata at all — treat as match
    return True


def _find_minhash_match(
    minhash: MinHash,
    structural_fp: str,
    id_tokens: str,
    lsh: MinHashLSH,
    kept_metadata: dict[str, tuple[str, str]],
) -> str | None:
    """Query the LSH index for candidate near-duplicates, then post-filter
    through identity checks.

    Returns the file_path of the first matching candidate, or None.
    """
    candidates = lsh.query(minhash)
    return _find_identity_match_in_candidates(
        candidates, structural_fp, id_tokens, kept_metadata
    )


def _find_identity_match_in_candidates(
    candidates: list[str],
    structural_fp: str,
    id_tokens: str,
    kept_metadata: dict[str, tuple[str, str]],
) -> str | None:
    """Post-filter LSH candidates through identity checks.

    Returns the first candidate whose identity matches, or None.
    """
    empty_fp = compute_structural_fingerprint("", "", [])

    for candidate_path in candidates:
        if candidate_path not in kept_metadata:
            continue

        kept_sfp, kept_tokens = kept_metadata[candidate_path]

        # Check structural fingerprint (strongest signal)
        if kept_sfp != empty_fp and structural_fp != empty_fp:
            if kept_sfp == structural_fp:
                return candidate_path
            continue  # Different structural identity

        # Fall back to identity tokens
        if kept_tokens and id_tokens:
            if kept_tokens == id_tokens:
                return candidate_path
            continue  # Different identity tokens

        # No distinguishing metadata — treat as match
        return candidate_path

    return None
