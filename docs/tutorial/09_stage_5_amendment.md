# Stage 5: Amendment Chain Resolution

> **Linear ticket:** AKS-17

Stage 5 is the most algorithmically complex stage in the Echelonos pipeline. It
takes the obligations extracted in earlier stages and determines which ones are
still in force, which have been replaced, and which have been deleted -- all by
walking chronological amendment chains and using LLM-powered clause comparison.

---

## Table of Contents

1. [Overview and Purpose](#overview-and-purpose)
2. [File Layout](#file-layout)
3. [Pydantic Models](#pydantic-models)
4. [The System Prompt](#the-system-prompt)
5. [Chain Building: `build_amendment_chain()`](#chain-building-build_amendment_chain)
6. [Clause Comparison: `compare_clauses()`](#clause-comparison-compare_clauses)
7. [The Heuristic Pre-Filter: `_clauses_potentially_related()`](#the-heuristic-pre-filter-_clauses_potentially_related)
8. [The `obligation_type` Match Bypass](#the-obligation_type-match-bypass)
9. [Single Obligation Resolution: `resolve_obligation()`](#single-obligation-resolution-resolve_obligation)
10. [Chain Resolution: `resolve_amendment_chain()`](#chain-resolution-resolve_amendment_chain)
11. [Public Entry Point: `resolve_all()`](#public-entry-point-resolve_all)
12. [Status Transitions](#status-transitions)
13. [Test Walkthrough](#test-walkthrough)
14. [The 5-Call Mystery: Why Not 9?](#the-5-call-mystery-why-not-9)
15. [Key Takeaways](#key-takeaways)
16. [Watch Out For](#watch-out-for)

---

## Overview and Purpose

When a master service agreement (MSA) has been amended one or more times,
obligations extracted from the original MSA may no longer be valid. Amendment #1
might shorten a delivery deadline (REPLACE), adjust a payment term (MODIFY),
or explicitly remove an SLA clause (DELETE). Stage 5 resolves all of these
relationships by:

1. Building ordered chains of documents: MSA -> Amendment #1 -> Amendment #2 ...
2. Walking each chain chronologically.
3. Comparing every MSA obligation against every amendment obligation (with
   heuristic filtering to avoid unnecessary LLM calls).
4. Assigning a final status to each obligation: `ACTIVE`, `SUPERSEDED`,
   `TERMINATED`, or `UNRESOLVED`.

**Source file:** `src/echelonos/stages/stage_5_amendment.py` (551 lines)
**Test file:** `tests/e2e/test_stage_5_amendment.py` (969 lines)

---

## File Layout

```
src/echelonos/stages/stage_5_amendment.py
  Lines   1-16    Module docstring (resolution strategy overview)
  Lines  17-29    Imports and logger
  Lines  32-45    Pydantic model: ResolutionResult
  Lines  48-58    Pydantic model: _ComparisonResponse (internal)
  Lines  60-80    System prompt constant: _CLAUSE_COMPARISON_SYSTEM_PROMPT
  Lines  83-153   Chain building: build_amendment_chain()
  Lines 156-222   Clause comparison: compare_clauses()
  Lines 225-364   Single obligation resolution: resolve_obligation()
                   - _clauses_potentially_related() (lines 230-258)
                   - resolve_obligation() (lines 261-364)
  Lines 367-448   Chain resolution: resolve_amendment_chain()
                   - _get_doc_obligations() (lines 372-377)
                   - resolve_amendment_chain() (lines 380-448)
  Lines 451-551   Public API: resolve_all()
```

---

## Pydantic Models

### `ResolutionResult` (lines 37-44)

```python
class ResolutionResult(BaseModel):
    action: str          # "REPLACE" | "MODIFY" | "UNCHANGED" | "DELETE"
    original_clause: str
    amendment_clause: str
    reasoning: str
    confidence: float
```

This is the **public-facing** result of a single clause comparison. It contains
both the source texts and the LLM's determination. The `action` field drives all
downstream status transitions.

### `_ComparisonResponse` (lines 52-57)

```python
class _ComparisonResponse(BaseModel):
    action: str
    reasoning: str
    confidence: float
```

This is the **internal** model passed as `response_format` to
`extract_with_structured_output()` in `claude_client.py`, which converts it to
a tool definition for Claude's tool_use API. It intentionally omits the clause
texts since those are already known to the caller -- only the LLM's
determination (action, reasoning, confidence) needs to come back from the API.

**Design decision:** Keeping `_ComparisonResponse` separate from
`ResolutionResult` keeps the LLM response schema minimal. The caller then
enriches it with the clause texts before returning `ResolutionResult`.

---

## The System Prompt

Lines 64-80 define `_CLAUSE_COMPARISON_SYSTEM_PROMPT`. This is sent as the
`system` message in every LLM clause comparison call.

```python
_CLAUSE_COMPARISON_SYSTEM_PROMPT = (
    "Compare these two contract clauses. Does the amendment clause REPLACE, "
    "MODIFY, or leave UNCHANGED the original? If it explicitly deletes, say "
    "DELETE.\n\n"
    "Definitions:\n"
    "- REPLACE: The amendment clause entirely supersedes the original clause. ...\n"
    "- MODIFY: The amendment clause changes part of the original clause ...\n"
    "- UNCHANGED: The amendment clause does not affect the original clause.\n"
    "- DELETE: The amendment clause explicitly removes or voids the original ...\n\n"
    "Return your assessment as structured output with:\n"
    "- action: one of REPLACE, MODIFY, UNCHANGED, DELETE\n"
    "- reasoning: brief explanation of your determination\n"
    "- confidence: your confidence in this assessment (0.0-1.0)"
)
```

The prompt is carefully worded to:

- Constrain the LLM to exactly four actions.
- Provide unambiguous definitions for each action.
- Request structured output that maps 1:1 to `_ComparisonResponse`.

---

## Chain Building: `build_amendment_chain()`

**Location:** Lines 88-153

```python
def build_amendment_chain(doc_links: list[dict]) -> list[list[str]]:
```

### What it does

Given a flat list of link records (from Stage 4), it constructs one or more
ordered chains of document IDs, each starting at a root MSA and proceeding
through amendments chronologically.

### Step-by-step

1. **Filter** (line 111): Only `status == "LINKED"` records are used. Records
   with `UNLINKED` or `AMBIGUOUS` status are discarded.

2. **Build adjacency map** (lines 114-121): A `defaultdict(list)` maps each
   `parent_doc_id` to its list of `child_doc_id`s. A set `all_children` tracks
   every document that appears as a child.

3. **Find roots** (lines 123-129): Root documents are those that appear as
   parents but **never** as children. If no roots exist, the function logs a
   warning and returns an empty list.

4. **DFS walk** (lines 132-146): The inner function `_walk()` performs a
   depth-first search from each root:

   ```python
   def _walk(doc_id: str, current_chain: list[str]) -> None:
       current_chain.append(doc_id)
       kids = children_of.get(doc_id, [])
       if not kids:
           # Leaf node -- this chain is complete.
           chains.append(list(current_chain))
       else:
           for kid in kids:
               _walk(kid, current_chain)
       current_chain.pop()
   ```

   Key details:
   - `current_chain` is mutated in place (append/pop) for efficiency.
   - When a leaf is reached (no children), a **copy** of the chain is saved
     (`list(current_chain)`).
   - After exploring all children, the current node is popped -- standard DFS
     backtracking.
   - Roots are iterated in `sorted()` order (line 145) for deterministic output.

### Branching chains

If an MSA has two independent amendments (e.g., `amend-001` and `amend-002`
both linking to `msa-001`), the DFS produces **two separate chains**:

```
["msa-001", "amend-001"]
["msa-001", "amend-002"]
```

This means the same MSA's obligations may be resolved independently against
different branches. The test `test_branching_chains` (test file, lines 294-318)
verifies this behavior.

---

## Clause Comparison: `compare_clauses()`

**Location:** Lines 161-222

```python
def compare_clauses(
    original_clause: str,
    amendment_clause: str,
    claude_client: Any = None,
) -> ResolutionResult:
```

### What it does

Sends exactly two clauses to the LLM and gets back a structured determination.

### Key lines

- **Line 191:** Falls back to `get_anthropic_client()` if no client is injected.
  This is what enables test mocking -- tests always pass a mock client.

- **Lines 198-203:** The actual API call:
  ```python
  parsed: _ComparisonResponse = extract_with_structured_output(
      client=client,
      system_prompt=_CLAUSE_COMPARISON_SYSTEM_PROMPT,
      user_prompt=user_prompt,
      response_format=_ComparisonResponse,
  )
  ```
  This uses `extract_with_structured_output()` from `claude_client.py`, which
  converts the `_ComparisonResponse` Pydantic model into a Claude tool definition
  and forces Claude to call it via `tool_choice`, guaranteeing structured output.

- **Lines 205-211:** The parsed `_ComparisonResponse` is enriched into a full
  `ResolutionResult` by attaching the original and amendment clause texts.

**Design decision:** The function sees ONLY two clauses -- no broader document
context. This keeps the prompt focused and the token cost low, at the expense of
occasionally needing the heuristic pre-filter to avoid irrelevant comparisons.

---

## The Heuristic Pre-Filter: `_clauses_potentially_related()`

**Location:** Lines 230-258

```python
def _clauses_potentially_related(
    original_text: str,
    amendment_text: str,
) -> bool:
```

This is a cheap, **non-LLM** pre-filter that decides whether two obligation
texts are similar enough to warrant an expensive LLM comparison.

### Algorithm

1. **Stop-word removal** (lines 240-249): A hardcoded set of 30 common English
   stop words (`the`, `a`, `shall`, `must`, `may`, etc.) is removed from both
   texts.

2. **Tokenization** (lines 245-249): Both texts are split on whitespace and
   lowercased into sets of unique words.

3. **Overlap calculation** (lines 255-258):
   ```python
   overlap = orig_words & amend_words
   min_size = min(len(orig_words), len(amend_words))
   return len(overlap) / min_size >= 0.20
   ```
   If the intersection of keywords is at least **20% of the smaller set**, the
   clauses are considered potentially related.

### Why 20%?

This threshold is deliberately low. The goal is to **avoid obvious mismatches**
(e.g., a confidentiality clause vs. a delivery clause) while keeping false
negatives rare. A confidentiality obligation and a delivery amendment typically
share fewer than 20% of non-stop-word tokens.

### Edge case (line 252)

```python
if not orig_words or not amend_words:
    return False
```

If either text is empty after stop-word removal, the function returns `False` --
no comparison needed.

---

## The `obligation_type` Match Bypass

**Location:** Lines 308-312 (inside `resolve_obligation()`)

```python
same_type = (
    obligation.get("obligation_type")
    and obligation.get("obligation_type") == amend_obl.get("obligation_type")
)
if not same_type and not _clauses_potentially_related(orig_text, amend_text):
    continue
```

This is a critical piece of logic. Even if two obligations fail the keyword
heuristic (< 20% overlap), they will **still** be compared by the LLM if they
share the same `obligation_type` (e.g., both are `"SLA"` or both are
`"Delivery"`).

### Rationale

Consider the MSA SLA obligation:

> "Vendor must maintain 99.9% uptime for all hosted services."

And the amendment deletion:

> "Section 4.1 regarding uptime SLA is hereby deleted in its entirety."

These two texts share very few keywords after stop-word removal. The heuristic
would filter them out. But they are both typed as `"SLA"`, so the type bypass
ensures the LLM still compares them.

### The two-gate check

A comparison happens if **either** gate passes:

| Gate | Condition | Purpose |
|------|-----------|---------|
| Type match | `obligation_type` is non-empty and identical | Catches semantic matches that keywords miss |
| Keyword heuristic | >= 20% keyword overlap | Catches textual similarity regardless of type |

If **neither** gate passes, the amendment obligation is skipped (`continue`).

---

## Single Obligation Resolution: `resolve_obligation()`

**Location:** Lines 261-364

```python
def resolve_obligation(
    obligation: dict,
    amendment_obligations: list[dict],
    claude_client: Any = None,
) -> dict:
```

### What it does

Takes a single MSA obligation and walks it against all amendment obligations
(in chronological order), building up an `amendment_history` and determining
the final `status`.

### Step-by-step

1. **Initialize** (lines 296-297):
   ```python
   history: list[dict] = []
   current_status = "ACTIVE"
   ```

2. **Loop over amendments** (line 299): For each amendment obligation:

   a. **Early exit on TERMINATED** (lines 301-302):
      ```python
      if current_status == "TERMINATED":
          break
      ```
      Once an obligation is deleted, no further amendments can change it. This
      is tested by `test_delete_stops_further_processing` (test file, lines
      812-873).

   b. **Pre-filter check** (lines 304-313): Apply the heuristic + type bypass
      (described above). If neither gate passes, `continue` to the next
      amendment.

   c. **LLM comparison** (lines 316-320): Call `compare_clauses()` with the
      obligation's `source_clause` vs. the amendment's `source_clause`.

   d. **Record history** (lines 322-329): Every LLM comparison result is
      appended to the history list, regardless of action.

   e. **Status transitions** (lines 331-352):
      - `REPLACE` -> sets status to `"SUPERSEDED"` (but does NOT break the
        loop -- a superseded obligation can still be compared against later
        amendments, though in practice this rarely changes the outcome)
      - `DELETE` -> sets status to `"TERMINATED"` (and the loop will break on
        the next iteration due to the early exit check)
      - `MODIFY` -> status stays `"ACTIVE"` (the modification is recorded in
        history only)
      - `UNCHANGED` -> no status change

3. **Return enriched obligation** (lines 354-364): The original obligation
   dict is copied and augmented with `status` and `amendment_history`.

### Important: MODIFY does NOT change status

A `MODIFY` action means "the obligation still applies, but with changes." The
obligation stays `ACTIVE`. This is a deliberate design choice -- the audit trail
captures the modification via `amendment_history`, but the obligation itself
remains in force (partially). The test at line 644 verifies:

```python
assert payment["status"] == "ACTIVE"  # MODIFY keeps it active.
```

---

## Chain Resolution: `resolve_amendment_chain()`

**Location:** Lines 380-448

```python
def resolve_amendment_chain(
    chain_docs: list[dict],
    claude_client: Any = None,
) -> list[dict]:
```

### What it does

Resolves one full amendment chain (MSA + all its amendments).

### Step-by-step

1. **Identify the MSA** (line 411): The first document in `chain_docs` is the
   MSA.

2. **Collect amendment obligations** (lines 415-417):
   ```python
   amendment_obligations: list[dict] = []
   for amend_doc in chain_docs[1:]:
       amendment_obligations.extend(amend_doc.get("obligations", []))
   ```
   All amendment obligations are flattened into a single chronological list.
   This is important: a 3-document chain (MSA -> Amend1 -> Amend2) produces
   a single flat list where Amend1's obligations come before Amend2's.

3. **Resolve each MSA obligation** (lines 420-428): Each MSA obligation is
   compared against the full flattened amendment list via `resolve_obligation()`.

4. **Include amendment obligations as ACTIVE** (lines 432-439): Amendment
   obligations are always added to the output with `status = "ACTIVE"` and
   an empty `amendment_history`. The rationale: amendment obligations represent
   the latest version and are always in force.

5. **Return combined list** (lines 441-448): All MSA obligations (with resolved
   statuses) plus all amendment obligations (always ACTIVE).

### Why amendment obligations are always ACTIVE

An amendment obligation represents a **new or replacement** clause. Even if it
replaced an MSA obligation, the amendment itself is the current version. There
is no further amendment in the chain that supersedes it (unless there is, in
which case it would be the MSA in a subsequent chain level).

---

## Public Entry Point: `resolve_all()`

**Location:** Lines 456-551

```python
def resolve_all(
    documents: list[dict],
    links: list[dict],
    claude_client: Any = None,
) -> list[dict]:
```

This is the main entry point for Stage 5. It orchestrates everything.

### Step-by-step

1. **Build chains** (line 497):
   ```python
   chains = build_amendment_chain(links)
   ```

2. **Build document lookup** (lines 499-504): A dict mapping `doc_id` to
   document dict for fast access.

3. **Track linked documents** (lines 507-509): Collect all doc IDs that appear
   in any chain.

4. **Resolve each chain** (lines 514-528): For each chain, look up the actual
   document dicts, then call `resolve_amendment_chain()`.

5. **Handle unlinked documents** (lines 531-540): Documents not in any chain
   get their obligations marked as `"UNRESOLVED"`:
   ```python
   entry["status"] = "UNRESOLVED"
   entry["amendment_history"] = []
   entry["source_doc_id"] = doc_id
   ```
   This is a key design decision: rather than guessing, unlinked documents are
   explicitly flagged as unresolvable.

6. **Summary logging** (lines 542-549): Final counts of ACTIVE, SUPERSEDED,
   TERMINATED, and UNRESOLVED obligations.

---

## Status Transitions

The four possible statuses and how they are reached:

| Status | Meaning | Trigger |
|--------|---------|---------|
| `ACTIVE` | Obligation is currently in force | Default for MSA obligations; also set for MODIFY results and all amendment obligations |
| `SUPERSEDED` | Replaced by an amendment | LLM returns `REPLACE` |
| `TERMINATED` | Explicitly deleted | LLM returns `DELETE` |
| `UNRESOLVED` | Document not linked to any chain | Document not found in any chain built from links |

State machine:

```
ACTIVE ----(REPLACE)----> SUPERSEDED
ACTIVE ----(DELETE)-----> TERMINATED
ACTIVE ----(MODIFY)-----> ACTIVE (with history entry)
ACTIVE ----(UNCHANGED)--> ACTIVE (no change)
TERMINATED --(any)------> TERMINATED (early exit, no further processing)
```

Note that `SUPERSEDED` does NOT cause an early exit. A superseded obligation
continues to be compared against remaining amendments. This is a subtle design
choice: in theory a later amendment could DELETE a previously superseded
obligation, changing its status to TERMINATED.

---

## Test Walkthrough

**Test file:** `tests/e2e/test_stage_5_amendment.py`

### Test data (lines 60-189)

The test file defines realistic contract obligation data:

- **MSA obligations** (4 total):
  - `MSA_OBLIGATION_DELIVERY` -- 30-day delivery term
  - `MSA_OBLIGATION_PAYMENT` -- 45-day payment term
  - `MSA_OBLIGATION_CONFIDENTIALITY` -- 5-year confidentiality
  - `MSA_OBLIGATION_SLA` -- 99.9% uptime

- **Amendment #1 obligations** (3 total):
  - `AMENDMENT_1_DELIVERY` -- replaces delivery with 15 business days
  - `AMENDMENT_1_PAYMENT` -- modifies payment to 30 days
  - `AMENDMENT_1_NEW_CLAUSE` -- new 24/7 support obligation (unrelated to MSA)

- **Amendment #2 obligation** (1 total):
  - `AMENDMENT_2_SLA_DELETE` -- explicitly deletes the SLA section

### Mock helpers (lines 29-53)

Helpers build mock Claude responses that mirror the output of `extract_with_structured_output()` from `claude_client.py`. Since the tests patch `extract_with_structured_output` at the module level, the mocks return `_ComparisonResponse` Pydantic model instances directly, matching the function's return type.

### Key test classes

| Class | Lines | What it tests |
|-------|-------|---------------|
| `TestBuildAmendmentChain` | 197-243 | Simple chain building, UNLINKED filtering, empty input |
| `TestBuildChainWithMultipleAmendments` | 246-339 | 3-doc chains, 4-doc chains, branching, multiple MSAs |
| `TestCompareClausesReplace` | 347-378 | LLM REPLACE action |
| `TestCompareClausesModify` | 381-409 | LLM MODIFY action |
| `TestCompareClausesUnchanged` | 412-438 | LLM UNCHANGED action |
| `TestResolveObligationSuperseded` | 446-474 | Single obligation -> SUPERSEDED |
| `TestResolveObligationStaysActive` | 477-513 | Heuristic skip, empty amendments |
| `TestResolveChainEndToEnd` | 521-662 | **The big test** -- full chain with all four statuses |
| `TestUnlinkedDocsStayUnresolved` | 670-773 | UNRESOLVED handling |
| `TestDeleteDetection` | 781-873 | DELETE -> TERMINATED, early exit after DELETE |
| `TestResolveAllIntegration` | 881-968 | Full integration with mixed linked/unlinked docs |

---

## The 5-Call Mystery: Why Not 9?

This is the trickiest part to understand and is documented in the test's
comments at lines 535-549.

### The naive expectation

In `test_resolve_chain_end_to_end`, we have:
- 4 MSA obligations
- 3 amendment obligations (2 from Amend #1, 1 from Amend #2)

Naively, you would expect 4 x 3 = **12** LLM comparisons, or at least
4 x 3 = 12 minus some for early exits = **9** or so.

### What actually happens: only 5 LLM calls

The heuristic pre-filter and type bypass dramatically reduce the comparison
count. Let us trace through every MSA obligation against every amendment
obligation:

#### MSA Delivery (type="Delivery") vs. all 3 amendment obligations:

| Amendment | Type match? | Keyword heuristic? | Result |
|-----------|-------------|-------------------|--------|
| Amend1-Delivery (type="Delivery") | YES | -- | **LLM call #1** -> REPLACE |
| Amend1-Payment (type="Financial") | NO | YES (shares "vendor", "within", "days", etc.) | **LLM call #2** -> UNCHANGED |
| Amend2-SLA-Delete (type="SLA") | NO | NO (delivery vs SLA language) | **Skipped** |

After REPLACE, status becomes SUPERSEDED, but loop continues. After checking
the SLA delete (skipped by heuristic), the loop ends. **2 LLM calls** for
Delivery.

#### MSA Payment (type="Financial") vs. all 3 amendment obligations:

| Amendment | Type match? | Keyword heuristic? | Result |
|-----------|-------------|-------------------|--------|
| Amend1-Delivery (type="Delivery") | NO | YES (shares "vendor", "within", "days") | **LLM call #3** -> UNCHANGED |
| Amend1-Payment (type="Financial") | YES | -- | **LLM call #4** -> MODIFY |
| Amend2-SLA-Delete (type="SLA") | NO | NO (payment vs SLA) | **Skipped** |

**2 LLM calls** for Payment.

#### MSA Confidentiality (type="Confidentiality") vs. all 3 amendment obligations:

| Amendment | Type match? | Keyword heuristic? | Result |
|-----------|-------------|-------------------|--------|
| Amend1-Delivery (type="Delivery") | NO | NO (confidentiality vs delivery) | **Skipped** |
| Amend1-Payment (type="Financial") | NO | NO (confidentiality vs payment) | **Skipped** |
| Amend2-SLA-Delete (type="SLA") | NO | NO (confidentiality vs SLA) | **Skipped** |

**0 LLM calls** for Confidentiality. The heuristic filters out all three.

#### MSA SLA (type="SLA") vs. all 3 amendment obligations:

| Amendment | Type match? | Keyword heuristic? | Result |
|-----------|-------------|-------------------|--------|
| Amend1-Delivery (type="Delivery") | NO | NO (SLA vs delivery) | **Skipped** |
| Amend1-Payment (type="Financial") | NO | NO (SLA vs payment) | **Skipped** |
| Amend2-SLA-Delete (type="SLA") | YES | -- | **LLM call #5** -> DELETE |

**1 LLM call** for SLA.

### Total: 2 + 2 + 0 + 1 = **5 LLM calls**

This is why the test at lines 550-581 defines exactly 5 mock responses in order,
plus a safety buffer of 10 extra UNCHANGED responses (lines 584-591) in case the
heuristic threshold changes slightly.

### The mock ordering

The `side_effect` list in the test must match the exact call order:

```python
responses = [
    # 1. MSA Delivery vs AMEND_1_DELIVERY -> REPLACE (same type)
    # 2. MSA Delivery vs AMEND_1_PAYMENT -> UNCHANGED (heuristic)
    # 3. MSA Payment vs AMEND_1_DELIVERY -> UNCHANGED (heuristic)
    # 4. MSA Payment vs AMEND_1_PAYMENT -> MODIFY (same type)
    # 5. MSA SLA vs AMEND_2_SLA_DELETE -> DELETE (same type)
]
```

Getting this order wrong would cause the wrong actions to be applied to the
wrong obligations, producing incorrect statuses. This is the most fragile
part of the test and the most common source of confusion.

### Why the safety buffer?

```python
# Safety buffer in case the heuristic passes additional pairs.
for _ in range(10):
    responses.append(
        _make_comparison_response(
            action="UNCHANGED",
            reasoning="Different subject matter.",
            confidence=0.99,
        )
    )
```

If someone changes the stop-word list or the 20% threshold, additional pairs
might pass the heuristic. The buffer ensures the test does not crash with a
`StopIteration` error from the exhausted `side_effect` list. All buffer
responses return UNCHANGED, which is safe -- it will not change any statuses.

---

## Key Takeaways

1. **DFS produces correct chain ordering.** The backtracking DFS in
   `build_amendment_chain()` naturally produces chains from root to leaf.
   Branching MSAs produce multiple independent chains.

2. **The heuristic + type bypass is a cost optimization, not a correctness
   optimization.** It reduces LLM calls from O(n*m) to much fewer, but every
   skipped pair is genuinely unrelated (high confidence). The 20% keyword
   threshold is a pragmatic choice.

3. **MODIFY keeps obligations ACTIVE.** This is intentional -- a modified
   obligation is still in force, just with changes recorded in history.

4. **TERMINATED is a terminal state.** Once an obligation is deleted, the loop
   breaks early and no further amendments are considered.

5. **SUPERSEDED is NOT a terminal state.** A replaced obligation continues to
   be compared (though practically this rarely matters).

6. **Unlinked documents get UNRESOLVED status.** This is explicit and
   intentional -- the system does not guess about documents it cannot resolve.

7. **Amendment obligations are always ACTIVE.** They represent the latest
   version and are included in the output as-is.

8. **The LLM sees only two clauses.** No broader context is provided. This
   keeps prompts focused but means the heuristic pre-filter is essential for
   avoiding irrelevant comparisons.

---

## Watch Out For

1. **Mock ordering in tests.** The `side_effect` list must match the exact
   order of LLM calls, which depends on the iteration order of obligations
   AND the heuristic/type bypass logic. If you change the test data, the stop
   words, or the 20% threshold, you must re-derive the call order.

2. **The 20% threshold is hardcoded.** It lives at line 258. There is no
   configuration or per-contract tuning. If you encounter contracts with
   unusual vocabulary patterns, this threshold might need adjustment.

3. **Stop words are English-only.** The set at lines 240-244 only covers
   English. Contracts in other languages will pass the heuristic more
   frequently, increasing LLM costs.

4. **`obligation_type` must be exact-match.** The type bypass at line 310
   uses `==`. Types like `"SLA"` and `"Service Level Agreement"` would NOT
   match. Ensure upstream extraction normalizes types.

5. **Branching chains may resolve the same MSA obligations multiple times.**
   If MSA-001 has two amendments (Amend-A and Amend-B), the same MSA
   obligations appear in two separate chains and are resolved independently.
   This could produce duplicates in the output.

6. **`_get_doc_obligations()` is defined but not used.** The helper at lines
   372-377 exists for potential future use but is currently not called by any
   function. `resolve_amendment_chain()` directly accesses
   `doc.get("obligations", [])`.

7. **The function returns dicts, not Pydantic models.** Unlike Stage 6, the
   resolved obligations are plain dicts. This gives flexibility but means there
   is no schema validation on the output.

8. **`resolve_obligation()` copies the obligation dict shallowly** (line 354:
   `dict(obligation)`). If obligation values are mutable (e.g., nested lists),
   the copy and the original share references to those nested objects.
