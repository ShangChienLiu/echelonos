# Testing Patterns and Guide

This tutorial covers the testing infrastructure, conventions, shared fixtures, mocking patterns, and common pitfalls in the Echelonos test suite. Understanding these patterns is essential for writing new tests and debugging failing ones.

---

## Table of Contents

- [Test Organization](#test-organization)
- [Pytest Configuration](#pytest-configuration)
- [Shared Fixtures in conftest.py](#shared-fixtures-in-conftestpy)
  - [Minimal File Bytes](#minimal-file-bytes)
  - [File Fixtures](#file-fixtures)
  - [Expanded Stage 0a Fixtures](#expanded-stage-0a-fixtures)
- [Mocking Patterns](#mocking-patterns)
  - [Mocking LLM Clients (Claude/Anthropic)](#mocking-llm-clients-claudeanthropic)
  - [Mocking External Services (OCR, libmagic)](#mocking-external-services-ocr-libmagic)
  - [Mock Response Builders](#mock-response-builders)
- [E2E Test Patterns](#e2e-test-patterns)
  - [Stage 0a: File Validation](#stage-0a-file-validation)
  - [Stage 3: Obligation Extraction](#stage-3-obligation-extraction)
  - [Stage 5: Amendment Resolution](#stage-5-amendment-resolution)
  - [Stage 7: Report Generation](#stage-7-report-generation)
- [Integration and Database Tests](#integration-and-database-tests)
  - [API + Database Integration](#api--database-integration)
  - [Full Pipeline E2E (Rexair)](#full-pipeline-e2e-rexair)
  - [Idempotent Persistence](#idempotent-persistence)
  - [Pipeline Run/Stop/Cancel](#pipeline-runstopcancellation)
  - [Upload and Clear Database](#upload-and-clear-database)
- [The Tricky Mock Ordering Issue in Stage 5](#the-tricky-mock-ordering-issue-in-stage-5)
- [Running Tests](#running-tests)
- [Key Takeaways](#key-takeaways)
- [Watch Out For](#watch-out-for)

---

## Test Organization

The test suite lives under the `tests/` directory with the following structure:

```
tests/
  conftest.py              # Shared fixtures
  e2e/
    test_stage_0a_validation.py
    test_stage_0b_dedup.py
    test_stage_1_ocr.py
    test_stage_2_classification.py
    test_stage_3_extraction.py
    test_stage_4_linking.py
    test_stage_5_amendment.py
    test_stage_6_evidence.py
    test_stage_7_report.py
    test_api_db_integration.py
    test_full_pipeline_rexair.py
    test_idempotency.py
    test_pipeline_run_stop.py
    test_upload_and_clear.py
    test_dedup_rexair_346.py
```

**Naming conventions:**

- Stage test files are named `test_stage_<N>_<name>.py`, matching the stage they test.
- Integration test files use descriptive names (e.g., `test_api_db_integration.py`, `test_full_pipeline_rexair.py`).
- Test classes are named `Test<DescriptiveName>` and group related test cases.
- Test methods are named `test_<what_is_being_tested>`.
- Helper functions and factories are prefixed with `_` (e.g., `_obligation()`, `_mock_anthropic_client()`).

All end-to-end tests live in `tests/e2e/`. There is no separate `tests/unit/` directory in the current codebase, though the pytest configuration supports `unit` markers.

---

## Pytest Configuration

**File:** `pyproject.toml`, lines 57--63

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "e2e: end-to-end tests",
    "unit: unit tests",
]
```

Key settings:

- **`testpaths = ["tests"]`** -- Pytest only discovers tests under the `tests/` directory, not in `src/` or the project root.
- **`asyncio_mode = "auto"`** -- The `pytest-asyncio` plugin automatically handles async test functions without requiring the `@pytest.mark.asyncio` decorator. This is set in anticipation of async database tests.
- **`markers`** -- Two custom markers are registered: `e2e` and `unit`. These allow selective test execution (e.g., `pytest -m e2e`). However, in the current codebase, tests are not actually decorated with these markers -- they are defined for future use.

### Development Dependencies

**File:** `pyproject.toml`, lines 46--52

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pytest-cov>=6.0",
    "ruff>=0.8",
    "mypy>=1.13",
]
```

Install with `pip install -e ".[dev]"` to get test tools, linting, and type checking.

---

## Shared Fixtures in conftest.py

**File:** `tests/conftest.py`

This file provides reusable test data and file fixtures. It is automatically loaded by pytest for all tests in the `tests/` directory.

### Minimal File Bytes

Three constants define the smallest possible valid files of each type. These are used to create test files without depending on external resources.

#### MINIMAL_PNG (lines 14--19)

```python
MINIMAL_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
    b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00'
    b'\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00'
    b'\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
)
```

A 1x1 pixel PNG image. This is the absolute minimum valid PNG: IHDR chunk (width=1, height=1, 8-bit RGB), a single IDAT chunk with compressed pixel data, and the IEND marker. It passes `libmagic` detection as `image/png`.

#### MINIMAL_JPG (lines 22--43)

A 1x1 white JPEG (~283 bytes). Larger than the PNG because JPEG requires Huffman tables in the file headers. It starts with the JFIF signature (`\xff\xd8\xff\xe0`) and includes a quantization table, frame header, and Huffman tables.

#### MINIMAL_PDF_BYTES (lines 46--77)

```python
MINIMAL_PDF_BYTES = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
...
```

A valid PDF with one page containing the text "Test contract". It includes the full PDF structure: catalog, pages, page object, content stream with a text operator (`BT ... Tj ET`), a font reference, cross-reference table, and trailer. This passes `libmagic` detection as `application/pdf`.

The text content ("Test contract", 13 characters) is deliberately short -- it is below the 50-character-per-page threshold used by `_check_pdf_text_layer()` in Stage 0a. Several tests mock the text layer check because of this.

### File Fixtures

#### tmp_org_folder (lines 85--90)

```python
@pytest.fixture
def tmp_org_folder(tmp_path: Path) -> Path:
    org = tmp_path / "test_org"
    org.mkdir()
    return org
```

Creates a temporary directory named `test_org` inside pytest's built-in `tmp_path`. All file fixtures write into this directory, simulating an organization's upload folder.

#### sample_pdf (lines 93--98)

Writes `MINIMAL_PDF_BYTES` to `tmp_org_folder/sample.pdf`. Returns the `Path` object.

#### sample_docx (lines 101--110)

Uses `python-docx` to create a minimal `.docx` file with one paragraph. Note the `from docx import Document` is inside the fixture (line 104), not at module level. This avoids import errors if `python-docx` is not installed when running tests that do not need this fixture.

#### zero_byte_file (lines 113--118)

Creates an empty file (`b"""`). Used to test validation of corrupted or empty uploads.

### Expanded Stage 0a Fixtures

Lines 126--186 provide fixtures for the expanded file format support:

| Fixture | Lines | What It Creates |
|---------|-------|-----------------|
| `sample_html` | 127--142 | HTML file with contract text |
| `sample_xlsx` | 145--157 | Excel file with 3 columns, 2 data rows (uses `openpyxl`) |
| `sample_png` | 160--165 | Writes `MINIMAL_PNG` to `image.png` |
| `sample_jpg` | 168--173 | Writes `MINIMAL_JPG` to `photo.jpg` |
| `sample_zip` | 176--186 | ZIP containing `inner_contract.pdf` and `notes.txt` |

The `sample_zip` fixture (lines 176--186) is notable because it creates a composite container. Inside the ZIP, `inner_contract.pdf` contains `MINIMAL_PDF_BYTES`, and `notes.txt` contains plain text. This tests the validation pipeline's ability to extract and validate nested files.

---

## Mocking Patterns

The Echelonos pipeline depends on external services (Anthropic Claude, Mistral OCR) that must be mocked for deterministic testing. Each stage has its own mocking approach, but they share common patterns.

### Mocking LLM Clients (Claude/Anthropic)

**Pattern used in:** `tests/e2e/test_stage_3_extraction.py`, `tests/e2e/test_stage_5_amendment.py`

All LLM calls in the pipeline use the Anthropic Claude API. In tests, the Claude client is passed as a `claude_client=MagicMock()` parameter. The stage functions accept this client dependency, making it straightforward to inject mocks:

```python
# Stage 3 extraction uses Claude for party roles, extraction, and CoVe
verified = extract_and_verify(contract_text, claude_client=MagicMock())

# Stage 5 amendment resolution uses Claude for clause comparison
resolved = resolve_amendment_chain(chain_docs, claude_client=MagicMock())
```

For stages where specific mock responses are needed, the `patch()` decorator is used to intercept calls to internal functions that invoke the Claude API, and `side_effect` lists control sequential call behavior.

`side_effect` with a list makes the mock return different values on successive calls. This is essential for testing pipelines that make multiple LLM calls (e.g., Stage 3: dual extraction + agreement check + CoVe).

### Mocking External Services (OCR, libmagic)

**Pattern used in:** `tests/e2e/test_stage_0a_validation.py`

Stage 0a tests mock at two levels:

1. **Internal helpers via `patch()`** -- For example, line 114-117:
   ```python
   with patch(
       "echelonos.stages.stage_0a_validation._check_pdf_text_layer",
       return_value=True,
   ):
       result = validate_file(str(sample_pdf))
   ```
   This patches a private function in the module under test. The patch target string is the **full import path** of the function, not where it is defined but where it is used.

2. **Class-level mocks for complex behavior** -- For password-protected PDF tests (lines 234--244), a custom mock class is defined:
   ```python
   class _MockReader:
       is_encrypted = True
       pages = []

       def decrypt(self, password: str) -> None:
           raise Exception("Invalid password")
   ```
   This is preferred over `MagicMock` when the mock needs specific method behavior (raising an exception).

3. **MIME type mocking** -- Several tests mock `_detect_mime_type` to control format classification:
   ```python
   with patch(
       "echelonos.stages.stage_0a_validation._detect_mime_type",
       return_value="image/png",
   ):
   ```
   This is necessary because `libmagic` may not be installed in all CI environments, and the minimal test files may not always be classified correctly by all versions of libmagic.

### Mock Response Builders

Each test file defines its own response builder helpers tailored to the Claude/Anthropic API response format. Stage functions that need structured output from Claude use internal parsing, so mock responses typically provide the expected data structures directly through `patch()` on internal functions or through `MagicMock()` return values with real Pydantic model instances.

The key principle is that test data uses **real Pydantic model instances** (e.g., `Obligation`, `_ComparisonResponse`), not mocks or dicts. This means the deserialization and validation logic in the production code is exercised identically in tests.

---

## E2E Test Patterns

### Stage 0a: File Validation

**File:** `tests/e2e/test_stage_0a_validation.py`

This test file is the most complex in the suite (703 lines, 12 test classes). Key patterns:

#### Graceful Import Guards (lines 25--48)

```python
try:
    from echelonos.stages.stage_0a_validation import _classify_format
except ImportError:
    _classify_format = None
```

Private helper functions are imported with `try/except` guards, and tests that use them are decorated with `@pytest.mark.skipif`:

```python
@pytest.mark.skipif(_classify_format is None, reason="_classify_format not yet implemented")
class TestClassifyFormat:
    ...
```

This allows the test suite to degrade gracefully when internal APIs change or are not yet implemented. The tests skip rather than fail.

#### Output Schema Validation (lines 57--96)

```python
EXPECTED_RESULT_KEYS = {
    "file_path", "status", "reason", "original_format",
    "needs_ocr", "extracted_from", "child_files",
}

def _assert_result_schema(result: dict) -> None:
    missing = EXPECTED_RESULT_KEYS - set(result.keys())
    assert not missing, f"Result is missing keys: {missing}"
```

A reusable assertion helper that checks every result dict has all 7 expected keys. This is called across multiple test classes (e.g., lines 635, 661, 703) to ensure schema compliance regardless of the validation outcome.

#### Multi-Status Assertions (line 202)

```python
assert result["status"] in ("INVALID", "REJECTED")
```

Some tests accept multiple valid outcomes. For example, a corrupted PDF might be detected as `INVALID` (recognized as PDF but damaged) or `REJECTED` (not recognized as PDF at all), depending on the version of libmagic. Using `in` with a tuple makes the test robust across environments.

### Stage 3: Obligation Extraction

**File:** `tests/e2e/test_stage_3_extraction.py`

This file tests the dual extraction ensemble pipeline. Stage 3 now runs two independent extractions and compares their results for agreement before applying Chain-of-Verification (CoVe). The test classes reflect this:

| Test Class | What It Tests |
|------------|---------------|
| `TestExtractPartyRoles` | Party role extraction from contract text |
| `TestExtractObligations` | Single-pass obligation extraction with Claude |
| `TestGroundingCheck` | Source clause grounding verification (pure logic, no mocks) |
| `TestMatchExtractions` | Matching paired obligations from two independent extractions |
| `TestCheckAgreement` | Agreement checking between matched extraction pairs |
| `TestCoVe` | Chain-of-Verification question/answer generation |
| `TestFullPipeline` | Full `extract_and_verify()` pipeline integration |

Key patterns:

#### Realistic Test Data

A multi-paragraph contract text constant (`SAMPLE_CONTRACT_TEXT`) is defined at module level. This text is realistic enough to test grounding logic (does the source clause actually appear in the text?) while being short enough to be readable in the test file.

#### Dual Extraction Ensemble Tests

The `TestMatchExtractions` class tests `match_extractions()`, which pairs obligations from two independent extraction runs by comparing their source clauses. The `TestCheckAgreement` class tests `check_agreement()`, which determines whether two matched obligations agree on type, responsible party, and text similarity. These replace the former single-model verification approach.

#### Grounding Check Without Mocks

```python
class TestGroundingCheck:
    def test_grounding_check_passes(self) -> None:
        assert verify_grounding(SAMPLE_OBLIGATION, SAMPLE_CONTRACT_TEXT) is True

    def test_grounding_check_fails(self) -> None:
        fabricated = Obligation(
            source_clause="The Vendor shall fly to the moon by end of Q3.",
            ...
        )
        assert verify_grounding(fabricated, SAMPLE_CONTRACT_TEXT) is False
```

The `verify_grounding()` function is pure string matching and does not need any mocks. These are true unit tests embedded in the E2E test file.

### Stage 5: Amendment Resolution

**File:** `tests/e2e/test_stage_5_amendment.py`

Key patterns:

#### Detailed Mock Response Sequencing (lines 550--591)

The full chain resolution test (`TestResolveChainEndToEnd`) is the most complex mock setup in the codebase:

```python
responses = [
    # 1. MSA Delivery vs AMEND_1_DELIVERY -> REPLACE (same type)
    _make_comparison_response(action="REPLACE", ...),
    # 2. MSA Delivery vs AMEND_1_PAYMENT -> UNCHANGED (heuristic)
    _make_comparison_response(action="UNCHANGED", ...),
    # ...
]

# Safety buffer for extra comparisons
for _ in range(10):
    responses.append(_make_comparison_response(action="UNCHANGED", ...))

with _patch_structured_side_effect(responses):
    resolved = resolve_amendment_chain(chain_docs, claude_client=MagicMock())
```

Comments at lines 535--549 explain exactly which pairs will be compared and why, based on the heuristic pre-filter and type-matching logic. This is critical documentation because the comparison order is determined by internal logic that is not immediately obvious.

#### Safety Buffer Pattern (lines 584--591)

```python
for _ in range(10):
    responses.append(
        _make_comparison_response(
            action="UNCHANGED",
            reasoning="Different subject matter.",
            confidence=0.99,
        )
    )
```

Extra `UNCHANGED` responses are appended as a safety buffer. If the heuristic pre-filter passes more pairs than expected (e.g., due to code changes), the test will not crash with `StopIteration` from an exhausted `side_effect` list. Instead, the extra comparisons return `UNCHANGED`, which does not affect the expected test outcomes.

This pattern is used in both `TestResolveChainEndToEnd` (line 584) and `TestResolveAllIntegration` (line 936).

### Stage 7: Report Generation

**File:** `tests/e2e/test_stage_7_report.py`

The simplest test file because Stage 7 is pure Python. See the [Stage 7 tutorial](./11_stage_7_report.md#test-walkthrough) for detailed coverage. The key pattern here is **no mocking at all** -- all tests use inline data builders.

---

## Integration and Database Tests

Five additional test files cover API-database integration, full pipeline workflows, idempotent persistence, pipeline run/stop behavior, and upload/clear functionality. These tests require a running PostgreSQL database (via Docker) and are automatically skipped when the database is unreachable.

### API + Database Integration

**File:** `tests/e2e/test_api_db_integration.py`

Verifies that the API endpoints work correctly with real PostgreSQL data. Each test runs inside a transaction that is rolled back after the test, so no data persists.

| Test Class | What It Tests |
|------------|---------------|
| `TestOrganizationsEndpoint` | `GET /api/organizations` returns orgs from the database |
| `TestReportEndpointRealData` | `GET /api/report/{org_name}` returns a real report when the org exists in DB, and falls back to demo data for unknown orgs. Sub-endpoints (`/obligations`, `/flags`, `/summary`) are also tested with real data. |
| `TestHealthCheck` | `GET /api/health` returns the expected status |

The tests use FastAPI's `TestClient` with a dependency override (`get_db`) to inject a test database session. This avoids any interference with a production database.

### Full Pipeline E2E (Rexair)

**File:** `tests/e2e/test_full_pipeline_rexair.py`

The most comprehensive test file -- it creates a simulated "Rexair" organization with one minimal file of each supported type (PDF, DOCX, HTML, XLSX, PNG, JPG, ZIP) and runs every stage of the pipeline end-to-end with mocked external services (Mistral OCR, Claude).

| Test Class | What It Tests |
|------------|---------------|
| `TestStage0aValidationRexair` | File validation across all formats |
| `TestStage0bDedupRexair` | Deduplication with multi-format files |
| `TestStage1OcrRexair` | OCR/text extraction for each file type |
| `TestStage2ClassificationRexair` | Document classification with Claude |
| `TestStage3ExtractionRexair` | Obligation extraction with dual extraction ensemble |
| `TestStage4LinkingRexair` | Document linking |
| `TestStage5AmendmentRexair` | Amendment resolution |
| `TestStage6EvidenceRexair` | Evidence packaging |
| `TestStage7ReportRexair` | Report generation |
| `TestFullPipelineRexair` | All stages in sequence for the full Rexair org |

When a stage fails, the test report captures the exact stage, file type, and error so the root cause is immediately visible.

### Idempotent Persistence

**File:** `tests/e2e/test_idempotency.py`

Tests the `persist.py` upsert functions to verify that double-inserting identical data produces no duplicates and that mutable fields are updated on re-insert.

| Test Class | What It Tests |
|------------|---------------|
| `TestGetOrCreateOrganization` | Organization get-or-create by name |
| `TestUpsertDocument` | Document upsert by `(org_id, filename)` |
| `TestUpsertObligation` | Obligation upsert by `(doc_id, source_clause)` |
| `TestUpsertDocumentLink` | Document link upsert by `child_doc_id` |
| `TestDoubleIngestion` | Full double-ingestion scenario (insert twice, verify no duplicates) |
| `TestUniqueConstraintEnforcement` | DB-level unique constraint violations raise `IntegrityError` |

Each test runs inside a rolled-back transaction. The tests verify both the application-level upsert logic and the database-level unique constraints.

### Pipeline Run/Stop/Cancellation

**File:** `tests/e2e/test_pipeline_run_stop.py`

Tests the `POST /api/pipeline/run` and `POST /api/pipeline/stop` endpoints. Pipeline stage functions are mocked so the tests run fast and do not require real LLM or OCR credentials.

| Test Class | What It Tests |
|------------|---------------|
| `TestRunPipelineErrors` | Error handling: missing org, no documents, already running |
| `TestStopPipelineErrors` | Error handling: no pipeline running |
| `TestPipelineStatusTransitions` | Status transitions: idle -> processing -> done |
| `TestPipelineCancellation` | Cooperative cancellation via `_cancel_event` |

The tests verify that the background thread is properly launched, that status transitions are correct, and that cancellation is handled gracefully with the correct final state.

### Upload and Clear Database

**File:** `tests/e2e/test_upload_and_clear.py`

Tests the `POST /api/upload` and `DELETE /api/database` endpoints.

| Test Class | What It Tests |
|------------|---------------|
| `TestUploadEndpoint` | File upload, validation, dedup, and persistence via the API |
| `TestPipelineStatus` | Pipeline status reporting during upload |
| `TestClearDatabase` | Clearing all data from all tables, verifying counts |

The upload tests verify that files are saved, validated (Stage 0a), deduplicated (Stage 0b), and persisted to the database. The clear database tests verify that rows are deleted in the correct dependency order and that the response includes accurate per-table deletion counts.

---

## The Tricky Mock Ordering Issue in Stage 5

This deserves its own section because it is the most common source of confusion in the test suite.

### The Problem

Stage 5's `resolve_amendment_chain()` compares MSA obligations against amendment obligations. The comparison pairs depend on:

1. **Obligation type matching** -- Obligations of the same type are always compared.
2. **Keyword overlap heuristic** -- Even if types differ, if the source clauses share enough keywords (>= 20% overlap), they are compared.

This means the number and order of LLM calls is **data-dependent**. If you change the test data (e.g., modify an obligation's text), you may change which heuristic comparisons pass, which changes the required number of mock responses.

### What Happens When It Breaks

If your `side_effect` list does not have enough responses, you get:

```
StopIteration
```

This error is cryptic because it does not tell you which mock ran out. It means one of the `side_effect` lists was exhausted before all calls were made.

### How to Debug It

1. **Add a call count check after the function call** to see how many calls were actually made. The `_patch_structured_side_effect` helper patches `extract_with_structured_output`, so you can inspect the mock's `call_count` attribute.
2. **Read the heuristic code** to understand which pairs will be compared. The comments in `test_resolve_chain_end_to_end` (lines 535--549) are a good reference.
3. **Use the safety buffer pattern.** Always append extra `UNCHANGED` responses after your expected responses.

### Example from the Codebase

In `test_resolve_chain_end_to_end` (line 524), the test expects exactly 5 LLM calls but provides 15 responses (5 expected + 10 safety buffer). The comment block at lines 535--549 documents the exact call order:

```python
#   1. Delivery vs Amend1-Delivery  (same type "Delivery") -> REPLACE
#   2. Delivery vs Amend1-Payment   (heuristic: vendor/within/30/days) -> UNCHANGED
#   3. Payment  vs Amend1-Delivery  (heuristic: vendor/within/days) -> UNCHANGED
#   4. Payment  vs Amend1-Payment   (same type "Financial") -> MODIFY
#   5. SLA      vs Amend2-SLA-Del   (same type "SLA") -> DELETE
```

If you added a new amendment obligation with type "Delivery", you would need to add additional mock responses because it would be compared against every existing MSA Delivery obligation.

---

## Running Tests

### Full Test Suite

```bash
pytest
```

This runs all tests in the `tests/` directory.

### With Coverage

```bash
pytest --cov=echelonos --cov-report=html
```

Generates an HTML coverage report in `htmlcov/`.

### Specific Stage

```bash
pytest tests/e2e/test_stage_7_report.py
```

### Specific Test Class

```bash
pytest tests/e2e/test_stage_5_amendment.py::TestResolveChainEndToEnd
```

### Specific Test Method

```bash
pytest tests/e2e/test_stage_5_amendment.py::TestResolveChainEndToEnd::test_resolve_chain_end_to_end
```

### By Marker (when markers are applied)

```bash
pytest -m e2e
pytest -m "not e2e"
```

### Verbose Output

```bash
pytest -v
```

### Stop on First Failure

```bash
pytest -x
```

---

## Key Takeaways

1. **Tests are self-contained.** Each test file defines its own helpers, test data, and mock builders. Shared fixtures in `conftest.py` are limited to file/directory creation. This trades some DRY-ness for clarity -- you can understand any test file by reading just that file.

2. **Mock responses use real Pydantic models.** Test data uses real model instances, not dicts or mocks. This means the production deserialization code is exercised in tests.

3. **`side_effect` lists define the call contract.** In multi-call tests, the `side_effect` list IS the specification of what the function should call and in what order. Comments should document why each response is in that position.

4. **The safety buffer pattern prevents brittle tests.** Appending extra `UNCHANGED` responses after the expected ones means heuristic changes do not break tests. The test outcomes are determined by the critical responses at the beginning, not the buffer.

5. **Stage 7 tests are the easiest on-ramp.** If you are new to the codebase, start with `test_stage_7_report.py`. It has no mocks, no external dependencies, and clearly demonstrates the input/output contracts.

6. **Database tests use transaction rollback.** All tests that interact with PostgreSQL run inside a transaction that is rolled back after each test. This ensures test isolation without requiring database cleanup or fixtures that create and destroy schemas.

7. **Idempotent persistence is tested explicitly.** The `test_idempotency.py` file verifies that re-uploading the same data produces no duplicates, giving confidence that the `persist.py` upsert functions work correctly.

---

## Watch Out For

1. **`StopIteration` means your mock ran out of responses.** When using `side_effect` with a list, ensure you have enough entries for all calls. Use the safety buffer pattern.

2. **The `MINIMAL_PDF_BYTES` text is too short.** It contains only "Test contract" (13 chars), which is below the 50-char-per-page threshold in `_check_pdf_text_layer()`. Tests that assert `needs_ocr=False` must mock `_check_pdf_text_layer` to return `True`. If you forget the mock, you will get `needs_ocr=True` instead.

3. **Import guards can hide test failures.** The `try/except ImportError` pattern in `test_stage_0a_validation.py` (lines 25--48) means that if you rename a function, the import silently fails and the test is skipped rather than erroring. Check `pytest -v` output for unexpected `SKIPPED` counts.

4. **Database tests require Docker.** The integration tests (`test_api_db_integration.py`, `test_idempotency.py`, `test_pipeline_run_stop.py`, `test_upload_and_clear.py`) connect to the PostgreSQL container defined in `docker-compose.yml`. If Docker is not running, these tests are automatically skipped. Check `pytest -v` output for unexpected `SKIPPED` counts.

5. **Async mode is set but no async tests exist yet.** `asyncio_mode = "auto"` in `pyproject.toml` means any `async def test_*` function will automatically be treated as an async test. This is set up in anticipation of async database tests. If you add async tests, they will work without additional configuration.

6. **Marker registration does not enforce usage.** The `e2e` and `unit` markers are registered in `pyproject.toml` but not applied to test classes or functions. Running `pytest -m e2e` currently runs zero tests. To use markers effectively, add `@pytest.mark.e2e` decorators to the test classes.

7. **`conftest.py` imports are local, not global.** The `sample_docx` fixture imports `from docx import Document` inside the function body (line 104), not at the top of the file. This is intentional -- it prevents `python-docx` from being a hard requirement for running tests that do not need DOCX support. The same pattern is used for `sample_xlsx` with `from openpyxl import Workbook` (line 148).

8. **Full pipeline tests are slow.** `test_full_pipeline_rexair.py` runs all seven stages across multiple file formats. Even with mocked external services, it exercises substantial code paths. Consider running it selectively with `pytest tests/e2e/test_full_pipeline_rexair.py` rather than as part of every test run.
