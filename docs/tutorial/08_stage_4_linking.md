# Stage 4: Cross-Document Linking

**Linear ticket:** AKS-16

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture at a Glance](#architecture-at-a-glance)
3. [Source File Walkthrough: `stage_4_linking.py`](#source-file-walkthrough-stage_4_linkingpy)
   - [Module-Level Constants and Regex Patterns](#module-level-constants-and-regex-patterns)
   - [Reference Parsing: `parse_parent_reference()`](#reference-parsing-parse_parent_reference)
   - [Date Extraction Internals](#date-extraction-internals)
   - [Matching Helpers](#matching-helpers)
   - [Core Matching: `find_parent_document()`](#core-matching-find_parent_document)
   - [Batch Linking: `link_documents()`](#batch-linking-link_documents)
   - [Backfill: `backfill_dangling_references()`](#backfill-backfill_dangling_references)
4. [Test Suite Walkthrough: `test_stage_4_linking.py`](#test-suite-walkthrough-test_stage_4_linkingpy)
5. [Key Takeaways](#key-takeaways)
6. [Watch Out For](#watch-out-for)

---

## Overview

Stage 4 links child documents (Amendments, Addendums, SOWs) to their parent contracts. Unlike Stage 3, this stage uses **no LLMs and no database access** -- it is pure Python logic that accepts and returns plain dicts. The Prefect flow layer is responsible for all I/O.

The linking strategy is based on **parsing free-text reference strings** that appear in child documents (e.g., "MSA dated January 10, 2023" or "Agreement between CDW and Acme dated 2023-01-10") and matching their parsed components against candidate documents in the same organization.

Each linking attempt produces one of three outcomes:

| Status | Meaning |
|--------|---------|
| `LINKED` | Exactly one candidate matched -- parent identified |
| `UNLINKED` | Zero candidates matched -- dangling reference |
| `AMBIGUOUS` | Multiple candidates matched -- requires human review |

**Key files:**

| File | Path |
|------|------|
| Stage 4 logic | `src/echelonos/stages/stage_4_linking.py` |
| E2E tests | `tests/e2e/test_stage_4_linking.py` |

---

## Architecture at a Glance

```
Organization Document Corpus
        |
        v
  Filter: only LINKABLE_DOC_TYPES (Amendment, Addendum, SOW)
        |
        v
  For each child document with a parent_reference_raw:
        |
        v
  [1] parse_parent_reference()    -- regex-based parsing of reference string
        |                            extracts: doc_type, date, parties
        v
  [2] find_parent_document()      -- compare parsed components against all
        |                            org documents
        v
  Match count?
    0 matches  --> UNLINKED  (dangling reference)
    1 match    --> LINKED    (parent identified)
    2+ matches --> AMBIGUOUS (human review needed)

  ---

  When a new document arrives later:
  [3] backfill_dangling_references()  -- re-check all UNLINKED references
                                         against the new document
```

The entire pipeline is deterministic and has no randomness or LLM variability.

---

## Source File Walkthrough: `stage_4_linking.py`

**Full path:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/stages/stage_4_linking.py`

### Module-Level Constants and Regex Patterns

**Lines 29-56**

```python
LINKABLE_DOC_TYPES: set[str] = {"Amendment", "Addendum", "SOW"}
```

**Line 29:** Only these three document types are considered "child" documents that need parent linking. An MSA, NDA, or other contract type is never treated as a child -- it is always a potential parent. This is enforced in `link_documents()` on line 366.

**Lines 33-37:** `_DOC_TYPE_PATTERN` is a compiled regex for extracting the document type from the beginning of a reference string:

```python
_DOC_TYPE_PATTERN = re.compile(
    r"^(MSA|NDA|SOW|Master Services Agreement|Non-Disclosure Agreement|"
    r"Statement of Work|Order Form|Agreement|Contract)\b",
    re.IGNORECASE,
)
```

Key points:
- The pattern is **anchored to the start** (`^`) -- it only matches if the reference begins with a doc-type token.
- It supports both abbreviations (`MSA`) and full names (`Master Services Agreement`).
- The `\b` word boundary prevents partial matches (e.g., "MSAB" would not match).
- `re.IGNORECASE` allows any casing.

**Lines 40-43:** `_PARTIES_PATTERN` extracts party names from "between X and Y" constructions:

```python
_PARTIES_PATTERN = re.compile(
    r"\bbetween\s+(.+?)\s+and\s+(.+?)(?:\s+dated\b|\s+effective\b|$)",
    re.IGNORECASE,
)
```

This regex captures two groups:
- Group 1: everything after "between" and before "and" (non-greedy)
- Group 2: everything after "and" until "dated", "effective", or end-of-string

**Design note:** The non-greedy `(.+?)` is important. Without it, "between Acme Corp and Widget Inc and Beta LLC dated 2023-01-10" would capture "Acme Corp and Widget Inc" as the first party. With non-greedy matching, it correctly captures "Acme Corp" and "Widget Inc" (though "and Beta LLC dated 2023-01-10" would be the second group, which is then trimmed by the `(?:\s+dated\b|...)` boundary).

**Lines 46-56:** `_DOC_TYPE_ALIASES` maps raw parsed types to canonical forms:

```python
_DOC_TYPE_ALIASES: dict[str, str] = {
    "msa": "MSA",
    "master services agreement": "MSA",
    "nda": "NDA",
    "non-disclosure agreement": "NDA",
    "sow": "SOW",
    "statement of work": "SOW",
    "order form": "Order Form",
    "agreement": None,  # generic -- keep None
    "contract": None,
}
```

**Lines 54-55:** The words "Agreement" and "Contract" map to `None`. This is a deliberate decision: these are too generic to identify a specific document type. When a reference says "Agreement between X and Y dated...", we cannot determine whether it refers to an MSA, NDA, or something else. By mapping to `None`, the matching logic treats the type as a **wildcard** (see `_doc_type_matches()` on line 205).

### Reference Parsing: `parse_parent_reference()`

**Lines 64-106**

```python
def parse_parent_reference(reference_raw: str) -> dict[str, Any]:
```

This function takes a free-text reference string and returns a structured dict with three components:

```python
result: dict[str, Any] = {"doc_type": None, "date": None, "parties": []}
```

**Lines 84-85:** Empty/whitespace-only input returns the empty result immediately. This is a guard clause that prevents regex errors on blank strings.

**Lines 89-93:** Doc type extraction:

```python
m = _DOC_TYPE_PATTERN.match(text)
if m:
    raw_type = m.group(1).strip().lower()
    result["doc_type"] = _DOC_TYPE_ALIASES.get(raw_type, raw_type.upper())
```

The matched type is lowercased, then looked up in `_DOC_TYPE_ALIASES`. If not found in the alias map, it is uppercased as a fallback. This means any unrecognized type would be preserved in uppercase form (e.g., "Purchase Order" would become "PURCHASE ORDER").

**Lines 96-100:** Party extraction:

```python
pm = _PARTIES_PATTERN.search(text)
if pm:
    p1 = pm.group(1).strip().strip(",").strip()
    p2 = pm.group(2).strip().strip(",").strip()
    result["parties"] = [p1, p2]
```

**Lines 98-99:** The double `strip(",")` handles edge cases where party names have trailing commas (e.g., "between Acme Corp, and Widget Inc").

**Line 103:** Date extraction is delegated to `_extract_date()`.

### Date Extraction Internals

**Lines 109-156**

`_extract_date()` is the most complex parsing function in the module, using a multi-strategy approach:

**Strategy 1 (Lines 118-128): Keyword-based extraction.**

Looks for explicit `"dated <date>"` or `"effective <date>"` phrases:

```python
for keyword in ("dated", "effective"):
    pattern = re.compile(rf"\b{keyword}\s+(.+?)$", re.IGNORECASE)
    m = pattern.search(text)
    if m:
        candidate = m.group(1).strip()
        parsed = _try_parse_date(candidate)
        if parsed:
            return parsed
```

This is tried first because it is the most reliable -- when a reference explicitly says "dated January 10, 2023", the date is unambiguous.

**Strategy 2 (Lines 130-145): Regex pattern matching.**

If no keyword is found, falls back to scanning for common date patterns:

```python
date_patterns = [
    r"\b(\d{4}-\d{1,2}-\d{1,2})\b",       # ISO: 2023-01-10
    r"\b(\d{1,2}/\d{1,2}/\d{4})\b",        # US: 01/10/2023
    r"\b([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})\b",  # Long: January 10, 2023
]
```

Each pattern is tried in order. The ISO format is tried first because it is unambiguous.

**Lines 150-156:** `_try_parse_date()` is a safe wrapper around `dateutil.parser.parse()`:

```python
def _try_parse_date(text: str) -> str | None:
    try:
        dt = dateutil_parser.parse(text, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return None
```

The `fuzzy=True` parameter allows `dateutil` to ignore surrounding non-date text. The output is always normalized to `YYYY-MM-DD` format.

### Matching Helpers

**Lines 164-210**

These four helper functions power the comparison logic in `find_parent_document()`.

#### `_normalize()` (Lines 164-166)

```python
def _normalize(s: str) -> str:
    return " ".join(s.lower().split())
```

Lowercases and collapses all whitespace sequences into single spaces. This handles cases where one source has `"CDW  Government   LLC"` and another has `"cdw government llc"`.

#### `_parties_overlap()` (Lines 169-178)

```python
def _parties_overlap(child_parties: list[str], doc_parties: list[str]) -> bool:
    child_set = {_normalize(p) for p in child_parties}
    doc_set = {_normalize(p) for p in doc_parties}
    return bool(child_set & doc_set)
```

Uses **set intersection** to check if any party from the reference matches any party in the candidate document. This is deliberately generous -- a single overlapping party is enough. The rationale is that references may not list all parties (e.g., "MSA between CDW and..." might omit the second party due to OCR truncation).

**Line 174:** Returns `False` if either party list is empty. This prevents vacuous matches.

#### `_dates_match()` (Lines 181-194)

```python
def _dates_match(parsed_date: str | None, doc_effective_date: str | None) -> bool:
    if parsed_date is None or doc_effective_date is None:
        return False
    try:
        d1 = dateutil_parser.parse(parsed_date).date()
        d2 = dateutil_parser.parse(str(doc_effective_date)).date()
        return d1 == d2
    except (ValueError, OverflowError, TypeError):
        return False
```

Compares dates at **day granularity** using `dateutil` parsing for robustness. The `str()` wrapper on line 191 handles cases where `doc_effective_date` might be a `datetime.date` object rather than a string.

**Line 188:** If either date is `None`, returns `False` immediately. This means documents without effective dates can never be matched by date.

#### `_doc_type_matches()` (Lines 197-210)

```python
def _doc_type_matches(parsed_type: str | None, doc_type: str | None) -> bool:
    if parsed_type is None:
        return True  # Generic reference -- cannot filter by type.
    if doc_type is None:
        return False
    return _normalize(parsed_type) == _normalize(doc_type)
```

**Line 205:** When `parsed_type is None` (the reference said "Agreement" or "Contract"), the function returns `True` for **any** document type. This is the wildcard behavior mentioned earlier.

**Line 208-209:** When the candidate document has no type (`doc_type is None`), it cannot match a specific parsed type, so returns `False`.

### Core Matching: `find_parent_document()`

**Lines 218-316**

This is the heart of Stage 4. It takes a child document and a list of organization documents, and returns a linking result.

```python
def find_parent_document(
    child_doc: dict[str, Any],
    org_documents: list[dict[str, Any]],
) -> dict[str, Any]:
```

**Lines 249-250:** Parse the reference string:

```python
parsed = parse_parent_reference(raw_ref)
candidates: list[dict[str, Any]] = []
```

**Lines 252-282:** The matching loop iterates over all org documents:

```python
for doc in org_documents:
    if doc.get("id") == child_id:
        continue  # Skip the child itself

    type_ok = _doc_type_matches(parsed["doc_type"], doc.get("doc_type"))
    date_ok = _dates_match(parsed["date"], doc.get("effective_date"))
    parties_ok = _parties_overlap(parsed["parties"], doc.get("parties") or [])

    if not date_ok:
        continue  # Date is the PRIMARY signal -- must match
```

**Line 266:** Date match is **required** -- it is the primary filtering criterion. No document can be a candidate without a date match. This is a strong design decision: in the legal domain, the effective date is the most reliable identifier for a specific contract.

**Lines 269-282:** Refinement logic when additional signals are available:

```python
if parsed["doc_type"] and parsed["parties"]:
    if type_ok or parties_ok:
        candidates.append(doc)
elif parsed["doc_type"]:
    if type_ok:
        candidates.append(doc)
elif parsed["parties"]:
    if parties_ok:
        candidates.append(doc)
else:
    candidates.append(doc)  # Only date was parsed
```

The logic is a prioritized filter:

| Parsed fields available | Requirement to match |
|-------------------------|---------------------|
| doc_type + parties | Date AND (type OR parties) |
| doc_type only | Date AND type |
| parties only | Date AND parties |
| neither (date only) | Date only |

**Design note:** When both type and parties are available, the logic uses **OR** rather than **AND** (line 272). This is deliberately lenient -- if the reference says "MSA between CDW and Acme dated 2023-01-10", a document matching the date and type (but with different parties) is still a valid candidate. This prevents false negatives from OCR-garbled party names.

**Lines 284-316:** Status determination based on candidate count:

```python
if len(candidates) == 1:
    return {"status": "LINKED", "parent_doc_id": candidates[0]["id"], ...}
elif len(candidates) == 0:
    return {"status": "UNLINKED", "parent_doc_id": None, ...}
else:
    return {"status": "AMBIGUOUS", "parent_doc_id": None, ...}
```

**Lines 319-326:** `_candidate_summary()` builds a slim dict for each candidate, including only `id`, `doc_type`, `effective_date`, and `parties`. This keeps the output manageable when there are multiple ambiguous candidates.

### Batch Linking: `link_documents()`

**Lines 334-381**

```python
def link_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
```

This is the batch entry point. It processes all documents in a single organization (or multiple organizations).

**Lines 354-357:** Documents are grouped by `org_id`:

```python
orgs: dict[str, list[dict[str, Any]]] = {}
for doc in documents:
    org_id = doc.get("org_id", "default")
    orgs.setdefault(str(org_id), []).append(doc)
```

**Design note:** Grouping by `org_id` ensures that a child document in Organization A cannot be linked to a parent in Organization B. This is a fundamental data isolation requirement.

**Lines 361-372:** The inner loop filters and processes:

```python
for doc in org_docs:
    doc_type = doc.get("doc_type", "")
    ref_raw = doc.get("parent_reference_raw")

    if doc_type not in LINKABLE_DOC_TYPES:
        continue
    if not ref_raw or not ref_raw.strip():
        continue

    result = find_parent_document(doc, org_docs)
    results.append(result)
```

**Line 366:** Only `Amendment`, `Addendum`, and `SOW` documents are processed. An MSA with a `parent_reference_raw` field would be silently skipped.

**Line 368:** Documents without a reference string are also skipped.

**Lines 374-381:** Summary logging with counts of linked, unlinked, and ambiguous results.

### Backfill: `backfill_dangling_references()`

**Lines 389-471**

```python
def backfill_dangling_references(
    new_doc: dict[str, Any],
    dangling_refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
```

This function addresses a specific real-world scenario: **late-arriving parent documents**. Consider this sequence:

1. An Amendment is ingested that references "MSA dated January 10, 2023"
2. The MSA has not been ingested yet
3. The Amendment is marked UNLINKED (dangling reference)
4. Later, the MSA is ingested
5. `backfill_dangling_references()` re-checks all dangling references against the new MSA

**Lines 425-464:** The function iterates over all dangling references and applies the same matching logic used in `find_parent_document()`:

```python
for ref in dangling_refs:
    raw_text = ref.get("reference_text", "")
    parsed = parse_parent_reference(raw_text)

    type_ok = _doc_type_matches(parsed["doc_type"], new_doc.get("doc_type"))
    date_ok = _dates_match(parsed["date"], new_doc.get("effective_date"))
    parties_ok = _parties_overlap(parsed["parties"], new_doc.get("parties") or [])

    # Same matching logic as find_parent_document...
    matched = False
    if date_ok:
        if parsed["doc_type"] and parsed["parties"]:
            matched = type_ok or parties_ok
        elif parsed["doc_type"]:
            matched = type_ok
        elif parsed["parties"]:
            matched = parties_ok
        else:
            matched = True
```

**Lines 450-464:** When a match is found, a resolution record is produced:

```python
resolved.append({
    "dangling_ref_id": ref.get("id"),
    "child_doc_id": ref.get("doc_id"),
    "parent_doc_id": new_doc.get("id"),
    "status": "LINKED",
})
```

**Design note:** The matching logic on lines 439-448 is duplicated from `find_parent_document()` (lines 269-282). This is a conscious tradeoff: extracting it into a shared function would reduce duplication but would also introduce coupling between two functions with slightly different input shapes (`child_doc` vs. `dangling_ref`). The current approach keeps each function self-contained and easy to reason about.

---

## Test Suite Walkthrough: `test_stage_4_linking.py`

**Full path:** `/Users/shangchienliu/Github-local/echelonos/tests/e2e/test_stage_4_linking.py`

Since Stage 4 is pure Python with no LLM or database dependencies, the tests are fully self-contained with inline test data. No mocking is needed.

### Helper: `_doc()` (Lines 25-41)

```python
def _doc(
    doc_type: str = "MSA",
    effective_date: str | None = "2023-01-10",
    parties: list[str] | None = None,
    parent_reference_raw: str | None = None,
    org_id: str = "org-1",
    doc_id: str | None = None,
) -> dict:
```

Builds a minimal document dict with sensible defaults. If no `doc_id` is provided, a random UUID is generated (line 35). All tests in the same org use `"org-1"` by default.

### `TestParseReferenceWithDateAndType` (Lines 49-57)

Tests the basic case: `"MSA dated January 10, 2023"` should parse into `doc_type="MSA"`, `date="2023-01-10"`, `parties=[]`.

### `TestParseReferenceMultipleFormats` (Lines 60-102)

**Lines 63-76:** Parametrized test for five different date formats:

| Input | Expected |
|-------|----------|
| `"MSA dated January 10, 2023"` | `2023-01-10` |
| `"MSA dated 01/10/2023"` | `2023-01-10` |
| `"MSA dated 2023-01-10"` | `2023-01-10` |
| `"NDA dated March 5, 2024"` | `2024-03-05` |
| `"SOW dated 12/25/2022"` | `2022-12-25` |

**Lines 77-84:** `test_parse_with_parties_and_generic_type` -- Tests that `"Agreement"` maps to `None` (generic type) and that parties are correctly extracted from `"Agreement between CDW and Acme dated 2023-01-10"`.

**Lines 86-91:** `test_parse_master_services_agreement_long_form` -- Confirms that `"Master Services Agreement"` is aliased to `"MSA"`.

**Lines 93-97:** `test_parse_empty_string` -- Guards against empty input.

**Lines 99-102:** `test_parse_no_date` -- A reference like `"MSA between Acme and Beta"` (no date) should still extract the doc type and parties, with `date=None`.

### `TestSingleMatchLinked` (Lines 110-133)

Sets up a parent MSA with `effective_date="2023-01-10"` and a child Amendment referencing `"MSA dated January 10, 2023"`. Since dates match and the type matches, the result is `LINKED` with `parent_doc_id` pointing to the parent.

### `TestNoMatchUnlinked` (Lines 141-163)

The parent has `effective_date="2022-06-15"` but the child references `"MSA dated January 10, 2023"`. The dates do not match, so the result is `UNLINKED` with empty candidates.

### `TestMultipleMatchesAmbiguous` (Lines 171-201)

Two parents exist with the same date and type (`MSA`, `2023-01-10`) but different parties. Since the child reference does not include party information (`"MSA dated January 10, 2023"`), both parents match, producing `AMBIGUOUS` with two candidates.

### `TestLinkDocumentsBatch` (Lines 209-246)

Tests the batch processing of multiple documents:

- `parent_msa` -- MSA dated 2023-01-10
- `amendment` -- references "MSA dated January 10, 2023" (matches)
- `sow` -- references "MSA dated 2023-01-10" (matches)
- `addendum` -- references "MSA dated March 15, 2025" (no match)

**Lines 238-246:** Asserts:
- 3 linkable documents are processed (the MSA itself is not linkable)
- Amendment and SOW are LINKED
- Addendum is UNLINKED (wrong date)

### `TestOnlyLinkableTypesProcessed` (Lines 254-271)

Creates four non-linkable documents (MSA, NDA, Other, Order Form) and gives each a `parent_reference_raw`. Asserts that `link_documents()` returns 0 results -- none of these types should be processed regardless of whether they have a parent reference.

### `TestBackfillResolvesDangling` (Lines 279-304)

Creates a new MSA and a dangling reference from an amendment. The backfill function correctly links them.

**Key assertions (lines 301-304):**

```python
assert resolved[0]["dangling_ref_id"] == "dang-001"
assert resolved[0]["child_doc_id"] == "amend-001"
assert resolved[0]["parent_doc_id"] == "msa-new"
assert resolved[0]["status"] == "LINKED"
```

### `TestBackfillNoMatch` (Lines 312-338)

A new NDA dated 2024-06-01 does not match any existing dangling references (which reference an MSA from 2023). Returns an empty list.

### `TestPartyOverlapMatching` (Lines 346-409)

Three subtests for party matching:

**Lines 349-365:** `test_party_overlap_matching` -- Both parties match exactly between reference and document.

**Lines 367-385:** `test_party_case_insensitive` -- Parent has lowercase parties (`"cdw government llc"`), reference has mixed case (`"CDW Government LLC"`). Still matches due to `_normalize()`.

**Lines 387-409:** `test_single_party_overlap_suffices` -- Reference mentions "CDW Government LLC" and "SomeOther", but the parent has "CDW Government LLC" and "Acme Corp". The overlap on "CDW Government LLC" is sufficient for a match.

---

## Key Takeaways

1. **Date is the primary matching signal.** Without a date match, a candidate is immediately excluded (line 266). This is because effective dates are typically the most reliable and unambiguous identifiers in legal document references. Type and party information serve as secondary refinement signals.

2. **The module is entirely LLM-free.** Stage 4 uses only regex and `dateutil` for parsing. This makes it fast, deterministic, and testable without any API mocking. The design assumes that the `parent_reference_raw` field was already extracted from the document (by Stage 3 or during ingestion).

3. **All functions are pure.** They accept plain dicts and return plain dicts. No database access, no file I/O, no global state. This makes the functions trivially testable and composable.

4. **The three-way outcome (LINKED/UNLINKED/AMBIGUOUS) is a deliberate design pattern.** Rather than forcing a best-guess match, the system explicitly identifies cases where human review is needed. This is critical for legal accuracy -- a wrong link could cause an obligation to be attributed to the wrong contract.

5. **Backfill handles document ingestion order.** In real-world scenarios, documents are not always ingested in chronological order. A child Amendment may arrive before its parent MSA. The `backfill_dangling_references()` function ensures that these late arrivals are automatically resolved.

6. **Organization isolation is enforced.** Documents are grouped by `org_id` (line 354-357), preventing cross-organization matching. This is a fundamental multi-tenancy requirement.

7. **Generic type references are treated as wildcards.** "Agreement" and "Contract" map to `None` in `_DOC_TYPE_ALIASES`, which causes `_doc_type_matches()` to return `True` for any document type. This is pragmatic -- generic references are common in legal documents and should not prevent matching.

---

## Watch Out For

1. **The matching logic is duplicated between `find_parent_document()` (lines 269-282) and `backfill_dangling_references()` (lines 439-448).** If you modify the matching criteria in one place, you must mirror the change in the other. Consider extracting a shared `_matches_reference()` function if this duplication becomes a maintenance burden.

2. **Date parsing assumes US locale.** The regex pattern `\b(\d{1,2}/\d{1,2}/\d{4})\b` (line 136) treats `01/10/2023` as January 10, not October 1. This is because `dateutil.parser.parse()` defaults to US date ordering (MM/DD/YYYY). If your contracts use DD/MM/YYYY formatting, you will get incorrect matches. Consider passing `dayfirst=True` to `dateutil_parser.parse()` if needed.

3. **The `_PARTIES_PATTERN` regex only handles "between X and Y" syntax.** References that use other party-naming patterns (e.g., "MSA with CDW Government LLC dated...") will not extract party information. The parties list will be empty, and matching will rely solely on date and type.

4. **`_parties_overlap()` uses set intersection, not subset/superset checking.** A single overlapping party name is sufficient for a match. This can produce false positives when two unrelated contracts happen to share one party (e.g., two different MSAs with CDW as a party but different counterparties). When this happens, the result will be AMBIGUOUS (multiple matches), which triggers human review -- so the false positive is caught, but it does create extra review work.

5. **The `LINKABLE_DOC_TYPES` set is hardcoded on line 29.** If your organization uses document types not in this set (e.g., "Change Order", "Extension"), they will be silently skipped by `link_documents()`. You must add them to this set.

6. **Empty `parent_reference_raw` is silently skipped (line 368).** If a child document should have a parent reference but the field is empty (e.g., due to an extraction failure in a prior stage), it will not appear in the results at all -- no UNLINKED entry is created. This means "missing reference" and "unresolvable reference" are indistinguishable in the output.

7. **No confidence scoring on links.** Unlike Stage 3, which produces confidence scores for extractions, Stage 4 returns only a binary LINKED/UNLINKED/AMBIGUOUS status. There is no indication of how strong the match is (e.g., "matched on date + type + parties" vs. "matched on date only"). Consider adding a `match_strength` field if downstream consumers need this information.

8. **The `_extract_date()` fallback patterns (lines 132-145) try ISO format first.** If a reference contains multiple dates in different formats, the first match wins. For example, `"MSA 2022-01-01 dated January 10, 2023"` would extract `2023-01-10` from the "dated" keyword (Strategy 1), not `2022-01-01`. But `"MSA 2022-01-01 and 2023-01-10"` (no keyword) would extract `2022-01-01` because the ISO pattern is tried first in Strategy 2.

9. **`_try_parse_date()` uses `fuzzy=True` (line 153).** This means `dateutil` will try to extract a date from strings that contain non-date text. For example, `"approximately 30 days after 2023-01-10"` would parse as `2023-01-10`. This is usually desirable, but in edge cases it might produce surprising results from text that was not intended as a date reference.
