# Stage 2: Document Classification

> **Linear ticket:** AKS-14

This tutorial covers Stage 2 of the Echelonos contract obligation extraction pipeline. Stage 2 takes the full text output of Stage 1 (OCR) and classifies each document into a contract category (MSA, SOW, Amendment, etc.) using Claude with structured output via tool_use. It also extracts key metadata -- parties, effective date, and parent contract references -- and applies rule-based cross-checks to catch misclassifications.

---

## Table of Contents

- [Overview](#overview)
- [Where Stage 2 Fits in the Pipeline](#where-stage-2-fits-in-the-pipeline)
- [File-by-File Walkthrough](#file-by-file-walkthrough)
  - [Claude Client: `claude_client.py`](#claude-client-claude_clientpy)
  - [Stage Logic: `stage_2_classification.py`](#stage-logic-stage_2_classificationpy)
  - [Tests: `test_stage_2_classification.py`](#tests-test_stage_2_classificationpy)
- [Key Takeaways](#key-takeaways)
- [Watch Out For](#watch-out-for)

---

## Overview

Once a document's text has been extracted (either via OCR in Stage 1, or directly from text-based formats), the pipeline needs to understand *what kind of contract* it is dealing with. A Master Service Agreement has different obligation patterns than a Statement of Work or an Amendment. Classification drives downstream extraction: the obligation extraction prompt in Stage 3 is informed by the document type.

Stage 2 uses a two-layer classification approach:

1. **LLM classification** -- Claude reads the document text and returns a structured classification result (document type, parties, effective date, parent reference, confidence score) via tool_use.
2. **Rule-based cross-checking** -- regex patterns scan the document text for textual signals that may contradict the LLM's classification. For example, if the LLM classifies a document as an MSA but the text contains "hereby amends", the classification is corrected to Amendment.

---

## Where Stage 2 Fits in the Pipeline

```
Stage 1 (OCR)
  |
  v
get_full_text(pages)  -->  Stage 2 (Classification)  -->  Stage 3 (Extraction)
                                |
                                v
                         ClassificationResult:
                           - doc_type
                           - parties
                           - effective_date
                           - parent_reference_raw
                           - confidence
```

Stage 2 receives the full document text (produced by `get_full_text()` in `stage_1_ocr.py`, which joins all page texts with form-feed separators). The output is a `ClassificationResult` Pydantic model that Stage 3 uses to tailor its obligation extraction strategy.

---

## File-by-File Walkthrough

### Claude Client: `claude_client.py`

**File:** `src/echelonos/llm/claude_client.py`

This module provides the low-level interface to Anthropic's Claude API. It is shared across multiple stages (Stage 2 for classification, Stage 3 for extraction, Stage 5 for amendment resolution).

#### `get_anthropic_client()` (lines 19-20)

```python
def get_anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)
```

Factory function that creates an `Anthropic` client using the API key from configuration (`src/echelonos/config.py`). A fresh instance is created each call for statelessness.

#### `extract_with_structured_output()` (lines 23-80)

```python
def extract_with_structured_output(
    client: anthropic.Anthropic,
    system_prompt: str,
    user_prompt: str,
    response_format: type,
):
```

This function is the cornerstone of Echelonos's LLM integration. It uses Claude's **tool_use** feature to enforce structured output. Key details:

- **Tool definition from Pydantic schema** -- the Pydantic model class is converted to a JSON Schema via `model_json_schema()`, which is then passed as a tool's `input_schema`. This forces Claude to produce valid JSON matching the schema.
- **`tool_choice={"type": "tool", "name": tool_name}`** -- forces Claude to call the tool (rather than responding with free-form text), guaranteeing structured output.
- **`response_format.model_validate(block.input)`** -- the tool call input is validated and deserialized into the Pydantic model instance. No manual JSON parsing is needed.
- **`model=settings.anthropic_model`** -- configured via the application settings.

**Design decision:** The function signature takes a generic `response_format: type` rather than being specific to classification. This allows the same function to be reused in Stage 3 for obligation extraction and Stage 5 for clause comparison, each with a completely different Pydantic model.

---

### Stage Logic: `stage_2_classification.py`

**File:** `src/echelonos/stages/stage_2_classification.py`

This is the main classification module. It defines the classification schema, the LLM prompt, and two public functions.

#### Valid Document Types (lines 22-30)

```python
VALID_DOC_TYPES = frozenset({
    "MSA",
    "SOW",
    "Amendment",
    "Addendum",
    "NDA",
    "Order Form",
    "Other",
})
```

A frozen set (immutable) defining the seven allowed classification categories. Note that `"UNKNOWN"` is *not* in this set -- it is a sentinel value applied by the code when the LLM's confidence is too low, not a category the LLM is asked to choose.

**Design decision:** Using a `frozenset` signals to developers that this set is not meant to be modified at runtime. If you need to add a new document type, you must do it in the source code.

#### `ClassificationResult` Model (lines 33-40)

```python
class ClassificationResult(BaseModel):
    """Structured result from the document classification stage."""

    doc_type: str          # MSA | SOW | Amendment | Addendum | NDA | Order Form | Other
    parties: list[str]
    effective_date: str | None
    parent_reference_raw: str | None
    confidence: float
```

A Pydantic `BaseModel` that serves dual purpose:

1. **Output schema for Claude** -- this model class is passed as the `response_format` to `extract_with_structured_output()`, where it is converted to a tool definition that forces Claude to return JSON matching these fields via tool_use.
2. **Type-safe data container** -- the result flows through the rest of the pipeline as a validated Pydantic model with type checking.

Field-by-field:

| Field | Type | Purpose |
|---|---|---|
| `doc_type` | `str` | The classification category. Should be one of `VALID_DOC_TYPES` or `"UNKNOWN"`. |
| `parties` | `list[str]` | Names of all parties/signatories identified in the document. |
| `effective_date` | `str \| None` | Effective date in ISO-8601 format (`YYYY-MM-DD`), or `None` if not stated. |
| `parent_reference_raw` | `str \| None` | Raw text referencing a parent/prior agreement, or `None`. Critical for linking amendments to their parent contracts. |
| `confidence` | `float` | The LLM's self-assessed confidence (0.0 to 1.0). |

**Note on `doc_type`:** The field is typed as `str`, not as a `Literal` or `Enum`. This is intentional -- it allows the code to set it to `"UNKNOWN"` (which is outside `VALID_DOC_TYPES`) without Pydantic validation errors. The trade-off is that typos in doc types would not be caught at the model level.

#### The Classification System Prompt (lines 47-90)

```python
CLASSIFICATION_SYSTEM_PROMPT = """\
You are a contract classification assistant.  Given the text of a legal
document, you must determine what type of contract it is and extract key
metadata.
...
"""
```

This is a carefully crafted system prompt that instructs Claude on how to classify documents. It is worth reading in full because it directly controls classification accuracy. The prompt has two sections:

**Section 1: Document Types (lines 54-73)**

Each of the seven categories is defined with:
- A bold label (e.g., `**MSA**`).
- A full definition (e.g., "An overarching contract that establishes the general terms...").
- Distinguishing characteristics (e.g., "Subsequent SOWs, Order Forms, or Addenda typically reference an MSA").
- For Amendments specifically: key indicator phrases ("hereby amends", "modifies Section", "amended and restated").

**Design decision:** The prompt includes explicit examples of textual signals for Amendments (line 63-65). This is a form of few-shot prompting within the system prompt. The same phrases appear as regex patterns in the cross-check function, creating a two-layer safety net: the LLM looks for them during classification, and the regex catches any the LLM misses.

**Section 2: Extraction Rules (lines 76-89)**

Five numbered rules tell the LLM exactly what to extract and in what format:

1. `doc_type` -- one of the seven categories.
2. `parties` -- all signatories.
3. `effective_date` -- ISO-8601 format or null.
4. `parent_reference_raw` -- raw reference string or null.
5. `confidence` -- a float between 0.0 and 1.0.

The final instruction tells Claude to use the `structured_output` tool to return the result. This aligns with the `tool_choice` parameter in `extract_with_structured_output()`, which forces Claude to call the tool rather than respond with free-form text.

#### `classify_document()` (lines 98-155) -- The Primary Public Function

```python
def classify_document(
    text: str,
    claude_client=None,
) -> ClassificationResult:
```

The main classification entry point. Execution flow:

**1. Empty input guard (lines 120-128)**

```python
if not text or not text.strip():
    log.warning("empty_document_text")
    return ClassificationResult(
        doc_type="UNKNOWN",
        parties=[],
        effective_date=None,
        parent_reference_raw=None,
        confidence=0.0,
    )
```

Empty or whitespace-only text returns `UNKNOWN` immediately without making an API call. This is both a cost optimization (avoids an unnecessary Claude call) and a correctness measure (the LLM cannot classify nothing).

**2. LLM call (lines 130-137)**

```python
client = claude_client or get_anthropic_client()

result: ClassificationResult = extract_with_structured_output(
    client=client,
    system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
    user_prompt=text,
    response_format=ClassificationResult,
)
```

The entire document text is passed as the `user_prompt`. The `system_prompt` is the classification prompt defined above. The `response_format=ClassificationResult` is converted into a tool definition that forces Claude to return structured output matching the Pydantic model's schema.

**Design decision:** The full document text is sent as a single user message. For very long documents, this could exceed the model's context window. Currently, there is no truncation or chunking logic. If you encounter context-length errors, you may need to add a text truncation step here or use a model with a larger context window.

**3. Low-confidence guard (lines 146-153)**

```python
if result.confidence < 0.7:
    log.warning(
        "low_confidence_classification",
        doc_type=result.doc_type,
        confidence=result.confidence,
    )
    result = result.model_copy(update={"doc_type": "UNKNOWN"})
```

If the LLM reports less than 70% confidence, the `doc_type` is overridden to `"UNKNOWN"`. This is a safety mechanism -- the original classification and confidence are logged before the override, so the information is not lost.

**Key detail:** `result.model_copy(update={...})` is a Pydantic v2 method that creates a new model instance with specified fields changed. The original result is not mutated. This immutability pattern appears throughout the classification module.

**Threshold note:** The confidence threshold (0.7) is hardcoded. Unlike the OCR confidence thresholds in Stage 1, this is not defined as a module-level constant. If you need to tune it, you will need to find this line directly (line 147).

#### `classify_with_cross_check()` (lines 158-215) -- Rule-Based Safety Net

```python
def classify_with_cross_check(
    text: str,
    result: ClassificationResult,
) -> ClassificationResult:
```

This function applies deterministic, regex-based cross-checks to catch LLM misclassifications. It takes both the original text and the LLM's classification result as inputs.

**Amendment detection patterns (lines 185-189):**

```python
amendment_patterns = [
    re.compile(r"hereby\s+amends?", re.IGNORECASE),
    re.compile(r"modifies?\s+section", re.IGNORECASE),
    re.compile(r"amended\s+and\s+restated", re.IGNORECASE),
]
```

Three compiled regex patterns that detect amendment language:

1. `"hereby amends"` or `"hereby amend"` -- the classic amendment declaration.
2. `"modifies Section"` or `"modify Section"` -- language indicating specific sections are being changed.
3. `"amended and restated"` -- a common legal phrase for restated agreements.

All patterns are case-insensitive (`re.IGNORECASE`). The `\s+` allows for any whitespace between words.

**Cross-check 1: MSA misclassified as Amendment (lines 194-201)**

```python
if result.doc_type == "MSA" and text_signals_amendment:
    log.warning(
        "cross_check_reclassified",
        from_type="MSA",
        to_type="Amendment",
        reason="amendment_language_detected",
    )
    result = result.model_copy(update={"doc_type": "Amendment"})
```

If the LLM said "MSA" but the text contains amendment language, the classification is changed to "Amendment". This is the most common misclassification scenario -- an amendment to an MSA may have "Master Service Agreement" prominently in its title, causing the LLM to classify it as an MSA rather than an Amendment.

**Cross-check 2: Amendment without parent reference (lines 203-213)**

```python
if result.doc_type == "Amendment" and not result.parent_reference_raw:
    log.warning(
        "cross_check_suspicious_amendment",
        reason="amendment_without_parent_reference",
    )
    result = result.model_copy(
        update={"parent_reference_raw": "SUSPICIOUS: no parent reference found"},
    )
```

If the document is classified as an Amendment but has no `parent_reference_raw`, a sentinel value is injected: `"SUSPICIOUS: no parent reference found"`. This does NOT change the `doc_type` -- the document is still treated as an Amendment. But the sentinel value alerts downstream stages (and human reviewers) that something may be wrong.

**Design decision:** The cross-check function modifies `parent_reference_raw` rather than adding a separate `flags` list (unlike Stage 1's approach). This was a pragmatic choice -- the downstream Stage 3 already reads `parent_reference_raw` to link amendments to parent contracts, so a sentinel value in that field is naturally visible to the linking logic.

---

### Tests: `test_stage_2_classification.py`

**File:** `tests/e2e/test_stage_2_classification.py`

The test file provides thorough coverage of both the LLM classification path and the rule-based cross-check path. All LLM interactions are mocked.

#### Test Infrastructure (lines 24-42)

**`_make_mock_client()`** (lines 24-32): Returns the `ClassificationResult` directly. Despite the name, this function does not create a mock client object -- the actual patching is done by `_patch_extract()`.

**`_patch_extract()`** (lines 35-42):

```python
def _patch_extract(result: ClassificationResult):
    return patch(
        "echelonos.stages.stage_2_classification.extract_with_structured_output",
        return_value=result,
    )
```

Returns a context manager that patches `extract_with_structured_output` at the module level inside `stage_2_classification`. When the patched function is called, it returns the pre-built `ClassificationResult` regardless of input. This is the standard approach for testing LLM-dependent code -- you control exactly what the "LLM" returns and verify that the surrounding logic handles it correctly.

#### Sample Texts (lines 49-97)

Four realistic contract text samples are defined as module-level constants:

| Constant | Lines | Content |
|---|---|---|
| `MSA_TEXT` | 49-62 | A Master Service Agreement between Acme Corp and Globex Inc |
| `AMENDMENT_TEXT` | 64-72 | A First Amendment referencing the MSA, containing "hereby amends" and "modifies Section" |
| `SOW_TEXT` | 74-87 | A Statement of Work with deliverables, timeline, and pricing |
| `NDA_TEXT` | 89-97 | A Non-Disclosure Agreement between TechStart LLC and DataSafe Corp |

These texts are intentionally realistic and contain the key phrases that both the LLM and the cross-check patterns would look for.

#### TestClassifyDocument (lines 105-208)

Seven tests covering the `classify_document()` function:

**`test_classify_msa`** (line 108): Verifies that an MSA classification passes through correctly when confidence is high. Checks `doc_type`, confidence threshold, and party names.

**`test_classify_amendment`** (line 126): Verifies amendment classification including `parent_reference_raw` extraction. Checks that the parent reference contains the expected date.

**`test_low_confidence_becomes_unknown`** (line 143): The critical low-confidence test. When the LLM returns `confidence=0.5` with `doc_type="SOW"`, the function must override `doc_type` to `"UNKNOWN"` while preserving the original confidence value (0.5). This tests lines 147-153 of the source.

**`test_parties_extraction`** (line 160): Verifies that the parties list is correctly passed through from the LLM result.

**`test_effective_date_extraction`** (line 177): Verifies ISO-8601 date extraction.

**`test_empty_text_handling`** (line 192): Empty string input returns `UNKNOWN` with zero confidence, empty parties, and null dates -- all without making an API call. The mock client is passed but should never be called.

**`test_whitespace_only_text_handling`** (line 203): Whitespace-only input (`"   \n\t  "`) is treated identically to empty string. This tests the `not text.strip()` condition on line 120 of the source.

#### TestClassifyWithCrossCheck (lines 211-297)

Five tests covering the `classify_with_cross_check()` function:

**`test_cross_check_reclassifies_amendment`** (line 214): The core cross-check test. An MSA classification on `AMENDMENT_TEXT` (which contains "hereby amends") is reclassified to Amendment. Also verifies that parties and confidence are preserved through the reclassification.

**`test_cross_check_flags_amendment_without_parent`** (line 232): An Amendment classification with `parent_reference_raw=None` produces the `"SUSPICIOUS"` sentinel. Checks that `doc_type` remains "Amendment" (not changed) and that the sentinel string is present in `parent_reference_raw`.

**`test_cross_check_does_not_alter_correct_msa`** (line 249): Genuine MSA text with an MSA classification is left unchanged. This is a negative test -- it verifies that the cross-check does not produce false positives on text that does not contain amendment language.

**`test_cross_check_does_not_alter_amendment_with_parent`** (line 264): An Amendment classification with a valid `parent_reference_raw` is left unchanged -- the `"SUSPICIOUS"` sentinel is NOT injected. Checks that `"SUSPICIOUS" not in result.parent_reference_raw`.

**`test_cross_check_modifies_section_triggers_reclassification`** (line 280): Tests the second regex pattern (`"modifies Section"`). A custom text containing "modifies Section 3" triggers reclassification from MSA to Amendment, verifying that all three amendment patterns work, not just "hereby amends".

---

## Key Takeaways

1. **Classification is a two-layer process.** The LLM provides the initial classification, and regex-based cross-checks catch common misclassification patterns. This defense-in-depth approach is particularly valuable for amendments, which are the most commonly confused document type.

2. **Structured output eliminates parsing bugs.** By using Claude's tool_use feature with a Pydantic model converted to a tool schema, the pipeline guarantees that every LLM response is well-formed JSON matching the expected schema. There is no JSON parsing code, no regex extraction of fields from free-form text, and no error handling for malformed responses.

3. **Low-confidence results become UNKNOWN, not errors.** When the LLM is uncertain (confidence below 0.7), the document is classified as `UNKNOWN` rather than being rejected or errored. This is a graceful degradation strategy -- the document continues through the pipeline, and downstream stages or human reviewers can decide how to handle it.

4. **Immutability via `model_copy()`.** The code never mutates a `ClassificationResult` in place. Every modification creates a new instance via `model_copy(update={...})`. This makes the code easier to reason about and debug -- you can always inspect the original result in logs.

5. **The system prompt is the most important piece of the classification.** The 40-line prompt in `CLASSIFICATION_SYSTEM_PROMPT` defines how the LLM understands each document type, what metadata to extract, and how to format the output. Changes to the prompt directly affect classification accuracy. Treat it with the same care as production code.

6. **Cross-check patterns mirror the prompt's guidance.** The three regex patterns in `classify_with_cross_check()` match the same amendment indicators described in the system prompt (lines 63-65). This redundancy is intentional -- the LLM may miss textual signals, but the regex will not.

---

## Watch Out For

1. **The confidence threshold (0.7) is hardcoded on line 147.** Unlike Stage 1's confidence thresholds, which are module-level constants, the classification confidence threshold is embedded inline in the function body. If you need to tune it, search for `result.confidence < 0.7` in `stage_2_classification.py`. Consider extracting it to a named constant for consistency with Stage 1.

2. **No context-window overflow handling.** The full document text is passed to Claude as a single user message (line 134). Claude's context window is large enough for most contracts, but extremely long documents (hundreds of pages) could exceed this limit. Currently, there is no truncation, chunking, or error handling for context-length errors. If this becomes an issue, add a character/token limit check before the API call.

3. **`doc_type` is a `str`, not an enum.** The `ClassificationResult.doc_type` field accepts any string value. The code can (and does) set it to `"UNKNOWN"`, which is not in `VALID_DOC_TYPES`. If the LLM returns an unexpected value (e.g., `"Master Agreement"` instead of `"MSA"`), it will pass validation. Consider adding post-processing logic to normalize unexpected values.

4. **Cross-checks only run for MSA-to-Amendment reclassification.** The `classify_with_cross_check()` function currently only has two cross-checks, both related to amendments. There are no cross-checks for other misclassification scenarios (e.g., SOW misclassified as an Order Form, NDA misclassified as Other). If you see patterns of misclassification in production, this is the place to add new checks.

5. **The cross-check function must be called separately.** `classify_document()` does NOT call `classify_with_cross_check()` internally. The pipeline orchestrator is responsible for calling both functions in sequence. If you call only `classify_document()`, you will miss the cross-check safety net. Make sure your orchestration code calls both:

   ```python
   result = classify_document(text, claude_client=client)
   result = classify_with_cross_check(text, result)
   ```

6. **The "SUSPICIOUS" sentinel is a string, not a structured flag.** When an amendment has no parent reference, the cross-check sets `parent_reference_raw` to `"SUSPICIOUS: no parent reference found"` (lines 211-213). Downstream code that reads `parent_reference_raw` must check for this sentinel. Unlike Stage 1's structured `flags` list, this is a convention-based signal. If you add new sentinel values, document them clearly.

7. **Amendment patterns use `\s+` for whitespace.** The regex `r"hereby\s+amends?"` will match "hereby  amends" (double space) or "hereby\namends" (newline), but NOT "herebyamends" (no space). If OCR introduces whitespace artifacts, the patterns should still work. But if OCR removes whitespace entirely, they could miss matches.

8. **No retry logic on the Claude call.** Unlike Stage 1's `_call_mistral()` which has tenacity retry decorators, Stage 2's `classify_document()` does not retry on Anthropic API errors. If the Claude API returns a transient error (rate limit, timeout), it will propagate as an unhandled exception. Consider adding retry logic similar to Stage 1 if you experience reliability issues with the Claude API.

9. **The `VALID_DOC_TYPES` set is not enforced at classification time.** The frozenset on lines 22-30 is defined but never checked against the LLM's output. It exists as documentation and could be used for validation, but currently the code does not reject results where `doc_type` is not in `VALID_DOC_TYPES`. This is a deliberate choice to allow `"UNKNOWN"` through, but it means the LLM could theoretically return a novel type like `"Service Agreement"` and it would pass through unchecked.

10. **Test mocking patches the function, not the client.** The tests patch `extract_with_structured_output` at the module level rather than mocking the Anthropic client object. This means the tests do not exercise the client initialization path (`get_anthropic_client()`), the `client.messages.create()` call with tool_use, or the response parsing. These are covered by the Anthropic SDK's own tests, but be aware that integration issues at the SDK level would not be caught by these unit tests.
