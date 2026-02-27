# Stage 0b: 4-Layer Deduplication

> **Linear ticket:** AKS-12
>
> **Source file:** `src/echelonos/stages/stage_0b_dedup.py` (636 lines)
> **Test file:** `tests/e2e/test_stage_0b_dedup.py` (369 lines)

---

## Table of Contents

1. [Purpose and Role in the Pipeline](#1-purpose-and-role-in-the-pipeline)
2. [The 4-Layer Strategy: Overview](#2-the-4-layer-strategy-overview)
3. [Module-Level Constants and Imports](#3-module-level-constants-and-imports)
4. [Function-by-Function Walkthrough](#4-function-by-function-walkthrough)
   - 4.1 [Layer 1: `compute_file_hash()`](#41-layer-1-compute_file_hash--sha-256-of-raw-bytes)
   - 4.2 [Text Extraction: `extract_text()`](#42-text-extraction-extract_text)
   - 4.3 [Text Normalization: `_normalize_text()`](#43-text-normalization-_normalize_text)
   - 4.4 [Layer 2: `compute_content_hash()`](#44-layer-2-compute_content_hash--normalized-text-hash)
   - 4.5 [Layer 3: `compute_minhash()`](#45-layer-3-compute_minhash--minhash--lsh-for-jaccard-similarity)
   - 4.6 [Layer 4: `compute_structural_fingerprint()`](#46-layer-4-compute_structural_fingerprint--contract-identity)
   - 4.7 [Blocking Keys: `extract_blocking_keys()` and `_blocking_keys_match()`](#47-blocking-keys-extract_blocking_keys-and-_blocking_keys_match)
   - 4.8 [`deduplicate_files()`](#48-deduplicate_files--the-main-pipeline)
   - 4.9 [`_identity_match_blocking()`](#49-_identity_match_blocking--layer-4-guard)
   - 4.10 [`_find_minhash_match_blocking()`](#410-_find_minhash_match_blocking--minhash-lsh-with-blocking-key-guard)
5. [The Dedup Pipeline Flow](#5-the-dedup-pipeline-flow)
6. [Duplicate Detection vs Near-Duplicate Detection](#6-duplicate-detection-vs-near-duplicate-detection)
7. [Test Coverage Walkthrough](#7-test-coverage-walkthrough)
8. [Key Takeaways](#8-key-takeaways)
9. [Watch Out For](#9-watch-out-for)

---

## 1. Purpose and Role in the Pipeline

Stage 0b runs immediately after Stage 0a (file validation). Its job is to **eliminate duplicate and near-duplicate files** before they enter the expensive extraction pipeline. Without this stage, the same contract uploaded in PDF and DOCX format, or a contract with minor typo corrections, would be processed multiple times -- wasting compute and producing confusing duplicate results.

The module docstring at lines 1-7 of `src/echelonos/stages/stage_0b_dedup.py` summarizes the four layers:

```python
"""Stage 0b: File Deduplication via 4-Layer Hash Pipeline.

Layer 1 - File Hash (SHA-256): Hash raw bytes to catch exact copies.
Layer 2 - Content Hash: Extract text, normalize, hash to catch format variants.
Layer 3 - MinHash + LSH: Jaccard similarity via MinHashLSH index for near-duplicates.
Layer 4 - Blocking Keys + Structural Fingerprint: Protects amendments/SOWs and
          template-based documents with different PO numbers/amounts/dates.
          Uses Claude-extracted structured fields when available, with regex fallback.
"""
```

The critical insight of this design is that **Layer 4 is not a dedup layer but a protection layer**. It prevents similar-but-legally-distinct documents (like a base contract and its amendment, or template-based invoices with different PO numbers) from being incorrectly flagged as duplicates. Layers 1-3 detect duplicates with increasing fuzziness; Layer 4 acts as a veto that can override any of them. Layer 4 now uses Claude-extracted blocking keys (vendor name, PO number, invoice number, amount, date) with a regex fallback, in addition to the structural fingerprint from metadata.

---

## 2. The 4-Layer Strategy: Overview

| Layer | Name | Input | Algorithm | Catches | Can Be Vetoed by L4? |
|---|---|---|---|---|---|
| **1** | File Hash | Raw file bytes | SHA-256 | Exact byte-for-byte copies (different filename, same content) | Yes |
| **2** | Content Hash | Extracted + normalized text | SHA-256 | Same text in different formats (PDF vs. DOCX), or with different formatting | Yes |
| **3** | MinHash + LSH | Extracted + normalized text tokens | MinHash (128 permutations) + MinHashLSH index, Jaccard similarity >= 0.85 | Near-duplicates with minor edits (typo fixes, small wording changes) | Yes |
| **4** | Blocking Keys + Structural Fingerprint | Claude-extracted fields (vendor, PO, invoice, amount, date) + `(doc_type, date, parties)` metadata | Field-level comparison with priority rules; SHA-256 of concatenated metadata | N/A -- this layer **protects** documents, it does not flag duplicates | N/A |

**Why four layers?** Each layer addresses a different real-world scenario:

- **Layer 1** is the cheapest and most certain. If two files are byte-identical, they are definitely the same. This catches the common case where someone downloads the same contract twice, or copies it to another folder.

- **Layer 2** handles format variants. A contract originally in DOCX that was also saved as PDF will have the same text content but completely different bytes. By normalizing the text (lowercasing, stripping punctuation, collapsing whitespace) before hashing, Layer 2 catches these.

- **Layer 3** handles near-duplicates. When someone fixes a typo in a contract, or a system adds/removes a header, the content hash changes entirely (SHA-256 is sensitive to any change). MinHash + LSH estimates Jaccard similarity between token sets, catching documents that share most of their vocabulary. The LSH index provides O(1) candidate lookups instead of pairwise comparison.

- **Layer 4** is the safety net. An amendment to a contract may share 95% of its text with the base contract. Without Layer 4, MinHash would flag the amendment as a duplicate. The protection layer has two mechanisms: (1) a structural fingerprint built from document type, date, and parties metadata, and (2) Claude-extracted blocking keys (vendor name, PO number, invoice number, amount, date) with a regex fallback. Blocking keys are especially effective at protecting template-based documents like invoices that share boilerplate text but have different PO numbers or amounts.

---

## 3. Module-Level Constants and Imports

**File:** `src/echelonos/stages/stage_0b_dedup.py`, lines 10-23.

```python
import hashlib
import re
import string

import structlog
from datasketch import MinHash, MinHashLSH
from pydantic import BaseModel

from echelonos.llm.claude_client import extract_with_structured_output

logger = structlog.get_logger()
```

### External dependencies

| Library | Purpose |
|---|---|
| `datasketch` (via `from datasketch import MinHash, MinHashLSH`) | MinHash and Locality-Sensitive Hashing for Jaccard similarity estimation |
| `pydantic` | Data validation for `BlockingKeyFields` model |
| `structlog` | Structured logging |
| `pypdf` | PDF text extraction (lazy import inside `_extract_pdf_text()`) |
| `python-docx` | DOCX text extraction (lazy import inside `_extract_docx_text()`) |
| `echelonos.llm.claude_client` | Claude-based structured field extraction for blocking keys |

### Key constants

```python
MINHASH_THRESHOLD = 0.85   # Minimum Jaccard similarity to consider near-duplicate
MINHASH_NUM_PERM = 128     # Number of permutations for MinHash
MAX_TEXT_FOR_BLOCKING = 4000  # Only send first 4K chars to Claude
MIN_TEXT_LENGTH = 50       # Minimum chars of extracted text to run Layer 2/3
```

`MINHASH_THRESHOLD` is the minimum Jaccard similarity (intersection over union of token sets) for two documents to be considered near-duplicates. At 0.85, documents must share at least 85% of their unique tokens. `MINHASH_NUM_PERM` controls the accuracy of the MinHash approximation -- more permutations give better estimates at the cost of memory.

### Text normalization table

```python
_PUNCTUATION_TABLE = str.maketrans("", "", string.punctuation)  # line 91
```

Pre-computed translation table used by `_normalize_text()` to strip all punctuation characters in O(n) time.

---

## 4. Function-by-Function Walkthrough

### 4.1 Layer 1: `compute_file_hash()` -- SHA-256 of Raw Bytes

**Lines 25-34.**

```python
def compute_file_hash(file_path: str) -> str:
    """Compute SHA-256 hash of the raw file bytes.

    Catches exact copies regardless of filename.
    """
    sha = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()
```

**Key details:**
- Reads the file in **8 KB chunks** (line 32) to handle large files without loading the entire thing into memory. The `iter(lambda: fh.read(8192), b"")` pattern calls `fh.read(8192)` repeatedly until it returns the empty bytes sentinel `b""` (EOF).
- Returns the hex digest as a lowercase string (64 hex characters for SHA-256).
- This is the only layer that works on raw bytes. All other layers work on extracted text.

**Test coverage:** `TestComputeFileHash` (test file lines 342-355) tests determinism and content sensitivity.

---

### 4.2 Text Extraction: `extract_text()`

**Lines 42-84.** A dispatcher that extracts plain text from PDF or DOCX files based on file extension.

```python
def extract_text(file_path: str) -> str:
    lower = file_path.lower()
    if lower.endswith(".pdf"):
        return _extract_pdf_text(file_path)
    elif lower.endswith(".docx"):
        return _extract_docx_text(file_path)
    else:
        logger.warning("extract_text.unsupported_format", file_path=file_path)
        return ""
```

**Design decision:** Unlike Stage 0a which uses MIME-based detection, this module uses **file extension** for format detection. This is acceptable here because files reaching Stage 0b have already been validated by Stage 0a -- their MIME types are known and their extensions are reliable.

**`_extract_pdf_text()`** (lines 58-72): Uses `pypdf.PdfReader` to iterate all pages and join their text with newlines. Lazy-imports pypdf.

**`_extract_docx_text()`** (lines 75-84): Uses `python-docx.Document` to extract paragraph text. Lazy-imports python-docx.

Both functions return an empty string on failure and log the exception.

**Test coverage:** `TestExtractText` (test file lines 321-339) tests PDF extraction, DOCX extraction, and unsupported format fallback.

---

### 4.3 Text Normalization: `_normalize_text()`

**Lines 94-99.** Normalizes text before hashing to make comparison resilient to superficial differences.

```python
_PUNCTUATION_TABLE = str.maketrans("", "", string.punctuation)

def _normalize_text(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation."""
    text = text.lower()
    text = text.translate(_PUNCTUATION_TABLE)
    text = re.sub(r"\s+", " ", text).strip()
    return text
```

**Three normalization steps:**
1. **Lowercase** (line 96): `"Hello World"` becomes `"hello world"`.
2. **Strip punctuation** (line 97): `"hello, world!"` becomes `"hello world"`. Uses the pre-computed `_PUNCTUATION_TABLE` for O(n) performance.
3. **Collapse whitespace** (line 98): `"hello   \n  world"` becomes `"hello world"`. The regex `\s+` matches one or more whitespace characters (spaces, tabs, newlines) and replaces them with a single space.

**Why these three?** They address the most common superficial differences between document versions:
- Different formatting may change capitalization.
- Different renderers may insert or remove punctuation (e.g., smart quotes vs. straight quotes).
- Different tools produce different whitespace (especially when converting between formats).

**This normalization is shared by Layers 2 and 3.** Both `compute_content_hash()` and `compute_minhash()` call `_normalize_text()` on the input text before processing.

**Test coverage:** `TestComputeContentHash` (test file lines 358-369) validates that the content hash is case-insensitive, whitespace-insensitive, and punctuation-insensitive:

```python
def test_case_insensitive(self):
    assert compute_content_hash("Hello World") == compute_content_hash("hello world")

def test_whitespace_insensitive(self):
    assert compute_content_hash("hello   world") == compute_content_hash("hello world")

def test_punctuation_insensitive(self):
    assert compute_content_hash("hello, world!") == compute_content_hash("hello world")
```

---

### 4.4 Layer 2: `compute_content_hash()` -- Normalized Text Hash

**Lines 102-108.**

```python
def compute_content_hash(text: str) -> str:
    """Normalize *text* then return its SHA-256 hex digest.

    Catches copies that differ only in formatting or file type.
    """
    normalized = _normalize_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
```

**How it differs from Layer 1:** Layer 1 hashes raw file bytes. Two files with the same text but different formats (PDF vs. DOCX) produce completely different file hashes. Layer 2 extracts the text, normalizes it, and hashes the result -- so format-independent copies produce the same content hash.

**Test coverage:** `TestContentDuplicateDetected` (test file lines 138-155) creates a PDF and a DOCX with identical text and verifies Layer 2 catches the duplicate:

```python
def test_content_duplicate_detected(self, tmp_path: Path):
    text = "Statement of Work for Project Phoenix between Acme Corp and Widget Inc."
    pdf_file = _make_pdf(tmp_path / "sow.pdf", text)
    docx_file = _make_docx(tmp_path / "sow.docx", text)

    files = [_entry(str(pdf_file)), _entry(str(docx_file))]
    unique = deduplicate_files(files)

    assert len(unique) == 1
    dup = files[1]
    assert dup["dedup_layer"] == 2
```

---

### 4.5 Layer 3: `compute_minhash()` -- MinHash + LSH for Jaccard Similarity

**Lines 257-273.**

```python
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
```

**How MinHash + LSH works:**

MinHash (Min-wise Independent Permutations) is a locality-sensitive hashing technique for estimating **Jaccard similarity** between sets. Unlike SimHash (which was used previously), MinHash operates on sets of tokens and estimates the ratio of their intersection to their union.

The algorithm:
1. **Tokenize** the text into words (line 269: `normalized.split()`).
2. For each token, hash it and update the MinHash signature (line 271: `mh.update(token.encode("utf-8"))`). Internally, MinHash maintains `num_perm` (128) hash functions, and for each one keeps the minimum hash value seen across all tokens.
3. The resulting signature is a vector of 128 minimum hash values. Two documents' Jaccard similarity can be estimated by comparing how many of these 128 values match.

The **MinHashLSH** index (created in `deduplicate_files()`) provides O(1) candidate lookups by hashing the MinHash signatures into buckets. Only documents that land in the same bucket are compared, avoiding the O(n^2) pairwise comparison that the old SimHash approach required.

**Return value:** A `datasketch.MinHash` object. The signature is stored on file entries as a hex-encoded byte string via `minhash.hashvalues.tobytes().hex()` for persistence (stored in the database as `minhash_signature` JSONB column).

**Why MinHash replaced SimHash:** MinHash + LSH provides two key advantages:
1. **O(1) candidate lookup** via the LSH index, instead of O(n) linear scan for each file.
2. **Jaccard similarity** is a more interpretable and tunable metric than Hamming distance on SimHash. The threshold of 0.85 directly means "85% token overlap."

**Test coverage:** `TestNearDuplicateDetected` creates two PDFs with a single-word difference ("consulting" vs. "advisory") and verifies:
1. File hashes differ (not caught by Layer 1).
2. Content hashes differ (not caught by Layer 2).
3. MinHash Jaccard similarity is >= 0.85 (caught by Layer 3).

---

### 4.6 Layer 4: `compute_structural_fingerprint()` -- Contract Identity

**Lines 305-319.**

```python
def compute_structural_fingerprint(
    doc_type: str,
    date: str,
    parties: list[str],
) -> str:
    """Hash (doc_type, date, sorted parties) to fingerprint contract identity."""
    sorted_parties = sorted(p.strip().lower() for p in parties)
    payload = f"{doc_type.strip().lower()}|{date.strip()}|{'|'.join(sorted_parties)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

**How it works:**
1. **Sort and normalize parties** (line 317): Parties are lowercased, stripped, and sorted alphabetically. This ensures `["Acme Corp", "Beta LLC"]` and `["Beta LLC", "Acme Corp"]` produce the same fingerprint.
2. **Build payload** (line 318): Concatenate `doc_type`, `date`, and sorted parties with `|` as separator.
3. **Hash** (line 319): SHA-256 of the UTF-8 encoded payload string.

**Example payloads:**
```
"msa|2024-01-15|acme corporation|widget incorporated"     # Base contract
"amendment|2024-06-01|acme corporation|widget incorporated" # Amendment
```

These two produce different fingerprints because `doc_type` differs ("msa" vs. "amendment") and `date` differs.

**Design decision:** The parties are sorted to make the fingerprint **order-independent**. Contract databases often store parties in inconsistent orders. Without sorting, `["A", "B"]` and `["B", "A"]` would produce different fingerprints.

**Test coverage:** `TestComputeStructuralFingerprint`:
- Deterministic (same input -> same output)
- Party order independent
- Different `doc_type` -> different fingerprint
- Different `date` -> different fingerprint

---

### 4.7 Blocking Keys: `extract_blocking_keys()` and `_blocking_keys_match()`

**Lines 31-161.** Layer 4 now includes a Claude-based blocking key system that extracts structured fields from document text and uses field-level comparison to decide whether two near-duplicate documents are truly the same or should be protected.

**`BlockingKeyFields` model** (lines 31-39):

```python
class BlockingKeyFields(BaseModel):
    document_title: str | None = None
    vendor_name: str | None = None
    client_name: str | None = None
    invoice_number: str | None = None
    po_number: str | None = None
    total_amount: str | None = None  # Raw string "$3,800.00"
    document_date: str | None = None  # ISO-8601 preferred
    contract_reference: str | None = None
```

This Pydantic model defines the structured fields that Claude extracts from document headers. Each field is optional -- Claude returns `null` for any field not found.

**`extract_blocking_keys()`** (lines 327-349):

```python
def extract_blocking_keys(text: str, claude_client=None) -> BlockingKeyFields | None:
    """Extract blocking keys from document text using Claude."""
    if claude_client is None:
        return None
    try:
        truncated = text[:MAX_TEXT_FOR_BLOCKING]
        result = extract_with_structured_output(
            claude_client, BLOCKING_KEY_SYSTEM_PROMPT, truncated, BlockingKeyFields,
        )
        return result
    except Exception:
        logger.warning("blocking_keys.claude_extraction_failed", exc_info=True)
        return None
```

Only the first 4,000 characters of text are sent to Claude, since identifying information (vendor name, PO number, dates) is typically found in document headers. If Claude is unavailable or extraction fails, the function returns `None` and the system falls back to regex extraction.

**`_regex_fallback_blocking_keys()`** (lines 352-371): A best-effort fallback that uses the same regex patterns formerly used for identity tokens (`_RE_LONG_NUMBERS`, `_RE_DOLLAR_AMOUNTS`, `_RE_DATES`) to populate a `BlockingKeyFields` object when Claude is unavailable.

**`_blocking_keys_match()`** (lines 114-160) -- the core comparison logic:

```python
def _blocking_keys_match(a: BlockingKeyFields, b: BlockingKeyFields) -> bool:
    """Field-level comparison with priority rules.

    1. PO/invoice numbers differ → protect (False)
    2. PO/invoice numbers match → collapse (True)
    3. Same vendor, different amount → protect
    4. Same vendor, different date → protect
    5. No distinguishing fields → collapse (True)
    """
```

The comparison follows a priority chain:
1. **PO numbers** are the strongest signal. If both documents have PO numbers and they differ, the documents are protected (return `False`). If they match, collapse them (return `True`).
2. **Invoice numbers** follow the same logic as PO numbers.
3. **Vendor + amount**: If both documents have the same vendor but different total amounts, protect.
4. **Vendor + date**: If both documents have the same vendor but different dates, protect.
5. **No distinguishing fields**: If none of the above rules fire, collapse (return `True`).

**Normalization helpers** (lines 64-111) ensure robust comparison:
- `_normalize_vendor()`: Strips LLC/Inc/Corp suffixes, lowercases, collapses whitespace.
- `_normalize_amount()`: Converts `"$3,800.00"` to `"3800"` (rounded integer).
- `_normalize_date()`: Normalizes to `YYYY-MM-DD` format, handling both ISO-8601 and US `MM/DD/YYYY`.
- `_normalize_id()`: Simple strip + lowercase for PO/invoice numbers.

**Lazy extraction with caching** -- `_get_or_extract_blocking_keys()` (lines 374-394): Blocking keys are only extracted when needed (when Layer 1, 2, or 3 finds a candidate match), and results are cached per file path. This avoids calling Claude for files that are clearly unique.

---

### 4.8 `deduplicate_files()` -- The Main Pipeline

**Lines 404-546.** This is the public entry point that orchestrates the full 4-layer pipeline.

```python
def deduplicate_files(
    files: list[dict],
    *,
    minhash_threshold: float = MINHASH_THRESHOLD,
    num_perm: int = MINHASH_NUM_PERM,
    claude_client=None,
) -> list[dict]:
```

**Parameters:**
- `files`: A list of dicts, each containing at minimum `{"file_path": str, "status": "VALID"}`. Optionally `doc_type`, `date`, and `parties` for structural fingerprint protection.
- `minhash_threshold`: Minimum Jaccard similarity for Layer 3 near-duplicate detection (default 0.85).
- `num_perm`: Number of permutations for MinHash signatures (default 128).
- `claude_client`: Optional Anthropic client for Claude-based blocking key extraction. If `None`, regex fallback is used.

**Returns:** A list of unique (non-duplicate) file entries. Duplicate entries are mutated in-place with `is_duplicate`, `duplicate_of`, and `dedup_layer` fields but excluded from the returned list.

**Accumulator data structures** (lines 437-448):

```python
seen_file_hashes: dict[str, str] = {}      # file_hash -> first file_path
seen_content_hashes: dict[str, str] = {}    # content_hash -> first file_path

# MinHash LSH index for O(1) near-duplicate lookups
lsh = MinHashLSH(threshold=minhash_threshold, num_perm=num_perm)

# Blocking key caches (lazy — only populated when candidates match)
kept_blocking_keys: dict[str, BlockingKeyFields | None] = {}
kept_structural_fps: dict[str, str] = {}
text_cache: dict[str, str] = {}
```

- `seen_file_hashes` and `seen_content_hashes` are dict-based for O(1) lookup (Layers 1 and 2).
- `lsh` is a `MinHashLSH` index that provides O(1) candidate lookups for Layer 3, replacing the old O(n) linear scan over a list of SimHash values.
- `kept_blocking_keys` and `text_cache` support lazy blocking key extraction -- Claude is only called when a candidate match is found and needs to be verified.

**Main loop** (lines 450-546):

For each file entry:

1. **Compute all fingerprints** (lines 455-477):
   ```python
   file_hash = compute_file_hash(fp)
   text = extract_text(fp)
   content_hash = compute_content_hash(text)
   minhash = compute_minhash(text, num_perm=num_perm)
   structural_fp = compute_structural_fingerprint(doc_type, date, parties)
   ```
   All fingerprints are computed upfront and stored on the entry dict. Text is cached for later blocking key extraction if needed.

2. **Layer 1 check** (lines 480-493):
   ```python
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
           continue
   ```
   If the file hash matches a previously seen file, check Layer 4 (structural fingerprint + blocking keys) before flagging. If the identities differ, the file is **protected** (kept as unique). Otherwise, it is flagged as a Layer 1 duplicate and `continue` skips to the next file.

3. **Layer 2 check** (lines 495-507): Same pattern as Layer 1, but using content hash.

4. **Layer 3 check** (lines 510-523):
   ```python
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
       continue
   ```
   Uses `_find_minhash_match_blocking()` which queries the LSH index for O(1) candidate lookup, then post-filters through blocking key checks.

5. **Record unique file** (lines 526-538):
   ```python
   seen_file_hashes[file_hash] = fp
   if has_text:
       seen_content_hashes[content_hash] = fp
       lsh.insert(fp, minhash)
   kept_structural_fps[fp] = structural_fp

   bk = kept_blocking_keys.get(fp)
   entry["blocking_keys"] = bk.model_dump() if bk else None
   entry["is_duplicate"] = False
   unique.append(entry)
   ```

**Output fields added to each entry:**

| Field | Type | Present On |
|---|---|---|
| `file_hash` | `str` | All entries |
| `content_hash` | `str` | All entries |
| `minhash_signature` | `str` (hex-encoded) | All entries |
| `identity_tokens` | `str` | All entries |
| `structural_fingerprint` | `str` | All entries |
| `blocking_keys` | `dict` or `None` | Unique entries |
| `is_duplicate` | `bool` | All entries |
| `duplicate_of` | `str` | Duplicates only |
| `dedup_layer` | `int` (1, 2, or 3) | Duplicates only |

**Summary log** (lines 540-545):
```python
logger.info(
    "dedup.complete",
    total=len(files),
    unique=len(unique),
    duplicates=len(files) - len(unique),
)
```

---

### 4.9 `_identity_match_blocking()` -- Layer 4 Guard

**Lines 554-586.**

```python
def _identity_match_blocking(
    current_fp: str,
    candidate_fp: str,
    structural_fp: str,
    text_cache: dict[str, str],
    keys_cache: dict[str, BlockingKeyFields | None],
    structural_fps: dict[str, str],
    client,
) -> bool:
```

This function answers: "Does the current file have the same identity as the candidate file it was matched against?" It uses a two-tier approach: structural fingerprint first, then blocking keys.

**Logic:**
1. Check the **structural fingerprint** first (strongest signal, from metadata). If both the current and candidate file have non-empty structural fingerprints, return `True` if they match, `False` if they differ.
2. If structural fingerprints are unavailable (empty metadata), **fall back to blocking keys**. Extract blocking keys for both files using `_get_or_extract_blocking_keys()` (which tries Claude first, then regex fallback, and caches results).
3. If both files have blocking keys, use `_blocking_keys_match()` to compare them field by field.
4. If no distinguishing information is available (no metadata, no blocking keys), return `True` (treat as matching, allow deduplication).

**Return value semantics:** `True` means "these documents match in identity, so the duplicate flag stands." `False` means "these documents differ in identity, so the duplicate flag should be overridden (Layer 4 protection)."

**Design decision on fallback chain:** The two-tier approach (structural fingerprint -> blocking keys) provides defense in depth. Structural fingerprints require metadata to be provided by the caller (doc_type, date, parties). Blocking keys are extracted automatically from the document text -- either by Claude (high quality) or by regex (best effort). This means Layer 4 can protect documents even when no metadata is supplied, as long as the document text contains identifying information like PO numbers or amounts.

---

### 4.10 `_find_minhash_match_blocking()` -- MinHash LSH with Blocking Key Guard

**Lines 589-635.**

```python
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
```

**Algorithm:**
1. **Query the LSH index** (line 604: `candidates = lsh.query(minhash)`). This returns only files whose MinHash signatures are estimated to have Jaccard similarity >= the threshold (0.85). This is an O(1) operation, not a linear scan.
2. For each candidate, check the **structural fingerprint** first. If both fingerprints are non-empty and they match, return the candidate. If they differ, skip.
3. If structural fingerprints are unavailable, fall back to **blocking keys**. Extract keys for both files (lazy, cached), then use `_blocking_keys_match()`.
4. If no distinguishing information is available, treat as a match and return the candidate.
5. Return `None` if no candidate passes all checks.

**Performance note:** The LSH index provides O(1) candidate lookup, making the overall Layer 3 performance O(n) for the full pipeline instead of the O(n^2) required by the old SimHash linear scan. This is a significant improvement for large document collections. The LSH index uses banded hashing internally -- the MinHash signature is split into bands, and documents that share at least one identical band become candidates.

---

## 5. The Dedup Pipeline Flow

Here is the complete flow for a single file through the pipeline:

```
Input file entry
       |
       v
Compute fingerprints:
  - file_hash (SHA-256 of bytes)
  - content_hash (SHA-256 of normalized text)
  - minhash (128-perm MinHash signature)
  - structural_fingerprint (SHA-256 of metadata)
  - Cache text for lazy blocking key extraction
       |
       v
Layer 1: Is file_hash in seen_file_hashes?
  YES --> Layer 4 check: _identity_match_blocking?
            (structural fingerprint → blocking keys → default)
            YES --> DUPLICATE (layer=1), skip
            NO  --> PROTECTED, continue to next layer
  NO  --> continue
       |
       v
Layer 2: Is content_hash in seen_content_hashes?
  YES --> Layer 4 check: _identity_match_blocking?
            YES --> DUPLICATE (layer=2), skip
            NO  --> PROTECTED, continue to next layer
  NO  --> continue
       |
       v
Layer 3: LSH index query for MinHash candidates (Jaccard >= 0.85)?
  YES --> _find_minhash_match_blocking post-filters:
            structural fingerprint → blocking keys → default
            Match found --> DUPLICATE (layer=3), skip
            No match after L4 filtering --> continue
  NO  --> continue
       |
       v
FILE IS UNIQUE: add to all accumulators + LSH index, add to output
```

**Important:** The layers are checked in order from cheapest to most expensive:
- Layer 1 is a dict lookup (O(1)).
- Layer 2 is a dict lookup (O(1)), but requires text extraction (done once, shared).
- Layer 3 is an LSH index query (O(1) amortized) followed by blocking key verification on candidates.
- Blocking key extraction (Claude call or regex fallback) is **lazy** -- it only happens when a candidate match is found and needs to be verified.

If a file is caught by Layer 1, the more expensive Layer 3 comparison is skipped entirely.

---

## 6. Duplicate Detection vs Near-Duplicate Detection

This module draws a clear line between three concepts:

### Exact duplicates (Layers 1 and 2)

**Layer 1** catches files that are **byte-for-byte identical**. This is the strongest form of deduplication. Two files with the same SHA-256 hash are mathematically certain to be identical (barring SHA-256 collision, which is computationally infeasible).

**Layer 2** catches files with **identical text content but different encoding**. The most common case is the same contract in PDF and DOCX format. The normalization step (lowercase, strip punctuation, collapse whitespace) also catches trivial formatting differences.

### Near-duplicates (Layer 3)

**Layer 3** catches files that are **mostly the same but with small differences**. MinHash estimates Jaccard similarity (intersection over union of token sets). The threshold of 0.85 means documents must share at least 85% of their unique tokens to be considered near-duplicates.

Real-world examples:
- Typo corrections ("consulting" changed to "advisory")
- Minor rephrasing
- Headers/footers added or removed by different systems
- Whitespace or encoding artifacts that survive normalization

### Structural protection (Layer 4)

**Layer 4** is NOT a dedup mechanism -- it is a **dedup override**. It prevents the system from incorrectly flagging legally distinct documents as duplicates. It now has two complementary mechanisms:

1. **Structural fingerprint** (from metadata): Uses `doc_type`, `date`, and `parties` to distinguish documents.
2. **Blocking keys** (from document text): Uses Claude-extracted structured fields (vendor name, PO number, invoice number, amount, date) with a regex fallback. This protects template-based documents that share boilerplate text but have different identifying numbers.

Real-world examples where Layer 4 intervenes:
- A **base MSA** and its **Amendment #1**: same parties, very similar text, but different `doc_type` and `date` (caught by structural fingerprint).
- An **MSA** and a **Statement of Work** under the same MSA: same parties, different `doc_type` (caught by structural fingerprint).
- Two **invoices** from the same vendor using the same template but with **different PO numbers** or **different amounts** (caught by blocking keys).
- Two **purchase orders** to the same vendor on **different dates** (caught by blocking keys).

**Test coverage for Layer 4:** `TestAmendmentNotFlagged` creates two nearly identical PDFs but with different metadata:

```python
files = [
    _entry(str(pdf_base), doc_type="MSA", date="2024-01-15",
           parties=["Acme Corporation", "Widget Incorporated"]),
    _entry(str(pdf_amend), doc_type="Amendment", date="2024-06-01",
           parties=["Acme Corporation", "Widget Incorporated"]),
]
unique = deduplicate_files(files)
assert len(unique) == 2  # Both survive!
```

---

## 7. Test Coverage Walkthrough

The test file at `tests/e2e/test_stage_0b_dedup.py` is organized into focused test classes:

| Class | What It Tests |
|---|---|
| `TestExactDuplicateDetected` | Layer 1: byte-identical PDFs, verifies `dedup_layer=1` |
| `TestContentDuplicateDetected` | Layer 2: same text in PDF vs DOCX, verifies `dedup_layer=2` |
| `TestNearDuplicateDetected` | Layer 3: single-word edit, verifies MinHash Jaccard similarity >= 0.85 and `dedup_layer=3` |
| `TestAmendmentNotFlagged` | Layer 4 protection: MSA vs Amendment both survive (structural fingerprint) |
| `TestUniqueFilesPass` | 3 completely different files all pass through |
| `TestEmptyInput` | Empty list returns empty list |
| `TestComputeStructuralFingerprint` | Determinism, party order independence, sensitivity to doc_type and date |
| `TestExtractText` | PDF and DOCX text extraction, unsupported format fallback |
| `TestComputeFileHash` | Determinism and content sensitivity |
| `TestComputeContentHash` | Case/whitespace/punctuation insensitivity |

### Test helpers

The test file includes sophisticated helpers for creating test PDFs (lines 30-84):

```python
def _build_pdf_bytes(text: str) -> bytes:
    """Build a minimal valid PDF whose page content stream contains *text*."""
```

This function dynamically computes correct xref table offsets, ensuring the test PDFs are valid and readable by pypdf. This is important because MinHash comparison requires actual text extraction -- mock objects would not exercise the full pipeline.

The `_entry()` helper builds minimal input dicts:

```python
def _entry(file_path: str, **kwargs) -> dict:
    base = {"file_path": file_path, "status": "VALID"}
    base.update(kwargs)
    return base
```

---

## 8. Key Takeaways

1. **Layered approach from cheap to expensive.** Layer 1 (file hash) is essentially free; Layer 3 (MinHash + LSH) uses an index for O(1) candidate lookup. By ordering the checks, the pipeline avoids expensive comparisons when cheap ones suffice.

2. **Layer 4 is a veto, not a filter.** It does not find duplicates -- it prevents false positives. This is a critical distinction. Without Layer 4, amendments, SOWs, and template-based invoices with different PO numbers would be incorrectly deduped against each other.

3. **Two-tier Layer 4 protection.** The structural fingerprint (from metadata) is checked first. If metadata is unavailable, Claude-extracted blocking keys provide a second line of defense. The regex fallback ensures some protection even when Claude is unavailable. This means Layer 4 can protect documents even when the caller provides no metadata, as long as the document text contains identifying information.

4. **Blocking key extraction is lazy and cached.** Claude is only called when a candidate match is found and needs to be verified. Results are cached per file path, so each file is sent to Claude at most once. This minimizes API costs.

5. **In-place mutation of input entries.** `deduplicate_files()` adds fingerprint fields to every entry dict and adds `is_duplicate`/`duplicate_of`/`dedup_layer` to duplicates. Callers should be aware that the input list is modified even though duplicates are excluded from the return value. This design allows callers to inspect duplicate entries after the call (e.g., for reporting).

6. **Text normalization is deliberately aggressive.** Lowercase + strip punctuation + collapse whitespace. This means documents that differ only in formatting, capitalization, or punctuation will have identical content hashes. This is usually desirable for contract dedup but could cause false positives in edge cases (e.g., two contracts that differ only in a date that is written in words).

7. **MinHash threshold of 0.85 is tunable.** The Jaccard similarity threshold balances sensitivity (catching true near-duplicates) against specificity (not flagging genuinely different documents). At 0.85, documents must share at least 85% of their unique tokens. This is passed as a parameter to `deduplicate_files()` and can be adjusted per use case.

---

## 9. Watch Out For

1. **Text extraction is extension-based, not MIME-based.** `extract_text()` uses `file_path.lower().endswith(".pdf")`. If a PDF has an unusual extension (e.g., `.PDF.bak`), text extraction will fail silently and the file will get an empty text hash. This means Layers 2 and 3 will not catch duplicates involving that file. Only Layer 1 (raw bytes) will work.

2. **The `datasketch` library must be installed.** It is imported at module level (`from datasketch import MinHash, MinHashLSH`), so the module will fail to load if the package is missing. Unlike other optional deps in Stage 0a, there is no graceful fallback.

3. **Claude client is optional but recommended.** If `claude_client=None` is passed to `deduplicate_files()`, blocking key extraction falls back to regex patterns, which are less accurate than Claude. The regex fallback can only extract PO numbers (long digit sequences), dollar amounts, and dates -- it cannot identify vendor names, invoice numbers, or contract references.

4. **Layer 4 structural fingerprint only works with metadata.** If your pipeline does not provide `doc_type`, `date`, and `parties` in the file entries, the structural fingerprint component of Layer 4 is effectively disabled. However, blocking keys (from document text) still provide protection in this case.

5. **Processing order matters.** The first file seen is always kept as the "original." If you pass `[amendment, base_contract]` (amendment first), the amendment survives and the base contract is flagged as a duplicate (absent Layer 4 protection). Always ensure the canonical/primary file is listed first, or provide metadata/blocking keys for Layer 4 to work correctly.

6. **The content hash treats all punctuation as noise.** The `_PUNCTUATION_TABLE` strips ALL punctuation characters. This means `"Section 1.1"` and `"Section 11"` normalize to the same string (`"section 11"`). For contract text this is rarely a problem, but it is a known limitation.

7. **DOCX paragraphs only.** `_extract_docx_text()` extracts text from `doc.paragraphs` only. Text in tables, headers, footers, or text boxes inside the DOCX is not extracted. This could cause false negatives in Layer 2/3 if significant contract content lives in tables.

8. **Duplicate entries are mutated but excluded.** The return value of `deduplicate_files()` contains only unique entries. To access information about which files were flagged as duplicates and why, you must inspect the original input list. The `files[1]` pattern used in tests demonstrates this.

9. **Blocking key normalization strips vendor suffixes.** `_normalize_vendor()` removes common suffixes like LLC, Inc, Corp, Ltd. This means "Acme Corp" and "Acme Corporation" will match, which is usually desirable. However, if two genuinely different companies share a base name (e.g., "Phoenix LLC" and "Phoenix Corp"), they would be treated as the same vendor.

10. **Only the first 4,000 characters are sent to Claude.** `MAX_TEXT_FOR_BLOCKING = 4000` truncates the document text before sending it to Claude. This assumes identifying information (vendor name, PO number, dates) is in the document header. Documents that place identifying information deep in the body may not have their blocking keys correctly extracted.
