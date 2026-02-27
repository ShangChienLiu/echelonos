"""Stage 0b: File Deduplication via 4-Layer Hash Pipeline.

Layer 1 - File Hash (SHA-256): Hash raw bytes to catch exact copies.
Layer 2 - Content Hash: Extract text, normalize, hash to catch format variants.
Layer 3 - MinHash + LSH: Jaccard similarity via MinHashLSH index for near-duplicates.
Layer 4 - Blocking Keys + Structural Fingerprint: Protects amendments/SOWs and
          template-based documents with different PO numbers/amounts/dates.
          Uses Claude-extracted structured fields when available, with regex fallback.
"""

from __future__ import annotations

import hashlib
import re
import string

import structlog
from datasketch import MinHash, MinHashLSH
from pydantic import BaseModel

from echelonos.llm.claude_client import extract_with_structured_output

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Blocking Key Fields (Pydantic model for structured extraction)
# ---------------------------------------------------------------------------


class BlockingKeyFields(BaseModel):
    document_title: str | None = None
    vendor_name: str | None = None
    client_name: str | None = None
    invoice_number: str | None = None
    po_number: str | None = None
    total_amount: str | None = None  # Raw string "$3,800.00"
    document_date: str | None = None  # ISO-8601 preferred
    contract_reference: str | None = None


BLOCKING_KEY_SYSTEM_PROMPT = """You are a document field extractor for legal contracts, purchase orders, and invoices.

Given the first portion of a document's text, extract the following fields if present.
Return null for any field not found. Be precise — extract exact values as they appear.

Fields to extract:
- document_title: The title or type of document (e.g., "Purchase Order", "Master Services Agreement")
- vendor_name: The vendor, supplier, or service provider name
- client_name: The client, buyer, or customer name
- invoice_number: Any invoice number or ID
- po_number: Any purchase order number
- total_amount: The total dollar amount (keep original formatting, e.g., "$3,800.00")
- document_date: The primary date (effective date, issue date, etc.) — prefer ISO-8601 format
- contract_reference: Any contract or agreement reference number"""

MAX_TEXT_FOR_BLOCKING = 4000  # Only send first 4K chars (identifying info is in headers)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_VENDOR_SUFFIXES = re.compile(
    r"\b(llc\.?|inc\.?|corp\.?|corporation|company|co\.?|ltd\.?|lp\.?|plc\.?)\s*$",
    re.IGNORECASE,
)


def _normalize_vendor(name: str | None) -> str:
    """Strip LLC/Inc/Corp suffixes, lowercase, collapse whitespace."""
    if not name:
        return ""
    result = name.strip().lower()
    result = _VENDOR_SUFFIXES.sub("", result).strip()
    result = re.sub(r"\s+", " ", result).strip()
    return result


def _normalize_amount(amount: str | None) -> str:
    """'$3,800.00' → '3800' (round to int)."""
    if not amount:
        return ""
    cleaned = amount.replace("$", "").replace(",", "").strip()
    try:
        return str(round(float(cleaned)))
    except ValueError:
        return cleaned


def _normalize_date(date_str: str | None) -> str:
    """Normalize to YYYY-MM-DD. Handles ISO-8601 and US MM/DD/YYYY."""
    if not date_str:
        return ""
    date_str = date_str.strip()
    # Already ISO-8601
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str
    # US format MM/DD/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", date_str)
    if m:
        month, day, year = m.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return date_str


def _normalize_id(value: str | None) -> str:
    """Strip whitespace, lowercase."""
    if not value:
        return ""
    return value.strip().lower()


def _blocking_keys_match(a: BlockingKeyFields, b: BlockingKeyFields) -> bool:
    """Field-level comparison with priority rules.

    1. PO/invoice numbers differ → protect (False)
    2. PO/invoice numbers match → collapse (True)
    3. Same vendor, different amount → protect
    4. Same vendor, different date → protect
    5. No distinguishing fields → collapse (True)
    """
    # Normalize fields for comparison
    a_po = _normalize_id(a.po_number)
    b_po = _normalize_id(b.po_number)
    a_inv = _normalize_id(a.invoice_number)
    b_inv = _normalize_id(b.invoice_number)

    # Priority 1: PO numbers
    if a_po and b_po:
        if a_po != b_po:
            return False  # Different PO → protect
        return True  # Same PO → collapse

    # Priority 1b: Invoice numbers
    if a_inv and b_inv:
        if a_inv != b_inv:
            return False  # Different invoice → protect
        return True  # Same invoice → collapse

    # Priority 2: Vendor + amount
    a_vendor = _normalize_vendor(a.vendor_name)
    b_vendor = _normalize_vendor(b.vendor_name)
    a_amount = _normalize_amount(a.total_amount)
    b_amount = _normalize_amount(b.total_amount)

    if a_vendor and b_vendor and a_vendor == b_vendor:
        if a_amount and b_amount and a_amount != b_amount:
            return False  # Same vendor, different amount → protect

    # Priority 3: Vendor + date
    a_date = _normalize_date(a.document_date)
    b_date = _normalize_date(b.document_date)

    if a_vendor and b_vendor and a_vendor == b_vendor:
        if a_date and b_date and a_date != b_date:
            return False  # Same vendor, different date → protect

    # No distinguishing fields → collapse
    return True

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
# Claude-based blocking key extraction
# ---------------------------------------------------------------------------


def extract_blocking_keys(
    text: str,
    claude_client=None,
) -> BlockingKeyFields | None:
    """Extract blocking keys from document text using Claude.

    Returns None if Claude is unavailable or extraction fails.
    """
    if claude_client is None:
        return None

    try:
        truncated = text[:MAX_TEXT_FOR_BLOCKING]
        result = extract_with_structured_output(
            claude_client,
            BLOCKING_KEY_SYSTEM_PROMPT,
            truncated,
            BlockingKeyFields,
        )
        return result
    except Exception:
        logger.warning("blocking_keys.claude_extraction_failed", exc_info=True)
        return None


def _regex_fallback_blocking_keys(text: str) -> BlockingKeyFields | None:
    """Extract blocking keys using regex patterns when Claude is unavailable.

    Uses existing regex patterns to populate BlockingKeyFields as best-effort.
    """
    if not text or len(text.strip()) < MIN_TEXT_LENGTH:
        return None

    numbers = _RE_LONG_NUMBERS.findall(text)
    amounts = _RE_DOLLAR_AMOUNTS.findall(text)
    dates = _RE_DATES.findall(text)

    if not numbers and not amounts and not dates:
        return None

    return BlockingKeyFields(
        po_number=numbers[0] if numbers else None,
        total_amount=amounts[0] if amounts else None,
        document_date=dates[0] if dates else None,
    )


def _get_or_extract_blocking_keys(
    fp: str,
    text_cache: dict[str, str],
    keys_cache: dict[str, BlockingKeyFields | None],
    client,
) -> BlockingKeyFields | None:
    """Lazy extraction with caching: Claude → regex fallback → None."""
    if fp in keys_cache:
        return keys_cache[fp]

    text = text_cache.get(fp, "")

    # Try Claude first
    keys = extract_blocking_keys(text, claude_client=client)

    # Fall back to regex
    if keys is None:
        keys = _regex_fallback_blocking_keys(text)

    keys_cache[fp] = keys
    return keys


# ---------------------------------------------------------------------------
# Main deduplication entry point
# ---------------------------------------------------------------------------

MIN_TEXT_LENGTH = 50  # Minimum chars of extracted text to run Layer 2/3


def deduplicate_files(
    files: list[dict],
    *,
    minhash_threshold: float = MINHASH_THRESHOLD,
    num_perm: int = MINHASH_NUM_PERM,
    claude_client=None,
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
    claude_client:
        Optional Anthropic client for Claude-based blocking key extraction.
        If None, regex fallback is used.

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

    # Blocking key caches (lazy — only populated when candidates match)
    kept_blocking_keys: dict[str, BlockingKeyFields | None] = {}
    kept_structural_fps: dict[str, str] = {}
    text_cache: dict[str, str] = {}

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

        # Cache text for lazy blocking key extraction
        if has_text:
            text_cache[fp] = text

        # --- Layer 1: exact file hash -----------------------------------
        if file_hash in seen_file_hashes:
            candidate = seen_file_hashes[file_hash]
            if not _identity_match_blocking(
                fp, candidate, structural_fp,
                text_cache, kept_blocking_keys, kept_structural_fps, claude_client,
            ):
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
            if not _identity_match_blocking(
                fp, candidate, structural_fp,
                text_cache, kept_blocking_keys, kept_structural_fps, claude_client,
            ):
                log.info("dedup.layer4_protected", layer=2, candidate=candidate)
            else:
                entry["is_duplicate"] = True
                entry["duplicate_of"] = candidate
                entry["dedup_layer"] = 2
                log.info("dedup.duplicate_found", layer=2, duplicate_of=candidate)
                continue

        # --- Layer 3: MinHash + LSH (near-duplicate) --------------------
        minhash_match = (
            _find_minhash_match_blocking(
                minhash, fp, structural_fp, lsh,
                text_cache, kept_blocking_keys, kept_structural_fps, claude_client,
            )
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
        kept_structural_fps[fp] = structural_fp

        # Store blocking keys on entry if they've been extracted
        bk = kept_blocking_keys.get(fp)
        entry["blocking_keys"] = bk.model_dump() if bk else None

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


def _identity_match_blocking(
    current_fp: str,
    candidate_fp: str,
    structural_fp: str,
    text_cache: dict[str, str],
    keys_cache: dict[str, BlockingKeyFields | None],
    structural_fps: dict[str, str],
    client,
) -> bool:
    """Return True if the current document matches the identity of *candidate_fp*.

    Uses structural fingerprint first, then blocking keys (Claude or regex).
    """
    kept_sfp = structural_fps.get(candidate_fp, "")
    empty_fp = compute_structural_fingerprint("", "", [])

    # Check structural fingerprint first (strongest signal — from metadata)
    if kept_sfp != empty_fp and structural_fp != empty_fp:
        return kept_sfp == structural_fp

    # Fall back to blocking keys (lazy extraction)
    current_keys = _get_or_extract_blocking_keys(
        current_fp, text_cache, keys_cache, client,
    )
    candidate_keys = _get_or_extract_blocking_keys(
        candidate_fp, text_cache, keys_cache, client,
    )

    if current_keys is not None and candidate_keys is not None:
        return _blocking_keys_match(current_keys, candidate_keys)

    # No distinguishing information — treat as match
    return True


def _find_minhash_match_blocking(
    minhash: MinHash,
    current_fp: str,
    structural_fp: str,
    lsh: MinHashLSH,
    text_cache: dict[str, str],
    keys_cache: dict[str, BlockingKeyFields | None],
    structural_fps: dict[str, str],
    client,
) -> str | None:
    """Query the LSH index for candidate near-duplicates, then post-filter
    through blocking key checks.

    Returns the file_path of the first matching candidate, or None.
    """
    candidates = lsh.query(minhash)
    empty_fp = compute_structural_fingerprint("", "", [])

    for candidate_path in candidates:
        if candidate_path not in structural_fps:
            continue

        kept_sfp = structural_fps[candidate_path]

        # Check structural fingerprint (strongest signal)
        if kept_sfp != empty_fp and structural_fp != empty_fp:
            if kept_sfp == structural_fp:
                return candidate_path
            continue  # Different structural identity

        # Fall back to blocking keys
        current_keys = _get_or_extract_blocking_keys(
            current_fp, text_cache, keys_cache, client,
        )
        candidate_keys = _get_or_extract_blocking_keys(
            candidate_path, text_cache, keys_cache, client,
        )

        if current_keys is not None and candidate_keys is not None:
            if _blocking_keys_match(current_keys, candidate_keys):
                return candidate_path
            continue  # Different blocking keys

        # No distinguishing information — treat as match
        return candidate_path

    return None
