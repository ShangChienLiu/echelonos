# Stage 1: OCR Processing

> **Linear ticket:** AKS-13

This tutorial covers Stage 1 of the Echelonos contract obligation extraction pipeline. Stage 1 takes validated files from Stage 0a and runs them through Mistral OCR to extract machine-readable text, page-level confidence scores, and structured table data. The output of Stage 1 feeds directly into Stage 2 (Document Classification).

---

## Table of Contents

- [Overview](#overview)
- [Where Stage 1 Fits in the Pipeline](#where-stage-1-fits-in-the-pipeline)
- [File-by-File Walkthrough](#file-by-file-walkthrough)
  - [Mistral Client: `mistral_client.py`](#mistral-client-mistral_clientpy)
  - [Stage Logic: `stage_1_ocr.py`](#stage-logic-stage_1_ocrpy)
  - [Tests: `test_stage_1_ocr.py`](#tests-test_stage_1_ocrpy)
- [Key Takeaways](#key-takeaways)
- [Watch Out For](#watch-out-for)

---

## Overview

Not every contract document arrives as clean, searchable text. Many contracts are scanned images, faxed PDFs, or photographed pages. Stage 1 handles these cases by sending documents to Mistral's OCR service, which performs Optical Character Recognition (OCR) and returns:

1. **Per-page text** -- the raw text content recognized on each page.
2. **Table structures** -- any tabular data detected, converted to Markdown format so downstream stages can reason about pricing schedules, obligation matrices, etc.
3. **Confidence scores** -- a float between 0.0 and 1.0 per page indicating how confident the OCR engine is in its recognition results.

Stage 1 also implements a **quality gate**: pages with low OCR confidence are flagged so that downstream stages (and human reviewers) know which text may be unreliable.

---

## Where Stage 1 Fits in the Pipeline

```
Stage 0a (Validation)
  |
  |-- needs_ocr = True  --> Stage 1 (OCR) --> Stage 2 (Classification)
  |-- needs_ocr = False --> [text extraction] --> Stage 2 (Classification)
```

Stage 0a (`src/echelonos/stages/stage_0a_validation.py`) produces a validation result for each file. One critical field in that result is `needs_ocr` (a boolean). When a file is an image format (PNG, JPG, TIFF) or a PDF, Stage 0a sets `needs_ocr=True`. All PDFs now always route through OCR because Mistral OCR handles both scanned and text-based PDFs effectively. Those files are routed through Stage 1.

After Stage 1 runs OCR, the extracted text is assembled into a single string via `get_full_text()` and passed to Stage 2 for document classification.

---

## File-by-File Walkthrough

### Mistral Client: `mistral_client.py`

**File:** `src/echelonos/ocr/mistral_client.py`

This module is the low-level interface to Mistral's OCR API. It contains three functions and no classes.

#### `get_mistral_client()` (lines 29-31)

```python
def get_mistral_client() -> Mistral:
    """Create and return a Mistral client from application settings."""
    return Mistral(api_key=settings.mistral_api_key)
```

Factory function that creates a `Mistral` client using the API key from configuration (`src/echelonos/config.py`). The key is read from environment variables via `pydantic_settings`.

**Design decision:** The client is created fresh each time rather than being a singleton. This keeps the module stateless and makes testing simpler -- callers can inject a mock client without worrying about shared state.

#### `_detect_mime()` (lines 34-40)

A helper that maps file extensions to MIME types using a built-in `_MIME_MAP` dictionary, with a fallback to Python's `mimetypes.guess_type()`. This is used to construct the correct data URI when uploading documents to Mistral.

#### `analyze_document()` (lines 43-137)

```python
def analyze_document(client: Mistral, file_path: str) -> dict:
```

This is the core OCR function. It does three things:

**1. Read and encode the file (lines 64-69)**

```python
file_bytes = Path(file_path).read_bytes()
b64_data = base64.standard_b64encode(file_bytes).decode("utf-8")
mime_type = _detect_mime(file_path)
data_uri = f"data:{mime_type};base64,{b64_data}"
```

The file is read from disk, base64-encoded, and packaged as a data URI for inline upload. Depending on the MIME type, the document is sent either as an `image_url` (for image files) or a `document_url` (for PDFs and other document formats).

**2. Call the Mistral OCR API (lines 79-83)**

```python
ocr_response = client.ocr.process(
    model=settings.mistral_ocr_model,
    document=document,
    include_image_base64=False,
)
```

The model used is configured via `settings.mistral_ocr_model`. The `include_image_base64=False` flag avoids returning image data in the response, reducing payload size.

**3. Parse Mistral's markdown response into page structures (lines 86-137)**

Mistral returns everything as markdown -- the function splits each page's markdown content into text and table parts by detecting lines that start and end with `|` (pipe characters). Tables are collected as separate markdown strings while non-table content becomes the page text.

A key detail: Mistral uses 0-indexed page numbers, so the code adds 1 (`page.index + 1`) to produce 1-indexed page numbers consistent with the rest of the pipeline.

Mistral OCR does not provide per-page confidence scores. The module uses a default confidence of `0.95` since Mistral OCR is generally high-quality for printed documents.

**Return value (line 137):**

```python
return {"pages": pages, "total_pages": len(pages)}
```

A dictionary with two keys: the list of per-page data and the total page count.

---

### Stage Logic: `stage_1_ocr.py`

**File:** `src/echelonos/stages/stage_1_ocr.py`

This module wraps the Mistral client with retry logic, confidence assessment, and a clean public API. It is the module that other parts of the pipeline import.

#### Confidence Thresholds (lines 27-28)

```python
LOW_CONFIDENCE_THRESHOLD: float = 0.60
MEDIUM_CONFIDENCE_THRESHOLD: float = 0.85
```

Two module-level constants define the quality gate boundaries:

| Confidence Range | Flag Type | Meaning |
|---|---|---|
| Below 0.60 | `LOW_OCR_QUALITY` | Text is likely unreliable; human review strongly recommended |
| 0.60 to 0.84 | `MEDIUM_OCR_QUALITY` | Text may have errors; proceed with caution |
| 0.85 and above | (no flag) | Text is considered reliable |

**Design decision:** These are strict less-than comparisons. A page at exactly 0.60 is categorized as `MEDIUM`, not `LOW`. A page at exactly 0.85 is not flagged at all. This is verified in the test suite (see the boundary tests below).

#### `_assess_confidence()` (lines 36-82)

```python
def _assess_confidence(pages: list[dict]) -> list[dict]:
```

Iterates over every page, reads its `ocr_confidence` value, and produces flag dictionaries for any page that falls below the thresholds. Each flag contains:

- `page_number` (int) -- identifies which page has the issue.
- `flag_type` (str) -- either `"LOW_OCR_QUALITY"` or `"MEDIUM_OCR_QUALITY"`.
- `message` (str) -- a human-readable description including the actual confidence value and the threshold it violates.

The function also emits structured log events: `log.warning` for low confidence (line 60) and `log.info` for medium confidence (line 76). This distinction is intentional -- low confidence is an actionable warning, while medium confidence is informational.

#### `_build_page_result()` (lines 85-99)

```python
def _build_page_result(raw_page: dict) -> dict:
```

A normalisation function that transforms the raw Mistral client output into the Stage 1 output schema. Key transformations:

1. **Tables are joined** -- the `tables` list (multiple Markdown table strings) is collapsed into a single `tables_markdown` string joined by double newlines (line 93).
2. **Confidence is renamed** -- `confidence` becomes `ocr_confidence` to make the field name unambiguous when it appears alongside other confidence scores later in the pipeline (line 98).

#### `_call_mistral()` (lines 101-109)

```python
@retry(
    retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _call_mistral(client, file_path: str) -> dict:
    """Call Mistral OCR with retry logic for transient errors."""
    return analyze_document(client, file_path)
```

This function wraps the Mistral call with the `tenacity` retry library. The retry configuration is worth studying carefully:

- **`retry_if_exception_type`** -- only retries on transient/network errors: `ConnectionError`, `TimeoutError`. Permanent errors (like `ValueError` for an invalid file) will not be retried.
- **`stop_after_attempt(3)`** -- maximum 3 attempts total (1 initial + 2 retries).
- **`wait_exponential(multiplier=1, min=2, max=30)`** -- exponential backoff starting at 2 seconds, doubling each time, capping at 30 seconds. So the waits are approximately 2s, 4s.
- **`reraise=True`** -- after all retries are exhausted, the original exception is re-raised rather than wrapped in a `tenacity.RetryError`. This keeps the error handling in `ingest_document` clean.

**Design decision:** The retry decorator is applied to a thin wrapper rather than directly to `analyze_document` in `mistral_client.py`. This keeps the retry policy as a stage-level concern, not an infrastructure concern. Different stages or callers could apply different retry strategies.

#### `ingest_document()` (lines 117-189) -- The Public Entry Point

```python
def ingest_document(
    file_path: str,
    doc_id: str,
    ocr_client=None,
) -> dict:
```

This is the main function that the pipeline orchestrator calls. It accepts:

- `file_path` -- the path to the PDF file to OCR.
- `doc_id` -- a unique identifier for tracking the document through the pipeline.
- `ocr_client` -- an optional pre-configured `Mistral` client (critical for testing).

**Execution flow:**

1. **Client initialization (lines 145-146):** If no client is provided, one is created via `get_mistral_client()`. This dependency-injection pattern is used throughout Echelonos for testability.

2. **Mistral API call with error handling (lines 148-168):** The function calls `_call_mistral()` inside a try/except block. On any error, the function returns a result dict with an `OCR_ERROR` flag and a message referencing "Mistral OCR API error". The error result has the same shape as a success result (`doc_id`, `pages`, `total_pages`, `flags`) but with an empty `pages` list and `total_pages=0`. This is a critical design decision -- **error results are structurally identical to success results**. Downstream code can check for `OCR_ERROR` flags without needing special error-handling logic.

3. **Page normalisation (line 171):** Each raw page is passed through `_build_page_result()` to produce the standardised output schema.

4. **Confidence quality gate (line 175):** `_assess_confidence()` scans all pages and produces flags for low/medium confidence pages.

5. **Return value (lines 184-189):**

```python
return {
    "doc_id": doc_id,
    "pages": pages,
    "total_pages": total_pages,
    "flags": flags,
}
```

#### `get_full_text()` (lines 212-239)

```python
def get_full_text(pages: list[dict]) -> str:
```

A utility function that concatenates all page text into a single string for downstream stages (particularly Stage 2 classification). Key details:

- **Page separator:** Pages are joined with `\f` (form-feed character, line 239). This is a deliberate choice -- form-feed is a traditional page-break character that downstream text processing can use to identify page boundaries without ambiguity.
- **Tables are included:** If a page has `tables_markdown`, it is appended after the page text (lines 234-236). This ensures that table content (pricing schedules, obligation matrices, etc.) is available to the classification and extraction stages.
- **Edge cases:** Empty `text` and empty `tables_markdown` are handled gracefully -- they are simply skipped, avoiding empty lines in the output.

---

### Tests: `test_stage_1_ocr.py`

**File:** `tests/e2e/test_stage_1_ocr.py`

The test file provides comprehensive coverage of Stage 1, organized into six test classes. Since Mistral OCR is an external service, all tests mock the Mistral client with realistic response structures.

#### Mock Helpers

The test file defines mock builders that simulate Mistral's OCR API response objects. The mocks replicate the structure returned by `mistral_client.analyze_document()`: a dictionary with `pages` (list of page dicts, each with `page_number`, `text`, `tables`, and `confidence`) and `total_pages`.

#### TestIngestDocumentSuccess (lines 94-142)

**`test_ingest_document_success`** (line 97): The primary happy-path test. Creates a mock Azure response with 3 pages of contract text, verifies:
- The `doc_id` is passed through correctly.
- `total_pages` equals 3.
- The `pages` list has 3 entries.
- Each page contains the expected text fragments.
- `ocr_confidence` values match what was set on the mock spans.
- High-confidence pages produce zero flags.

#### TestIngestDocumentWithTables (lines 145-224)

**`test_ingest_document_with_tables`** (line 148): Verifies that a table on page 1 is correctly converted to Markdown and placed in `tables_markdown` for page 1, while page 2 has empty `tables_markdown`. Checks for pipe characters (`|`) and separator rows (`---`).

**`test_multiple_tables_on_same_page`** (line 191): Verifies that when two tables appear on the same page, both are present in the `tables_markdown` field. This tests the join behavior in `_build_page_result()` (the `"\n\n".join(...)` on line 93 of `stage_1_ocr.py`).

#### TestGetFullText (lines 227-294)

Four tests covering `get_full_text()`:

- **`test_get_full_text`** (line 230): Multi-page document -- checks that all page texts appear, table markdown is included, and exactly 2 form-feed characters separate 3 pages.
- **`test_get_full_text_single_page`** (line 263): Single page -- no form-feed separator expected.
- **`test_get_full_text_empty`** (line 278): Empty list returns empty string.
- **`test_get_full_text_with_tables_only`** (line 282): Pages with no paragraph text but with table markdown still produce output.

#### TestConfidenceFlagging (lines 297-417)

This is the most thorough test class, covering the quality gate:

- **`test_low_confidence_flagging`** (line 300): Two pages at 0.45 and 0.30 confidence are flagged as `LOW_OCR_QUALITY`. Verifies flag count, page numbers, and message text.
- **`test_medium_confidence_warning`** (line 330): Two pages at 0.70 and 0.75 are flagged as `MEDIUM_OCR_QUALITY`.
- **`test_mixed_confidence_levels`** (line 359): One page at each level (0.40 LOW, 0.72 MEDIUM, 0.95 HIGH) -- verifies that exactly one LOW and one MEDIUM flag are produced, and the high-confidence page is clean.
- **`test_exactly_at_low_threshold`** (line 388): A page at exactly 0.60 must NOT be LOW; it must be MEDIUM. Tests the boundary condition of the strict less-than comparison.
- **`test_exactly_at_medium_threshold`** (line 405): A page at exactly 0.85 must NOT be flagged at all.

#### TestEmptyDocument (lines 420-436)

**`test_empty_document`** (line 423): An Azure result with zero pages returns an empty `pages` list, `total_pages=0`, and no flags.

#### TestMistralApiErrorHandling

Tests covering error scenarios:

- **`test_ocr_api_error`**: An exception from the Mistral OCR API is caught, producing an `OCR_ERROR` flag with a message referencing "Mistral OCR API error". Uses `patch` to bypass tenacity retries in tests.
- **`test_error_result_shape_matches_success`**: Verifies that error results contain exactly the same top-level keys (`doc_id`, `pages`, `total_pages`, `flags`) as success results. This is a contract test that protects downstream code.

---

## Key Takeaways

1. **Structural consistency matters.** Both success and error results share the same dictionary shape (`doc_id`, `pages`, `total_pages`, `flags`). This eliminates the need for special error-handling branches in downstream code -- any consumer can check `flags` for `OCR_ERROR` entries.

2. **The quality gate is a signal, not a blocker.** Low and medium OCR confidence flags are informational. The pipeline does not stop processing when OCR quality is poor. Instead, flags propagate forward so that later stages and human reviewers can decide how to handle unreliable text.

3. **Tables are first-class citizens.** Table data is explicitly extracted, converted to Markdown, and associated with the correct page. This is critical because contract documents frequently contain pricing tables, obligation matrices, and schedule data that pure text extraction would mangle.

4. **Retries are scoped to transient errors.** The tenacity configuration on `_call_mistral()` only retries errors that could plausibly succeed on a second attempt (network timeouts, connection errors). Programming errors or invalid input are not retried.

5. **Dependency injection enables testing.** Every public function accepts an optional client parameter. Tests pass mock clients without touching environment variables or configuration. This pattern is used consistently across all Echelonos stages.

6. **`get_full_text()` uses form-feed separators.** The `\f` character preserves page boundary information in the concatenated output. Downstream stages can split on `\f` to recover page-level context if needed.

---

## Watch Out For

1. **Confidence thresholds are strict less-than comparisons.** A page at exactly 0.60 is `MEDIUM`, not `LOW`. A page at exactly 0.85 is unflagged. If you change the threshold values, make sure you understand and update the boundary tests in `test_stage_1_ocr.py` (lines 388-417).

2. **The Mistral OCR model is configured via `settings.mistral_ocr_model`.** This is read from the application configuration. If you need to switch to a different Mistral model, update the configuration rather than modifying `mistral_client.py` directly.

3. **Table detection uses pipe-character heuristics.** The table splitter in `mistral_client.py` identifies table lines as those starting and ending with `|`. Mistral returns everything as markdown, so non-standard table formats or tables with missing pipe characters may not be correctly separated from text content.

4. **Mistral OCR does not provide per-page confidence scores.** A default confidence of `0.95` is used for all pages. This means the confidence quality gate in `_assess_confidence()` will rarely trigger. If you need true per-page confidence, you would need to implement an additional quality check.

5. **The retry decorator uses `reraise=True`.** After 3 failed attempts, the original exception propagates up. The `ingest_document()` function catches these re-raised exceptions in its try/except block. If you add new exception types to the retry configuration, make sure they are also handled in `ingest_document()`.

6. **Tests patch `_call_mistral` to bypass retries.** In the error-handling tests, `_call_mistral` is patched at the module level rather than mocking the Mistral client directly. This is because tenacity would otherwise retry the mock 3 times before the exception propagates, slowing down the test suite. If you refactor the retry logic, update these patches accordingly.

7. **OCR results are not cached.** Each call to `ingest_document()` makes a fresh Mistral API call. If the same document is processed twice (e.g., after a pipeline retry), it will incur duplicate API charges. Consider adding a caching layer at the orchestration level if reprocessing is common in your deployment.

8. **Files are base64-encoded for upload.** The `analyze_document()` function in `mistral_client.py` reads the entire file into memory and base64-encodes it. For very large files, this increases memory usage by approximately 33%. The MIME type is auto-detected from the file extension via `_detect_mime()`, which supports PDF, PNG, JPG, TIFF, DOCX, and PPTX.
