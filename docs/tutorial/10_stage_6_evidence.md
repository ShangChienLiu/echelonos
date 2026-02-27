# Stage 6: Append-Only Evidence Trails

> **Linear ticket:** AKS-18

Stage 6 is the final stage of the Echelonos extraction pipeline. It packages
every resolved obligation into an immutable, tamper-resistant evidence record
that traces the obligation back to its source document, extraction model,
verification result, and amendment history. The guiding principle is simple:
**never update, never delete -- only append.**

---

## Table of Contents

1. [Overview and Purpose](#overview-and-purpose)
2. [File Layout](#file-layout)
3. [Core Design Principles](#core-design-principles)
4. [The `VerificationResult` Enum](#the-verificationresult-enum)
5. [The `EvidenceRecord` Model (Frozen Pydantic)](#the-evidencerecord-model-frozen-pydantic)
6. [Verification Resolution: `_resolve_verification_result()`](#verification-resolution-_resolve_verification_result)
7. [Creating a Single Record: `create_evidence_record()`](#creating-a-single-record-create_evidence_record)
8. [Batch Packaging: `package_evidence()`](#batch-packaging-package_evidence)
9. [Status Change Records: `create_status_change_record()`](#status-change-records-create_status_change_record)
10. [Chain Validation: `validate_evidence_chain()`](#chain-validation-validate_evidence_chain)
11. [Stronger Validation: `validate_evidence_chain_against_obligations()`](#stronger-validation-validate_evidence_chain_against_obligations)
12. [Test Walkthrough](#test-walkthrough)
13. [Key Takeaways](#key-takeaways)
14. [Watch Out For](#watch-out-for)

---

## Overview and Purpose

After Stage 5 has resolved which obligations are active, superseded, or
terminated, Stage 6 creates the **audit trail**. In regulated environments
(legal, compliance, financial), it is not enough to know the final status of an
obligation -- you must also prove:

- Which document the obligation came from (provenance).
- Which AI model extracted it (reproducibility).
- Whether a second model verified it (cross-validation).
- How amendments changed it over time (lineage).
- That none of this evidence has been tampered with (integrity).

Stage 6 addresses all five concerns through frozen Pydantic models, append-only
semantics, and chain validation.

**Source file:** `src/echelonos/stages/stage_6_evidence.py` (381 lines)
**Test file:** `tests/e2e/test_stage_6_evidence.py` (464 lines)

---

## File Layout

```
src/echelonos/stages/stage_6_evidence.py
  Lines   1-7     Module docstring
  Lines   8-16    Imports and logger
  Lines  19-61    Pydantic models: VerificationResult enum, EvidenceRecord
  Lines  64-86    Helper: _resolve_verification_result()
  Lines  89-142   Public API: create_evidence_record()
  Lines 145-210   Public API: package_evidence()
  Lines 213-271   Public API: create_status_change_record()
  Lines 274-334   Public API: validate_evidence_chain()
  Lines 337-381   Public API: validate_evidence_chain_against_obligations()
```

---

## Core Design Principles

### 1. Immutability

Every `EvidenceRecord` is a **frozen** Pydantic model. Once created, no field
can be changed. Attempting to set an attribute raises a `ValidationError`. This
is enforced at the language level by Pydantic's `frozen=True` configuration.

### 2. Append-Only

When an obligation's status changes (e.g., from ACTIVE to SUPERSEDED), the
system does NOT update the existing evidence record. Instead, it creates a
**new** record that captures the transition. The old record remains untouched.
This means the evidence list only grows -- it never shrinks or mutates.

### 3. Provenance

Every record links back to:
- The source document (`doc_id`, `doc_filename`, `page_number`, `section_reference`)
- The exact source clause text
- The extraction model that found it
- The verification model that checked it
- The amendment history that affected it

### 4. Validation

Two validation functions check that:
- Amendment histories have the required keys (internal consistency).
- Every expected obligation ID has at least one evidence record (completeness).

---

## The `VerificationResult` Enum

**Location:** Lines 24-29

```python
class VerificationResult(str, Enum):
    CONFIRMED = "CONFIRMED"
    DISPUTED = "DISPUTED"
    UNVERIFIED = "UNVERIFIED"
```

This enum inherits from both `str` and `Enum`. The `str` inheritance means
each member's `.value` is a plain string, which is important because
`EvidenceRecord.verification_result` is typed as `str` (not as the enum
itself). The enum serves as the canonical set of allowed values.

The three states represent:

| Value | Meaning |
|-------|---------|
| `CONFIRMED` | A second model verified the obligation exists in the source document |
| `DISPUTED` | A second model could NOT confirm the obligation |
| `UNVERIFIED` | No verification was performed (or the result is ambiguous) |

---

## The `EvidenceRecord` Model (Frozen Pydantic)

**Location:** Lines 32-61

```python
class EvidenceRecord(BaseModel, frozen=True):
    obligation_id: str
    doc_id: str
    doc_filename: str
    page_number: int | None = None
    section_reference: str | None = None
    source_clause: str
    extraction_model: str
    verification_model: str
    verification_result: str = Field(
        description="CONFIRMED | DISPUTED | UNVERIFIED",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    amendment_history: list[dict] | None = None
```

### Why `frozen=True`?

The `frozen=True` parameter on line 32 tells Pydantic to make the model
immutable. Under the hood, Pydantic overrides `__setattr__` and `__delattr__`
to raise `ValidationError` on any mutation attempt. This is the foundation of
the append-only guarantee: you literally cannot change a record after creation.

The test at lines 261-276 of the test file verifies this:

```python
def test_evidence_immutability(self) -> None:
    record = create_evidence_record(...)

    with pytest.raises(ValidationError):
        record.confidence = 0.50
    with pytest.raises(ValidationError):
        record.verification_result = "DISPUTED"
    with pytest.raises(ValidationError):
        record.source_clause = "tampered"
```

### Field-by-field breakdown

| Field | Type | Purpose |
|-------|------|---------|
| `obligation_id` | `str` | Unique identifier from extraction (Stage 2) |
| `doc_id` | `str` | Source document ID |
| `doc_filename` | `str` | Human-readable filename (e.g., `"services_agreement_v2.pdf"`) |
| `page_number` | `int \| None` | Page where the clause was found; `None` if unknown |
| `section_reference` | `str \| None` | Section label (e.g., `"Article 1.1"`); `None` if not available |
| `source_clause` | `str` | The exact clause text from the document |
| `extraction_model` | `str` | Model that extracted the obligation (e.g., `"gpt-4o-2025-04-01"`) |
| `verification_model` | `str` | Model that verified the obligation (e.g., `"claude-sonnet-4-20250514"`) |
| `verification_result` | `str` | One of `CONFIRMED`, `DISPUTED`, `UNVERIFIED` |
| `confidence` | `float` | Confidence score, constrained to `[0.0, 1.0]` by `Field(ge=0.0, le=1.0)` |
| `amendment_history` | `list[dict] \| None` | Chronological list of amendment records, or `None` |

### The `@model_validator` (lines 53-61)

```python
@model_validator(mode="after")
def _validate_verification_result(self) -> "EvidenceRecord":
    allowed = {v.value for v in VerificationResult}
    if self.verification_result not in allowed:
        raise ValueError(
            f"verification_result must be one of {allowed}, "
            f"got {self.verification_result!r}"
        )
    return self
```

This validator runs **after** all field validators. It checks that
`verification_result` is one of the three allowed enum values. Since the field
is typed as `str` (not as the enum), Pydantic will not reject arbitrary strings
at the field level -- the model validator provides the constraint.

**Design decision:** Using `str` instead of the enum type gives flexibility for
serialization (no need to handle enum encoding) while the validator still
enforces correctness. The test at lines 425-437 confirms invalid values are
rejected:

```python
def test_invalid_verification_result_rejected(self) -> None:
    with pytest.raises(ValidationError, match="verification_result"):
        EvidenceRecord(
            ...,
            verification_result="MAYBE",  # Not a valid value.
            ...
        )
```

### Confidence bounds

The `confidence` field uses `Field(ge=0.0, le=1.0)` to enforce bounds. The test
at lines 439-463 confirms that values outside `[0.0, 1.0]` (like `1.5` or
`-0.1`) raise `ValidationError`.

---

## Verification Resolution: `_resolve_verification_result()`

**Location:** Lines 69-86

```python
def _resolve_verification_result(verification: dict) -> str:
```

This helper normalizes the various ways a verification dict might represent its
outcome into one of the three canonical strings.

### Resolution priority

1. **Explicit `result` string** (lines 77-79): If the dict has a `result` key
   that is already a valid `VerificationResult` value, use it directly:
   ```python
   explicit = verification.get("result")
   if explicit and explicit in {v.value for v in VerificationResult}:
       return explicit
   ```

2. **Boolean `verified` flag** (lines 81-85):
   - `True` -> `"CONFIRMED"`
   - `False` -> `"DISPUTED"`

3. **Default** (line 86): If neither key is present (or `verified` is `None`),
   return `"UNVERIFIED"`.

### Precedence matters

The test at lines 409-423 shows a tricky case: when both `result` and
`verified` are present but **contradictory**, the explicit `result` wins:

```python
verification = {
    "verification_model": "claude-sonnet-4-20250514",
    "result": "DISPUTED",
    "verified": True,  # Contradictory but 'result' takes precedence.
    "confidence": 0.40,
}
record = create_evidence_record(...)
assert record.verification_result == "DISPUTED"
```

This is intentional: the `result` key is considered more authoritative because
it is an explicit string, while `verified` is a boolean that may have been set
by a different process.

---

## Creating a Single Record: `create_evidence_record()`

**Location:** Lines 94-142

```python
def create_evidence_record(
    obligation: dict,
    document: dict,
    verification: dict,
    amendment_history: list[dict] | None = None,
) -> EvidenceRecord:
```

### What it does

Assembles an `EvidenceRecord` from three data sources:

1. **obligation dict** -- provides `obligation_id`, `source_clause`,
   `extraction_model`, `source_page`, `section_reference`, `confidence`.
2. **document dict** -- provides `doc_id`, `filename`.
3. **verification dict** -- provides `verification_model`, plus `verified`
   and/or `result`.
4. **amendment_history** (optional) -- list of amendment dicts.

### Key lines

- **Line 120:** Verification resolution happens first:
  ```python
  verification_result = _resolve_verification_result(verification)
  ```

- **Line 132:** Confidence comes from verification first, falling back to the
  obligation's confidence, and finally defaulting to `0.0`:
  ```python
  confidence=verification.get("confidence", obligation.get("confidence", 0.0)),
  ```
  This means the verification model's confidence takes precedence over the
  extraction model's confidence. The rationale: if a verifier checked the
  obligation, its confidence assessment is more recent and relevant.

- **Lines 136-141:** Structured logging records the creation event with key
  identifiers.

---

## Batch Packaging: `package_evidence()`

**Location:** Lines 145-210

```python
def package_evidence(
    obligations: list[dict],
    documents: dict[str, dict],
    verifications: dict[str, dict],
    amendment_chains: dict[str, list[dict]] | None = None,
) -> list[EvidenceRecord]:
```

### What it does

Processes a batch of obligations and produces one `EvidenceRecord` per
obligation. This is the typical entry point for production use.

### Parameter design

Note the lookup-dict pattern:
- `documents` is keyed by `doc_id`.
- `verifications` is keyed by `obligation_id`.
- `amendment_chains` is keyed by `obligation_id`.

This avoids O(n^2) lookups that would occur with list-based matching.

### Graceful skipping (lines 177-192)

If a document or verification is missing for a given obligation, the function
logs a warning and skips that obligation:

```python
document = documents.get(doc_id)
if document is None:
    log.warning("evidence_missing_document", obligation_id=ob_id, doc_id=doc_id)
    continue

verification = verifications.get(ob_id)
if verification is None:
    log.warning("evidence_missing_verification", obligation_id=ob_id)
    continue
```

This is a deliberate design choice: partial evidence is better than crashing.
The final log message (lines 204-209) reports how many obligations were skipped,
enabling monitoring.

The tests at lines 163-182 and 184-192 verify both skip scenarios:

```python
def test_package_evidence_skips_missing_document(self) -> None:
    records = package_evidence(obligations=[obligation_orphan], documents={}, ...)
    assert len(records) == 0

def test_package_evidence_skips_missing_verification(self) -> None:
    records = package_evidence(obligations=[...], ..., verifications={})
    assert len(records) == 0
```

---

## Status Change Records: `create_status_change_record()`

**Location:** Lines 213-271

```python
def create_status_change_record(
    obligation_id: str,
    old_status: str,
    new_status: str,
    reason: str,
    changed_by_doc_id: str | None = None,
) -> EvidenceRecord:
```

### What it does

This is the function that enforces the append-only pattern for status
transitions. Rather than mutating an existing `EvidenceRecord`, it creates a
**brand new** record that captures what changed.

### How it works

The function creates an `EvidenceRecord` with special sentinel values:

```python
record = EvidenceRecord(
    obligation_id=obligation_id,
    doc_id=changed_by_doc_id or "SYSTEM",        # Who triggered the change
    doc_filename="status_change",                  # Sentinel filename
    page_number=None,
    section_reference=None,
    source_clause=f"Status changed from {old_status} to {new_status}: {reason}",
    extraction_model="SYSTEM",                     # Not extracted by AI
    verification_model="SYSTEM",                   # Not verified by AI
    verification_result=VerificationResult.UNVERIFIED.value,
    confidence=1.0,                                # System-generated = certain
    amendment_history=[
        {
            "old_status": old_status,
            "new_status": new_status,
            "reason": reason,
            "changed_by_doc_id": changed_by_doc_id,
        }
    ],
)
```

### Key design decisions

1. **`doc_id` defaults to `"SYSTEM"`** (line 245): When no document triggered
   the change (e.g., a contract expired by date), the system itself is the
   actor.

2. **`doc_filename` is `"status_change"`** (line 246): This is a sentinel value
   that clearly distinguishes status-change records from extraction records.

3. **`extraction_model` and `verification_model` are `"SYSTEM"`** (lines
   248-249): These records are not AI-generated.

4. **`confidence` is `1.0`** (line 251): A system-generated status change is
   certain -- there is no probabilistic element.

5. **`verification_result` is `UNVERIFIED`** (line 250): A status change is a
   fact, not something that needs external verification.

6. **`source_clause` is human-readable** (line 249): The clause field is
   repurposed to hold a readable description of the transition:
   `"Status changed from ACTIVE to SUPERSEDED: Amendment doc-bbb extends delivery to 45 days."`

### Append-only demonstrated in tests

The test at lines 278-297 shows both records existing simultaneously:

```python
def test_status_change_produces_new_record(self) -> None:
    original = create_evidence_record(...)
    transition = create_status_change_record(...)

    # Two separate record objects -- the original is not mutated.
    assert original is not transition
    assert original.verification_result == "CONFIRMED"
    assert transition.verification_result == "UNVERIFIED"
    assert original.source_clause != transition.source_clause
```

The original record is never touched. Both records coexist in the evidence
trail, forming a complete history.

---

## Chain Validation: `validate_evidence_chain()`

**Location:** Lines 274-334

```python
def validate_evidence_chain(records: list[EvidenceRecord]) -> dict:
```

### What it does

Checks the internal consistency of a set of evidence records. Returns a dict
with:

```python
{
    "valid": bool,
    "missing_evidence": list[str],  # obligation IDs with no records
    "gaps": list[str],              # descriptions of amendment-history issues
}
```

### Checks performed

1. **Amendment history integrity** (lines 303-311): For each record that has
   an `amendment_history`, each entry is checked for the required keys:
   `{"doc_id", "clause", "status"}`. Missing keys are reported as gaps:

   ```python
   for idx, entry in enumerate(record.amendment_history):
       required_keys = {"doc_id", "clause", "status"}
       missing_keys = required_keys - set(entry.keys())
       if missing_keys:
           gaps.append(
               f"obligation {record.obligation_id}: amendment_history[{idx}] "
               f"missing keys {missing_keys}"
           )
   ```

2. **Self-consistency** (lines 313-320): The function collects all
   `obligation_id`s from the records. In its current form, it does not compare
   against an external list -- that is the job of the stronger variant
   (`validate_evidence_chain_against_obligations`). The `missing_evidence` list
   is always empty in this variant.

### Validation result

The chain is `valid` only if there are **zero gaps and zero missing evidence**:

```python
result = {
    "valid": len(gaps) == 0 and len(missing_evidence) == 0,
    "missing_evidence": missing_evidence,
    "gaps": gaps,
}
```

The test at lines 346-366 demonstrates gap detection:

```python
bad_amendment_history = [
    {"doc_id": "doc-aaa", "clause": "Original clause", "status": "ACTIVE"},
    {"doc_id": "doc-bbb"},  # Missing 'clause' and 'status'.
]
record = create_evidence_record(..., amendment_history=bad_amendment_history)
result = validate_evidence_chain([record])

assert result["valid"] is False
assert len(result["gaps"]) == 1
assert "amendment_history[1]" in result["gaps"][0]
```

---

## Stronger Validation: `validate_evidence_chain_against_obligations()`

**Location:** Lines 337-381

```python
def validate_evidence_chain_against_obligations(
    records: list[EvidenceRecord],
    expected_obligation_ids: list[str],
) -> dict:
```

### What it does

Extends `validate_evidence_chain()` by also checking that every expected
obligation ID has at least one evidence record.

### Key lines

```python
base_result = validate_evidence_chain(records)

covered = {r.obligation_id for r in records}
missing = [oid for oid in expected_obligation_ids if oid not in covered]

valid = base_result["valid"] and len(missing) == 0
```

The function first runs the base validation (amendment history checks), then
computes which expected obligation IDs are NOT covered by any record.

### When to use which

| Function | Use case |
|----------|----------|
| `validate_evidence_chain()` | Quick internal consistency check; no external context needed |
| `validate_evidence_chain_against_obligations()` | Full validation against a known set of obligation IDs from the extraction pipeline |

The test at lines 328-344 shows the stronger variant catching a missing
obligation:

```python
result = validate_evidence_chain_against_obligations(
    records=[record_for_ob_001],
    expected_obligation_ids=["ob-001", "ob-002"],
)
assert result["valid"] is False
assert "ob-002" in result["missing_evidence"]
```

---

## Test Walkthrough

**Test file:** `tests/e2e/test_stage_6_evidence.py`

### Test data (lines 26-75)

Shared test data is defined at module level:

- `SAMPLE_OBLIGATION` -- a delivery obligation with all required fields.
- `SAMPLE_DOCUMENT` -- minimal document dict with `doc_id` and `filename`.
- `SAMPLE_VERIFICATION_CONFIRMED` -- verification with `verified: True`.
- `SAMPLE_VERIFICATION_DISPUTED` -- verification with `verified: False`.
- `SAMPLE_VERIFICATION_UNVERIFIED` -- verification with no `verified` key.
- `SAMPLE_AMENDMENT_HISTORY` -- two-entry amendment history.

Note that **no mocking is required** for Stage 6 tests. All functions are pure
-- they take dicts in and produce `EvidenceRecord` objects out. There are no LLM
calls, no database queries, no network I/O.

### Test classes

| Class | Lines | What it tests |
|-------|-------|---------------|
| `TestCreateEvidenceRecord` | 83-125 | Basic record creation, amendment history inclusion |
| `TestPackageEvidence` | 128-209 | Batch packaging, missing document skip, missing verification skip, amendment chains |
| `TestStatusChangeRecord` | 212-255 | Status transition records, system-triggered changes |
| `TestEvidenceImmutability` | 258-297 | Frozen model enforcement, append-only pattern |
| `TestValidateEvidenceChain` | 300-374 | Valid chain, missing evidence detection, amendment history gaps, empty records |
| `TestVerificationResultTypes` | 377-463 | All three verification outcomes, explicit result precedence, invalid values, confidence bounds |

### Notable test patterns

**1. Immutability test (lines 261-276):** Uses `pytest.raises(ValidationError)`
to prove frozen models reject mutation. This is a contract test -- if Pydantic's
frozen behavior ever changes, this test will catch it.

**2. Append-only pattern test (lines 278-297):** Creates an original record and
a status-change record, then asserts they are distinct objects (`is not`) with
different field values. This proves the append-only pattern works at the object
level.

**3. Precedence test (lines 409-423):** The contradictory verification test
(explicit `result: "DISPUTED"` with `verified: True`) documents the
resolution priority order. This test serves as executable documentation.

**4. Bounds test (lines 439-463):** Two separate `pytest.raises` blocks confirm
both upper and lower bounds on `confidence`. This is a classic boundary-value
test.

---

## Key Takeaways

1. **Frozen Pydantic models are the foundation of immutability.** The
   `frozen=True` parameter on `EvidenceRecord` (line 32) makes mutation
   impossible at the language level. This is stronger than a convention -- it is
   enforced by Pydantic's `__setattr__` override.

2. **Append-only is a pattern, not a database feature.** Stage 6 does not
   interact with a database. The append-only semantic is enforced by the API
   design: `create_status_change_record()` always returns a NEW record rather
   than providing an update method.

3. **Graceful degradation over hard failures.** `package_evidence()` skips
   obligations with missing documents or verifications rather than crashing.
   This ensures partial evidence is produced even when upstream stages have gaps.

4. **Verification confidence takes precedence.** When building the evidence
   record (line 132), the verification dict's confidence is preferred over the
   obligation's confidence. This reflects the temporal order: verification
   happens after extraction.

5. **No external dependencies.** Stage 6 has zero external calls -- no LLM,
   no database, no network. All functions are pure. This makes it the easiest
   stage to test and the most reliable in production.

6. **Status-change records use sentinels.** The `"SYSTEM"` values for
   `doc_id`, `extraction_model`, and `verification_model` are conventions,
   not enforced by the schema. Consumers of evidence records should filter
   by `doc_filename == "status_change"` to identify transition records.

7. **Validation is two-tiered.** The basic `validate_evidence_chain()` checks
   internal consistency. The stronger
   `validate_evidence_chain_against_obligations()` additionally checks
   completeness against a known set of obligation IDs. Use the stronger
   variant in production pipelines.

---

## Watch Out For

1. **Amendment history schema is not validated at creation time.** The
   `amendment_history` field is typed as `list[dict] | None` with no schema
   on the inner dicts. Validation only happens in `validate_evidence_chain()`,
   which checks for `{"doc_id", "clause", "status"}` keys. If you add new
   required keys, update the validation function at line 305.

2. **Status-change records reuse `EvidenceRecord`.** The
   `create_status_change_record()` function creates a regular `EvidenceRecord`
   with sentinel values. There is no separate model for status changes. This
   means consumers must check `doc_filename == "status_change"` or
   `extraction_model == "SYSTEM"` to distinguish them.

3. **`_resolve_verification_result()` is not defensive against invalid `result`
   strings.** If `verification.get("result")` returns a string that is NOT in
   the `VerificationResult` enum, the function falls through to check `verified`
   (lines 81-86). However, if `result` is a non-empty string that is not a
   valid enum value, it will be silently ignored. The `model_validator` on
   `EvidenceRecord` will catch this later, but the error message may be
   confusing.

4. **The `confidence` fallback chain.** Line 132:
   ```python
   confidence=verification.get("confidence", obligation.get("confidence", 0.0))
   ```
   If both `verification` and `obligation` lack a `confidence` key, the default
   is `0.0`. This silent default could mask data quality issues. Consider
   whether a missing confidence should be flagged.

5. **Hash-chaining is not yet implemented.** The module docstring and design
   principles reference tamper detection, but the current implementation relies
   on Pydantic's frozen models for integrity. True cryptographic hash-chaining
   (where each record includes the hash of the previous record) is not present.
   The `validate_evidence_chain()` function checks structural integrity, not
   cryptographic integrity.

6. **Thread safety of the append-only list.** The `package_evidence()` function
   returns a `list[EvidenceRecord]`. If multiple threads or processes append to
   a shared evidence list, standard Python list operations are not atomic.
   Consider using a thread-safe collection or a database-backed append-only log
   in production.

7. **Shallow dict references in `amendment_history`.** The `EvidenceRecord` is
   frozen, but the dicts inside `amendment_history` are regular mutable dicts.
   While Pydantic prevents reassigning `record.amendment_history`, a caller
   could theoretically mutate a dict inside the list (e.g.,
   `record.amendment_history[0]["status"] = "HACKED"`). For true deep
   immutability, consider freezing the inner dicts or using frozen dataclasses.
