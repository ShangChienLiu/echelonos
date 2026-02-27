# Stage 7: Report Generation

**Linear ticket:** AKS-19

Stage 7 is the final deliverable step of the Echelonos pipeline. It takes all the data produced by Stages 0--6 (obligations, documents, linking results) and assembles a structured report consisting of three sections: an **Obligation Matrix**, a **Flag Report**, and a **Summary**. Critically, this stage is **pure Python** -- it has no LLM calls, no database I/O, and no external service dependencies. Everything is deterministic.

---

## Table of Contents

- [Source File Overview](#source-file-overview)
- [Pydantic Models](#pydantic-models)
  - [ObligationRow](#obligationrow)
  - [FlagItem](#flagitem)
  - [ObligationReport](#obligationreport)
- [Constants and Configuration](#constants-and-configuration)
- [Source-Reference Formatting](#source-reference-formatting)
  - [_format_source()](#_format_source)
  - [_extract_section_ref()](#_extract_section_ref)
  - [_get_amendment_suffix()](#_get_amendment_suffix)
- [Obligation Matrix Builder](#obligation-matrix-builder)
  - [build_obligation_matrix()](#build_obligation_matrix)
- [Flag Report Builder](#flag-report-builder)
  - [build_flag_report()](#build_flag_report)
- [Summary Builder](#summary-builder)
  - [build_summary()](#build_summary)
- [Main Entry Point](#main-entry-point)
  - [generate_report()](#generate_report)
- [Export Functions](#export-functions)
  - [export_to_markdown()](#export_to_markdown)
  - [export_to_json()](#export_to_json)
- [Test Walkthrough](#test-walkthrough)
- [Key Takeaways](#key-takeaways)
- [Watch Out For](#watch-out-for)

---

## Source File Overview

| File | Purpose |
|------|---------|
| `src/echelonos/stages/stage_7_report.py` | All report generation logic |
| `tests/e2e/test_stage_7_report.py` | End-to-end tests (no mocking required) |

---

## Pydantic Models

All three models are defined at the top of `src/echelonos/stages/stage_7_report.py` (lines 57--93). They use Pydantic `BaseModel` for validation and serialization.

### ObligationRow

**File:** `src/echelonos/stages/stage_7_report.py`, lines 57--69

```python
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
```

This model represents a single row in the obligation matrix. Key design decisions:

- **`number: int`** -- This is a sequential row number assigned *after* sorting, not a persistent ID. It is initially set to `0` during construction and overwritten in `build_obligation_matrix()` (line 254).
- **`source: str`** -- A human-readable formatted reference like `"SOW S4.2 (Amd #2 modified)"`. This is computed by `_format_source()`, not pulled directly from the obligation data.
- **`status: str`** -- One of four values: `ACTIVE`, `SUPERSEDED`, `UNRESOLVED`, or `TERMINATED`. This comes from Stage 5 (Amendment Resolution).
- **`frequency` and `deadline`** -- Both are optional. They default to `None` when the obligation has no recurring schedule or no explicit deadline.
- **`confidence: float`** -- The extraction confidence from Stage 3. This drives the LOW_CONFIDENCE flag detection.

### FlagItem

**File:** `src/echelonos/stages/stage_7_report.py`, lines 72--79

```python
class FlagItem(BaseModel):
    """A single entry in the flag report."""

    flag_type: str  # UNVERIFIED | UNLINKED | AMBIGUOUS | UNRESOLVED | LOW_CONFIDENCE
    severity: str  # RED | ORANGE | YELLOW | WHITE
    entity_type: str  # "obligation" | "document"
    entity_id: str
    message: str
```

Each flag item captures a specific risk or issue found in the extracted data:

- **`flag_type`** -- The category of the flag. There are five types, each with a fixed severity mapping defined in `_SEVERITY_FOR_FLAG` (line 40).
- **`severity`** -- A color-coded severity level. The hierarchy from most to least severe: `RED` > `ORANGE` > `YELLOW` > `WHITE`.
- **`entity_type`** -- Either `"obligation"` or `"document"`, indicating what kind of entity the flag refers to.
- **`entity_id`** -- The ID of the flagged obligation or document. This links back to the source data for traceability.
- **`message`** -- A human-readable description truncated to 80 characters of the obligation text (see lines 321, 332-333, 346-347).

### ObligationReport

**File:** `src/echelonos/stages/stage_7_report.py`, lines 82--93

```python
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
```

The top-level report model. Design decisions:

- **`generated_at: str`** -- Stored as an ISO-format string (not a `datetime` object) to make JSON serialization straightforward. It is generated at report creation time using `datetime.now(timezone.utc).isoformat()` (line 484).
- **Top-level count fields** (`total_obligations`, `active_obligations`, etc.) -- These are pre-computed convenience fields so consumers do not have to iterate over the obligations list. They duplicate information from `summary["by_status"]` but are provided for quick access.
- **`summary: dict`** -- Typed as a plain `dict` rather than a typed model. This gives flexibility for the summary structure to evolve without breaking the Pydantic schema.

---

## Constants and Configuration

**File:** `src/echelonos/stages/stage_7_report.py`, lines 30--49

### Status Sort Order (lines 33--38)

```python
_STATUS_ORDER: dict[str, int] = {
    "ACTIVE": 0,
    "UNRESOLVED": 1,
    "SUPERSEDED": 2,
    "TERMINATED": 3,
}
```

Controls the display order of obligations in the matrix. ACTIVE obligations appear first because they are the most actionable. Unknown statuses get a fallback value of `99` (line 246: `_STATUS_ORDER.get(r.status, 99)`), pushing them to the end.

### Severity Mapping (lines 40--46)

```python
_SEVERITY_FOR_FLAG: dict[str, str] = {
    "UNVERIFIED": "RED",
    "AMBIGUOUS": "ORANGE",
    "UNLINKED": "YELLOW",
    "UNRESOLVED": "YELLOW",
    "LOW_CONFIDENCE": "WHITE",
}
```

Maps each flag type to its severity level. Note that while this mapping is defined, it is **not directly referenced** in the flag building code -- the severities are hardcoded at each `FlagItem` construction site (lines 318, 329, 344, etc.). The `_SEVERITY_FOR_FLAG` dict serves as documentation and could be used for future refactoring.

### Low Confidence Threshold (line 49)

```python
_LOW_CONFIDENCE_THRESHOLD: float = 0.80
```

Obligations with confidence below 0.80 get a LOW_CONFIDENCE flag. This threshold is used on line 326: `if confidence < _LOW_CONFIDENCE_THRESHOLD`.

---

## Source-Reference Formatting

These three private functions work together to produce human-readable source references like `"SOW S4.2 (Amd #2 modified)"`.

### _format_source()

**File:** `src/echelonos/stages/stage_7_report.py`, lines 101--138

```python
def _format_source(
    obligation: dict[str, Any],
    documents: dict[str, dict[str, Any]],
    links: list[dict[str, Any]],
) -> str:
```

This is the orchestrator for source formatting. It:

1. Looks up the document type from the `documents` dict using the obligation's `doc_id` (lines 122--124).
2. Extracts a section reference from `source_clause` via `_extract_section_ref()` (line 128).
3. Builds the base string, e.g., `"SOW S4.2"` (lines 129--132).
4. Checks for amendment suffixes via `_get_amendment_suffix()` (line 135).
5. Returns the final formatted string, optionally with a parenthetical amendment note (lines 137--138).

### _extract_section_ref()

**File:** `src/echelonos/stages/stage_7_report.py`, lines 141--157

Parses section references from source clause text using three regex patterns (lines 147--151):

1. `Section 4.2` or `section 4.2` -- matches `Section` followed by dotted numbers.
2. The Unicode section sign (U+00A7): `SS4.2`.
3. `Article 3` or `Art. 3` -- matches article references.

All results are normalized to the section sign format: `SS4.2`. If no pattern matches, returns an empty string.

### _get_amendment_suffix()

**File:** `src/echelonos/stages/stage_7_report.py`, lines 160--195

Determines if a document is an amendment and, if so, what number it is:

1. Checks if `doc_type` is `"Amendment"` or `"Addendum"` (line 173). If not, returns empty.
2. Searches through the links list for a link where this document is the child (line 178).
3. If found and status is `"LINKED"`, counts all siblings sharing the same parent to determine the amendment number (lines 184--192).
4. Returns a string like `"Amd #2 modified"`.

This logic allows the report to show which amendment modified which clause, giving the reader a clear audit trail.

---

## Obligation Matrix Builder

### build_obligation_matrix()

**File:** `src/echelonos/stages/stage_7_report.py`, lines 203--257

```python
def build_obligation_matrix(
    obligations: list[dict[str, Any]],
    documents: dict[str, dict[str, Any]],
    links: list[dict[str, Any]],
) -> list[ObligationRow]:
```

This function transforms raw obligation dicts into sorted, numbered `ObligationRow` objects:

1. **Iteration** (lines 227--241): For each obligation dict, it calls `_format_source()` and constructs an `ObligationRow` with `number=0` (placeholder).
2. **Sorting** (lines 244--250): Rows are sorted by a composite key:
   - Primary: status order (ACTIVE first, via `_STATUS_ORDER`)
   - Secondary: obligation type (alphabetical)
   - Tertiary: responsible party (alphabetical)
3. **Numbering** (lines 253--254): After sorting, rows are numbered sequentially starting at 1.

The sort order is a deliberate design choice. ACTIVE obligations are what the reader cares about most, so they appear first. Within each status group, alphabetical ordering by type and party provides consistent, predictable output.

---

## Flag Report Builder

### build_flag_report()

**File:** `src/echelonos/stages/stage_7_report.py`, lines 265--381

```python
def build_flag_report(
    obligations: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> list[FlagItem]:
```

This is the most complex function in Stage 7. It generates flags from two sources: obligation-level issues and document-level issues.

**Note the parameter difference**: `documents` here is a `list[dict]`, not a `dict[str, dict]`. This is because `generate_report()` passes `list(documents.values())` on line 471. The documents list is not actually used in the current implementation -- it is reserved for future document-level checks.

#### Obligation-Level Flags (lines 299--351)

Three types of obligation-level flags are checked:

1. **UNVERIFIED (RED)** -- Lines 311--322. Triggered when:
   - The obligation has a `verification_result` dict
   - AND `verification_result["verified"]` is `False`
   - The condition `not is_verified and verification` (line 315) ensures we only flag obligations where verification was attempted but failed. If `verification_result` is `None` or `{}`, no UNVERIFIED flag is raised.

2. **LOW_CONFIDENCE (WHITE)** -- Lines 325--336. Triggered when:
   - `confidence < 0.80` (the `_LOW_CONFIDENCE_THRESHOLD`)
   - Note: the default confidence is `1.0` (line 325), so obligations without a confidence field are NOT flagged.

3. **UNRESOLVED (YELLOW)** -- Lines 339--350. Triggered when:
   - The obligation's `doc_id` is in the set of unlinked document IDs
   - The `unlinked_doc_ids` set is built from links with `status == "UNLINKED"` (lines 302--305)

#### Document-Level Flags (lines 354--378)

Two types of document-level flags come from the linking data:

1. **UNLINKED (YELLOW)** -- Lines 358--364. A document that was supposed to link to a parent but could not find one.
2. **AMBIGUOUS (ORANGE)** -- Lines 367--378. A document with multiple parent candidates. The message includes the candidate count for diagnostic purposes.

---

## Summary Builder

### build_summary()

**File:** `src/echelonos/stages/stage_7_report.py`, lines 389--425

```python
def build_summary(
    obligations: list[ObligationRow],
    flags: list[FlagItem],
) -> dict[str, Any]:
```

Uses `collections.Counter` to aggregate counts across five dimensions:

1. **`by_type`** -- Counts per obligation type (e.g., `{"Delivery": 3, "Financial": 2}`)
2. **`by_status`** -- Counts per status (e.g., `{"ACTIVE": 5, "SUPERSEDED": 1}`)
3. **`by_party`** -- Counts per responsible party
4. **`flags_by_severity`** -- Counts per severity level
5. **`flags_by_type`** -- Counts per flag type

The `Counter` results are converted to plain `dict` (line 410--414) via `dict(Counter(...))` for JSON serialization compatibility. Empty groups produce empty dicts, not missing keys.

---

## Main Entry Point

### generate_report()

**File:** `src/echelonos/stages/stage_7_report.py`, lines 433--503

```python
def generate_report(
    org_name: str,
    obligations: list[dict[str, Any]],
    documents: dict[str, dict[str, Any]],
    links: list[dict[str, Any]],
) -> ObligationReport:
```

This is the **primary entry point** for Stage 7. It orchestrates the entire report generation:

1. **Build matrix** (line 468): Calls `build_obligation_matrix()` to get sorted, numbered rows.
2. **Build flags** (lines 469--473): Calls `build_flag_report()`. Note the conversion `list(documents.values())` on line 471 -- this transforms the `dict[str, dict]` into a `list[dict]` to match the flag builder's expected interface.
3. **Build summary** (line 474): Calls `build_summary()` with the matrix rows and flags.
4. **Compute top-level counts** (lines 477--480): Simple list comprehensions counting obligations by status.
5. **Assemble report** (lines 482--492): Creates the `ObligationReport` with the current UTC timestamp.

The function accepts only **plain dicts** as input, not database models or Pydantic objects. This is by design -- the docstring (lines 4--6) explicitly states that "all functions are pure -- they accept and return plain dicts / Pydantic models so that the Prefect flow layer is responsible for all database I/O."

---

## Export Functions

### export_to_markdown()

**File:** `src/echelonos/stages/stage_7_report.py`, lines 518--637

Renders the report as a complete Markdown document with four sections:

1. **Header** (lines 541--544): Report title and generation timestamp.
2. **Overview** (lines 547--553): Bullet list of top-level counts.
3. **Obligation Matrix** (lines 556--584): A Markdown table with 10 columns. If there are no obligations, it renders `"_No obligations found._"` (line 584).
4. **Flag Report** (lines 589--601): A bullet list with severity indicators like `[RED]`, `[ORANGE]`, etc.
5. **Summary** (lines 605--636): Sub-sections for each summary dimension, rendered as sorted bullet lists.

The severity indicators are defined in `_SEVERITY_INDICATOR` (lines 510--515) and are plain text brackets, not emoji or colored markers, so they render cleanly in any Markdown viewer.

### export_to_json()

**File:** `src/echelonos/stages/stage_7_report.py`, lines 645--658

A thin wrapper around Pydantic's `model_dump_json(indent=2)`. This produces pretty-printed JSON that can be parsed back into an `ObligationReport` for roundtrip testing.

---

## Test Walkthrough

**File:** `tests/e2e/test_stage_7_report.py`

Since Stage 7 is pure Python with no external dependencies, the tests are **fully self-contained**. There are no mocks, no fixtures from `conftest.py`, and no environment variables needed.

### Test Helpers (lines 30--183)

The test file defines three helper functions and three sample data factories:

- **`_obligation()`** (lines 33--62): Builds a minimal obligation dict with sensible defaults. Uses keyword-only arguments for clarity.
- **`_document()`** (lines 65--76): Builds a minimal document dict.
- **`_link()`** (lines 79--92): Builds a minimal link dict.
- **`_sample_obligations()`** (lines 95--164): Returns 5 obligations covering different types, statuses, and confidence levels. This is the core test dataset.
- **`_sample_documents()`** (lines 167--174): Returns a mapping of 4 documents (MSA, SOW, NDA, Amendment).
- **`_sample_links()`** (lines 177--182): Returns 2 links showing doc-sow and doc-amd linked to doc-msa.

### Test Classes

| Class | Lines | What It Tests |
|-------|-------|---------------|
| `TestBuildObligationMatrix` | 190--241 | Matrix construction, source formatting, amendment suffixes |
| `TestMatrixSorting` | 249--291 | Sort order: ACTIVE before SUPERSEDED before TERMINATED; alphabetical within status groups |
| `TestBuildFlagReportUnverified` | 299--318 | UNVERIFIED flag (RED) when verification fails |
| `TestBuildFlagReportUnlinked` | 326--365 | UNLINKED (YELLOW) and AMBIGUOUS (ORANGE) flags from linking results |
| `TestBuildFlagReportLowConfidence` | 373--425 | LOW_CONFIDENCE (WHITE) flag for confidence < 0.80; no flag for high confidence; UNRESOLVED from unlinked doc |
| `TestBuildSummary` | 432--515 | Correct Counter aggregation across all five dimensions |
| `TestGenerateReportComplete` | 523--584 | Full report generation with all sections; correct counts; flags generated; summary keys present |
| `TestExportToMarkdown` | 592--653 | Markdown contains header, table delimiters, section headers, obligation data, severity indicators |
| `TestExportToJson` | 660--711 | Valid JSON output; roundtrip parsing; all expected fields present |
| `TestEmptyObligations` | 719--759 | Empty input produces empty report with zero counts, empty lists, empty summary dicts |

### Notable Test Patterns

1. **Inline test data** (no conftest fixtures): Every test builds its own data. This makes tests self-documenting and avoids hidden dependencies.

2. **Section reference assertion** (line 220-221):
   ```python
   assert "\u00a74.2" in rows[0].source  # SS4.2 (section sign character)
   ```
   Note the use of the Unicode escape `\u00a7` (SS) instead of the literal character, making the assertion unambiguous.

3. **Empty state testing** (lines 719--759): The `TestEmptyObligations` class is thorough -- it tests the report, Markdown export, JSON export, and summary structure all with zero obligations.

4. **JSON roundtrip** (lines 678--692): Tests that JSON output can be parsed back, ensuring no data loss in serialization.

---

## Key Takeaways

1. **Pure functions by design.** Stage 7 has zero side effects. It takes data in, produces a report out. This makes it trivially testable and easy to reason about.

2. **Three-section architecture.** The report is always Obligation Matrix + Flag Report + Summary. Each section is built by a dedicated function, making it easy to modify one section without affecting others.

3. **Flag detection is multi-source.** Flags come from three different inputs: obligation verification results, obligation confidence scores, and document linking results. Understanding which input drives which flag type is essential for debugging.

4. **Sorting is deliberate.** ACTIVE obligations appear first because they are the most actionable. The composite sort key (status, then type, then party) provides consistent, deterministic output.

5. **The `documents` parameter inconsistency.** `generate_report()` accepts `dict[str, dict]` for documents, but `build_flag_report()` accepts `list[dict]`. The conversion happens on line 471. Be aware of this when calling these functions directly.

---

## Watch Out For

1. **The `number` field is ephemeral.** Row numbers are assigned after sorting and are not stable across different input data. Do not use them as persistent identifiers.

2. **The UNVERIFIED flag condition is subtle.** Line 315: `not is_verified and verification`. This means:
   - `verification_result = None` -- No flag (no verification was attempted)
   - `verification_result = {}` -- No flag (empty dict is falsy)
   - `verification_result = {"verified": False}` -- **UNVERIFIED flag raised**
   - `verification_result = {"verified": True}` -- No flag

3. **LOW_CONFIDENCE default is 1.0, not 0.0.** Line 325: `confidence = obl.get("confidence", 1.0)`. If an obligation dict is missing the `confidence` key, it defaults to 1.0 (maximum confidence), so it will NOT be flagged. But in `ObligationRow` (line 239), a missing confidence defaults to `0.0`. This asymmetry means the same obligation might get flagged differently depending on whether it is processed as a raw dict (for flags) or as an ObligationRow (for display).

4. **Amendment numbering depends on link order.** The `_get_amendment_suffix()` function counts siblings by their position in the links list (line 190). If links are reordered, amendment numbers change. The links list order must be stable.

5. **The `_SEVERITY_FOR_FLAG` dict is unused in runtime code.** It exists as documentation but the actual severities are hardcoded at each `FlagItem()` construction site. If you change the mapping in the dict, you must also update the individual construction sites.
