# Stage 3: Obligation Extraction & Dual Ensemble Verification

**Linear ticket:** AKS-15

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture at a Glance](#architecture-at-a-glance)
3. [Source File Walkthrough: `stage_3_extraction.py`](#source-file-walkthrough-stage_3_extractionpy)
   - [Pydantic Models & the Obligation Schema](#pydantic-models--the-obligation-schema)
   - [Party-Role Extraction](#party-role-extraction)
   - [Obligation Extraction (Primary)](#obligation-extraction-primary)
   - [Obligation Extraction (Independent)](#obligation-extraction-independent)
   - [Matching: Pair Obligations from Both Extractions](#matching-pair-obligations-from-both-extractions)
   - [Agreement Check](#agreement-check)
   - [Verification Layer 1: Grounding Check](#verification-layer-1-grounding-check)
   - [Verification Layer 2: Chain-of-Verification (CoVe)](#verification-layer-2-chain-of-verification-cove)
   - [The Orchestrator: `extract_and_verify()`](#the-orchestrator-extract_and_verify)
4. [LLM Client Walkthrough](#llm-client-walkthrough)
   - [`claude_client.py`](#claude_clientpy)
5. [Test Suite Walkthrough: `test_stage_3_extraction.py`](#test-suite-walkthrough-test_stage_3_extractionpy)
6. [Key Takeaways](#key-takeaways)
7. [Watch Out For](#watch-out-for)

---

## Overview

Stage 3 is the core intelligence layer of the Echelonos pipeline. It takes raw contract text (produced by Stages 1-2) and extracts every contractual obligation from it, then subjects each extraction to a **multi-layer verification pipeline** built around a dual independent extraction ensemble:

1. **Dual independent extraction** -- two separate Claude calls with different prompt framings ("obligations" vs "binding commitments") extract obligations independently, avoiding anchoring bias.
2. **Programmatic matching** -- obligations from both runs are paired by source_clause similarity using `difflib.SequenceMatcher` (ratio > 0.7).
3. **Agreement check** -- matched pairs are compared on obligation_type, responsible_party, and obligation_text similarity.
4. **Grounding check** -- a mechanical substring match confirms the cited source clause actually exists in the original document.
5. **Chain-of-Verification (CoVe)** -- for DISAGREED or SOLO extractions (not matched or not in agreement), Claude generates verification questions, re-reads the document to answer them, and compares the answers against the original extraction.

The design philosophy is **ensemble consensus**: two independently-prompted extractions are more reliable than one extraction verified after the fact. An obligation is marked VERIFIED only when both extractions agree and the cited source clause is grounded, or when a disputed/solo extraction passes both grounding and CoVe arbitration.

**Key files:**

| File | Path |
|------|------|
| Stage 3 logic | `src/echelonos/stages/stage_3_extraction.py` |
| Claude client | `src/echelonos/llm/claude_client.py` |
| E2E tests | `tests/e2e/test_stage_3_extraction.py` |
| Configuration | `src/echelonos/config.py` |

---

## Architecture at a Glance

```
Raw Contract Text
        |
        v
  [1] extract_party_roles()              -- Claude identifies Vendor, Client, etc.
        |
        v
  [2a] extract_obligations()             -- Claude primary extraction ("obligations")
  [2b] extract_obligations_independent() -- Claude independent extraction ("binding commitments")
        |
        v
  [3] match_extractions()                -- pair obligations by source_clause similarity (>0.7)
        |
        v
  For each pair:
    [4a] check_agreement()               -- same type, party, and text similarity?
    [4b] verify_grounding()              -- substring match (pure Python, no LLM)
    [4c] run_cove()                      -- CoVe, only for DISAGREED or SOLO
        |
        v
  Decision logic:
    AGREED + grounded          --> VERIFIED
    DISAGREED/SOLO + grounded + CoVe passed --> VERIFIED
    Otherwise                  --> UNVERIFIED
```

The key insight is that two independent extractions reaching the same conclusion (AGREED) is strong evidence the extraction is correct. Disputed or solo extractions require additional verification (grounding + CoVe) to earn VERIFIED status.

---

## Source File Walkthrough: `stage_3_extraction.py`

**Full path:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/stages/stage_3_extraction.py`

### Pydantic Models & the Obligation Schema

**Lines 37-68**

The module starts by defining the canonical obligation schema as Pydantic models.

```python
OBLIGATION_TYPES: list[str] = [
    "Delivery",
    "Financial",
    "Compliance",
    "SLA",
    "Confidentiality",
    "Termination",
    "Indemnification",
    "Governance",
]
```

**Line 37-46:** `OBLIGATION_TYPES` is a closed enum of eight obligation categories. This list is injected into both extraction prompts (lines 148 and 217) so Claude knows exactly which types are valid. This is a critical design decision -- constraining the type vocabulary prevents the LLM from inventing ad-hoc categories that downstream stages would not recognize.

```python
class Obligation(BaseModel):
    obligation_text: str
    obligation_type: str  # one of OBLIGATION_TYPES
    responsible_party: str
    counterparty: str
    frequency: str | None = None
    deadline: str | None = None
    source_clause: str
    source_page: int
    confidence: float
```

**Lines 49-60:** The `Obligation` model captures everything needed to represent a single contractual obligation:

| Field | Purpose |
|-------|---------|
| `obligation_text` | A concise human-readable summary of the obligation |
| `obligation_type` | One of the eight types from `OBLIGATION_TYPES` |
| `responsible_party` | The party that must fulfill the obligation (uses role labels like "Vendor") |
| `counterparty` | The party that benefits from the obligation |
| `frequency` | How often the obligation recurs (e.g., "Quarterly"), or `None` |
| `deadline` | When the obligation must be fulfilled, or `None` |
| `source_clause` | **The exact verbatim text** from the contract that establishes this obligation |
| `source_page` | Page number where the clause appears (1-indexed) |
| `confidence` | The LLM's self-assessed confidence score (0.0 to 1.0) |

**Design note on `source_clause`:** This field is intentionally required to be a verbatim copy. The extraction prompt (line 145-146) explicitly instructs "copy it character-for-character". This is what makes the grounding check possible -- if the LLM fabricates or paraphrases, the substring match will fail.

**Lines 63-67:** `ExtractionResult` bundles the list of obligations with the party role mapping:

```python
class ExtractionResult(BaseModel):
    obligations: list[Obligation]
    party_roles: dict[str, str]  # e.g. {"Vendor": "CDW Government LLC"}
```

### Party-Role Extraction

**Lines 74-123**

Before extracting obligations, the pipeline first identifies who the parties are and what roles they play.

**Lines 75-85:** The system prompt (`_PARTY_ROLES_SYSTEM_PROMPT`) instructs Claude to look for patterns like `"CDW Government LLC, hereinafter 'Vendor'"`. It asks for a JSON object mapping role names to full legal entity names.

**Lines 88-91:** `_PartyRolesResponse` is a thin Pydantic wrapper for structured output parsing:

```python
class _PartyRolesResponse(BaseModel):
    party_roles: dict[str, str]
```

**Lines 94-123:** The `extract_party_roles()` function:

```python
def extract_party_roles(
    text: str,
    claude_client=None,
) -> dict[str, str]:
```

- **Line 113:** Uses dependency injection for the Claude client -- `claude_client or get_anthropic_client()`. This pattern appears throughout Stage 3 and is essential for testability. Tests pass mock clients; production code uses the real client.
- **Lines 114-119:** Calls `extract_with_structured_output()` from `claude_client.py` with `response_format=_PartyRolesResponse`. This helper converts the Pydantic model into a tool schema, forces Claude to call it via `tool_choice`, and parses the result back into a typed Pydantic instance. No manual JSON parsing needed.

**Why extract party roles first?** The roles are injected into both obligation extraction prompts so Claude uses consistent role labels (e.g., "Vendor" and "Client") rather than varying between "CDW Government LLC", "the Vendor", "CDW", etc. This normalization is critical for downstream processing and for the agreement check to work correctly.

### Obligation Extraction (Primary)

**Lines 130-192**

**Lines 130-148:** The primary extraction system prompt (`_EXTRACTION_SYSTEM_PROMPT`) is the first of two extraction prompts. Key instructions include:

- Extract **every** contractual obligation (not just obvious ones)
- Use one of the eight predefined `OBLIGATION_TYPES`
- Use role labels for `responsible_party` and `counterparty`
- Copy `source_clause` **character-for-character** (lines 143-144)
- Include a `confidence` score from 0.0 to 1.0

**Line 148:** Note the prompt construction:

```python
).format(types=", ".join(OBLIGATION_TYPES), roles="{roles_placeholder}")
```

This pre-fills the type list at module load time but leaves `{roles_placeholder}` as a literal string to be replaced per-call. This two-phase template approach keeps the type list consistent across all invocations while allowing per-document party roles.

**Lines 157-192:** The `extract_obligations()` function:

- **Line 179:** Formats the party roles into a readable string:
  ```python
  roles_str = "\n".join(f"  {role}: {entity}" for role, entity in party_roles.items())
  ```
- **Line 180:** Replaces the placeholder with the actual roles.
- **Lines 183-188:** Calls Claude structured output via `extract_with_structured_output()` with `_ExtractionResponse` as the format.
- **Line 192:** Returns an `ExtractionResult` bundling obligations and roles together.

### Obligation Extraction (Independent)

**Lines 199-258**

This is the second half of the dual extraction ensemble. The independent extraction uses a **deliberately different prompt framing** to avoid anchoring bias.

**Lines 199-217:** The independent extraction system prompt (`_INDEPENDENT_EXTRACTION_SYSTEM_PROMPT`) reframes the task:

- Instead of "obligations", it asks for **"binding commitments, duties, and requirements"**
- Instead of "contract obligation extractor", the role is **"legal contract reviewer"**
- The structured fields are the same, but described differently (e.g., "commitment" vs "obligation")

This wording difference is intentional. If both prompts used identical language, the two extractions would be more likely to produce the same errors. By varying the framing, the pipeline gets genuinely independent signals.

**Lines 220-258:** The `extract_obligations_independent()` function mirrors `extract_obligations()` exactly in structure -- same Pydantic response format, same role injection, same Claude structured output call -- but uses the independent prompt. Crucially, it does **not** receive the primary extraction results. Each extraction sees only the raw document text and party roles.

```python
def extract_obligations_independent(
    text: str,
    party_roles: dict[str, str],
    claude_client=None,
) -> ExtractionResult:
```

### Matching: Pair Obligations from Both Extractions

**Lines 266-317**

After both extractions complete, the pipeline must determine which obligations from the primary run correspond to which obligations from the independent run.

```python
def match_extractions(
    primary: list[Obligation],
    independent: list[Obligation],
    threshold: float = 0.7,
) -> list[tuple[Obligation, Obligation | None]]:
```

**Algorithm (lines 289-317):**

1. For each primary obligation, find the best-matching independent obligation by `source_clause` similarity using `difflib.SequenceMatcher`.
2. If the best match has a ratio >= 0.7 (the `threshold`), pair them together.
3. If no match meets the threshold, the primary obligation is paired with `None` (a SOLO extraction).
4. After all primary obligations are matched, any unmatched independent obligations are also added as SOLO entries (with `None` in the second position).

**Why `source_clause` similarity?** The source clause is the most stable anchor between two extractions. Even if the two prompts produce different `obligation_text` summaries, they should cite the same clause from the document if they are referring to the same obligation.

**Why 0.7 threshold?** This is lenient enough to tolerate minor wording differences in how the two extractions copy the source clause, but strict enough to prevent unrelated obligations from being falsely paired.

### Agreement Check

**Lines 325-358**

Once two obligations are paired, the pipeline checks whether they substantively agree.

```python
def check_agreement(
    primary: Obligation,
    independent: Obligation,
    text_threshold: float = 0.6,
) -> bool:
```

Two obligations "agree" if **all three conditions** are met:

1. Same `obligation_type` (exact match)
2. Same `responsible_party` (exact match)
3. `obligation_text` similarity > 0.6 (using `difflib.SequenceMatcher`)

**Why a lower threshold (0.6) for obligation_text?** The two prompts deliberately use different framing, so the summary text will naturally differ in wording. A 0.6 threshold allows for different phrasing while still catching cases where the two extractions describe fundamentally different obligations.

**Why exact match for type and party?** These are constrained fields (type comes from a closed enum, party from the extracted roles), so they should be identical if the two extractions refer to the same obligation.

### Verification Layer 1: Grounding Check

**Lines 366-386**

```python
def verify_grounding(obligation: Obligation, raw_text: str) -> bool:
    grounded = obligation.source_clause in raw_text
    return grounded
```

This is deliberately the simplest function in the module. It performs a **mechanical substring match** -- no AI, no heuristics. If `source_clause` appears anywhere in `raw_text`, the check passes.

**Why this works:** Because the extraction prompt demands character-for-character copying, any hallucinated or paraphrased clause will fail this check. This is the first line of defense against LLM fabrication.

**Why it uses `in` rather than fuzzy matching:** The design intentionally avoids fuzzy matching. If the LLM cannot reproduce the exact text, that is a signal the extraction may be unreliable. Fuzzy matching would mask this signal. The tradeoff is that minor formatting differences (e.g., extra whitespace introduced during OCR) can cause false negatives -- but the team has decided this is preferable to false positives.

### Verification Layer 2: Chain-of-Verification (CoVe)

**Lines 393-497**

CoVe is triggered for obligations with DISAGREED or SOLO agreement status (see lines 579-580 in the orchestrator). It serves as an arbitration mechanism for extractions that lack ensemble consensus. It is a three-step process:

#### Step 1: Generate Verification Questions (Lines 451-466)

**Lines 393-399:** The `_COVE_QUESTIONS_SYSTEM_PROMPT` instructs Claude to generate 3-5 specific verification questions that can be answered by re-reading the document.

```python
questions_parsed: _CoVeQuestionsResponse = extract_with_structured_output(
    client=client,
    system_prompt=_COVE_QUESTIONS_SYSTEM_PROMPT,
    user_prompt=questions_user_prompt,
    response_format=_CoVeQuestionsResponse,
)
```

The user prompt (lines 452-458) includes the obligation details and source clause.

#### Step 2: Answer Questions from the Document (Lines 468-479)

**Lines 402-407:** The `_COVE_ANSWERS_SYSTEM_PROMPT` tells Claude to answer each question **only** using the provided document text. If the answer cannot be found, it must say "NOT FOUND".

```python
answers_user_prompt = (
    f"Document text:\n{raw_text}\n\n"
    f"Questions:\n" + "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
)
```

**Critical design point:** The questions and answers are generated in **separate LLM calls**. This is intentional. In the question-generation call, the LLM sees the extraction. In the answer call, the LLM sees only the document and the questions -- not the original extraction. This separation prevents the LLM from simply confirming its own prior output (a common pitfall called "self-confirmation bias").

#### Step 3: Compare Results (Lines 482-497)

```python
not_found_count = sum(1 for a in answers if "NOT FOUND" in a.upper())
cove_passed = not_found_count == 0
```

**Lines 484-485:** The comparison logic is simple: if any answer contains "NOT FOUND", the CoVe check fails. The idea is that if a verification question cannot be answered from the document, the original extraction likely referenced information that does not exist in the source material.

**Return value (lines 493-497):**

```python
return {
    "cove_passed": cove_passed,
    "questions": questions,
    "answers": answers,
}
```

The questions and answers are preserved in the output for auditability.

### The Orchestrator: `extract_and_verify()`

**Lines 505-623**

This is the main entry point for Stage 3. It ties together all the components described above.

```python
def extract_and_verify(
    text: str,
    claude_client=None,
) -> list[dict]:
```

**Pipeline flow:**

1. **Line 541:** Extract party roles via `extract_party_roles()`.
2. **Lines 544-549:** Run both extractions independently:
   - `extract_obligations()` -- primary extraction
   - `extract_obligations_independent()` -- independent extraction with different prompt framing
3. **Lines 552-555:** Match obligations from both extractions via `match_extractions()`.
4. **Lines 559-606:** For each matched pair:
   - **Lines 567-572:** Determine agreement status (AGREED / DISAGREED / SOLO) via `check_agreement()`.
   - **Line 575:** Run grounding check on the primary obligation.
   - **Lines 578-580:** If DISAGREED or SOLO, run CoVe arbitration.
   - **Lines 583-593:** Determine final status.

**The status decision logic (lines 583-593):**

```python
if agreement == "AGREED" and grounded:
    status = "VERIFIED"
elif agreement in ("DISAGREED", "SOLO"):
    cove_ok = cove_result is not None and cove_result.get("cove_passed", False)
    if grounded and cove_ok:
        status = "VERIFIED"
    else:
        status = "UNVERIFIED"
else:
    # AGREED but not grounded
    status = "UNVERIFIED"
```

This is a **conditional decision tree** rather than a simple AND gate:

- **AGREED + grounded** --> VERIFIED. When both extractions agree and the source clause is grounded in the document, no further verification is needed. Ensemble consensus is sufficient.
- **DISAGREED or SOLO** --> needs both grounding AND CoVe to pass. Since the extractions did not agree, the pipeline requires stronger evidence.
- **AGREED but not grounded** --> UNVERIFIED. Even if both extractions agree, a fabricated source clause means the extraction cannot be trusted.

**Output format (lines 595-605):** Each obligation produces a dict with full traceability:

```python
entry = {
    "obligation": primary_obl.model_dump(),
    "grounding": grounded,
    "ensemble": {
        "agreement": agreement,
        "primary_extraction": primary_obl.model_dump(),
        "independent_extraction": independent_obl.model_dump() if independent_obl else None,
    },
    "cove": cove_result,
    "status": status,
}
```

Note the `"ensemble"` key replaces the old `"claude_verification"` key. It contains the agreement status and both extractions for full auditability. The `"cove"` key is `None` for AGREED obligations (CoVe is not triggered).

**Logging (lines 617-622):** The pipeline logs a final summary with total, verified, and unverified counts.

---

## LLM Client Walkthrough

All LLM calls in Stage 3 go through a single client module. OpenAI has been completely removed from the pipeline.

### `claude_client.py`

**Full path:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/llm/claude_client.py`

**Lines 19-20:** `get_anthropic_client()` creates an Anthropic client using `settings.anthropic_api_key` (from `config.py`).

**Lines 23-80:** `extract_with_structured_output()` is the workhorse function used by all Stage 3 extraction and CoVe calls. It works by:

1. Converting a Pydantic model into a JSON Schema (line 50).
2. Creating a tool definition with that schema (lines 56-62).
3. Calling `client.messages.create()` with `tool_choice` forced to that tool (lines 64-71).
4. Parsing the tool_use block back into a typed Pydantic instance (line 76).

This approach gives Claude's equivalent of "structured output" -- the response is guaranteed to conform to the Pydantic schema because Claude is forced to call the tool, and the tool's input schema matches the model.

**Lines 83-110:** `verify_extraction()` is a legacy convenience function for obligation verification. It is no longer called by Stage 3 (which now uses the dual ensemble approach instead), but remains in the module for backward compatibility.

**Configuration:** The model is configurable via the `ANTHROPIC_MODEL` environment variable (see `config.py`).

---

## Test Suite Walkthrough: `test_stage_3_extraction.py`

**Full path:** `/Users/shangchienliu/Github-local/echelonos/tests/e2e/test_stage_3_extraction.py`

All LLM calls are mocked. No API keys or network access are needed to run these tests. All mocks target the Claude client (via `extract_with_structured_output`), since Claude is the sole LLM provider.

### Test Fixtures and Helpers

`SAMPLE_CONTRACT_TEXT` is a realistic multi-article contract covering delivery, financial, confidentiality, and indemnification clauses. This text is carefully crafted so that the `source_clause` fields in the sample obligations appear **verbatim** within it.

`SAMPLE_PARTY_ROLES` defines the expected role mapping.

`SAMPLE_OBLIGATION` is a high-confidence (0.95) delivery obligation. Its `source_clause` is an exact substring of `SAMPLE_CONTRACT_TEXT`. This is critical for the grounding check tests.

Mock helpers create MagicMock objects mimicking the Anthropic client's behavior, with `extract_with_structured_output` returning typed Pydantic instances.

### `TestExtractPartyRoles`

Calls `extract_party_roles()` with the mock client. Asserts the returned roles match exactly. Verifies the client was called exactly once (no unexpected extra calls).

### `TestExtractObligations`

Tests both the primary `extract_obligations()` and the independent `extract_obligations_independent()` functions. Verifies that each returns an `ExtractionResult` with the correct number of obligations and that individual fields (type, parties, confidence, page) are preserved.

### `TestMatchExtractions`

Tests the `match_extractions()` function. Verifies that obligations with similar source clauses are correctly paired, that unmatched primary obligations produce SOLO entries (paired with `None`), and that unmatched independent obligations are also appended as SOLO entries.

### `TestAgreementCheck`

Tests the `check_agreement()` function. Verifies that two obligations with the same type, responsible party, and similar text are marked as agreeing. Tests disagreement cases where obligation_type, responsible_party, or obligation_text differs significantly.

### `TestGroundingCheck`

`test_grounding_check_passes` -- the sample obligation's `source_clause` is present in the contract text, so `verify_grounding()` returns `True`.

`test_grounding_check_fails` -- a fabricated obligation with a non-existent `source_clause` is tested against the contract text. Since this clause does not appear in the text, `verify_grounding()` returns `False`.

### `TestCoVe`

Tests that CoVe runs correctly with two sequential Claude calls (question generation, then answer generation). All answers are found in the document, so `cove_passed` is `True`. Verifies that `extract_with_structured_output` was called exactly twice.

### `TestFullPipeline`

**Happy path (AGREED + grounded):** Both extractions agree and the source clause is grounded. Status is VERIFIED. CoVe is not triggered (`cove` is `None`). The result dict contains an `"ensemble"` key with `agreement: "AGREED"`.

**UNVERIFIED marking (AGREED but not grounded):** Both extractions agree but the source clause is fabricated. Status is UNVERIFIED.

**DISAGREED path with CoVe:** The two extractions disagree on a field (e.g., different obligation_type). CoVe is triggered. If grounding and CoVe both pass, status is VERIFIED. If either fails, status is UNVERIFIED.

**SOLO path with CoVe:** An obligation appears in only one extraction (no match). CoVe is triggered as arbitration. The test verifies Claude is called the expected number of times: party roles (1) + primary extraction (1) + independent extraction (1) + CoVe questions (1) + CoVe answers (1) = 5 calls total for a SOLO obligation that triggers CoVe.

---

## Key Takeaways

1. **Dual independent extraction is the safety net.** Two separate Claude calls with different prompt framings ("obligations" vs "binding commitments") provide genuinely independent signals. Two independently-prompted extractions agreeing is much stronger than one extraction verified after the fact.

2. **Prompt framing diversity avoids anchoring bias.** The primary prompt says "obligation"; the independent prompt says "binding commitment". This deliberate variation means the two extractions are less likely to share the same blind spots or make the same mistakes.

3. **Grounding is your cheapest defense against hallucination.** The substring check is free (no API call) and catches the most egregious fabrications. It works because both prompts demand verbatim copying.

4. **CoVe is selective by design.** Running CoVe on every obligation would significantly increase the number of Claude API calls. CoVe is only triggered for DISAGREED or SOLO extractions -- obligations that lack ensemble consensus and need arbitration.

5. **The decision tree is conservative.** AGREED + grounded obligations pass immediately. Disputed obligations must pass both grounding AND CoVe. AGREED but ungrounded obligations are marked UNVERIFIED. This means some legitimate obligations may be marked UNVERIFIED (false negatives), but fabricated obligations are very unlikely to be marked VERIFIED (low false positives). For legal contract analysis, this is the right tradeoff.

6. **Dependency injection enables testing.** Every function accepts an optional `claude_client` parameter. This allows tests to inject mocks without patching or monkeypatching global state.

7. **Structured output via tool_use eliminates JSON parsing failures.** Using `extract_with_structured_output()` with Pydantic models guarantees type-safe responses from Claude via the tool_use API. No manual JSON parsing is needed for any extraction or CoVe call.

8. **Party role extraction happens first for a reason.** Normalizing party references to role labels (Vendor, Client) before extraction prevents downstream inconsistencies and is essential for the agreement check (which compares `responsible_party` by exact match).

---

## Watch Out For

1. **OCR artifacts can break grounding.** If Stage 1/2 introduces extra whitespace, ligature characters, or other OCR artifacts into the raw text, the substring match in `verify_grounding()` (line 380) will fail even for legitimate extractions. If you see high rates of grounding failures, check the OCR quality first.

2. **The matching threshold (0.7) and agreement text threshold (0.6) are hardcoded.** These are parameters of `match_extractions()` (line 269) and `check_agreement()` (line 328) respectively. There is no configuration option for these values. If you need to tune them, you must modify the source code or pass them as arguments to those functions.

3. **CoVe's "NOT FOUND" check is a simple string match (line 484).** It uses `"NOT FOUND" in a.upper()`, which could false-positive on answers like "The term 'NOT FOUND' appears in the contract." This is unlikely for legal documents but worth being aware of.

4. **Full document text is sent to Claude multiple times.** The document is included in the party role extraction, both obligation extraction calls, and potentially the CoVe answer call. For very large contracts, this could significantly increase API costs. There is no chunking or summarization -- the entire document is included in each message.

5. **No retry logic.** If an LLM API call fails (network error, rate limit, etc.), the function will raise an unhandled exception. Retry logic should be handled at the Prefect flow layer (Stage 3 is called from a Prefect task), not within these functions.

6. **Both `_EXTRACTION_SYSTEM_PROMPT` and `_INDEPENDENT_EXTRACTION_SYSTEM_PROMPT` use a two-phase template (lines 148 and 217).** The `{roles_placeholder}` literal is embedded via `.format()` at module load time. This means if you add curly braces to `OBLIGATION_TYPES` names, the `.format()` call will break. The `roles_placeholder` is later replaced with `.replace()` per-call, which is safe from this issue.

7. **SOLO obligations from the independent extraction are also processed.** After matching, any unmatched independent obligations are appended as SOLO entries (lines 313-316). These are treated the same as unmatched primary obligations -- they need grounding + CoVe to become VERIFIED. This means the pipeline can surface obligations that only the independent extraction found, which is a feature, not a bug.

8. **Agreement check uses exact match for `obligation_type` and `responsible_party`.** If the two prompts produce slightly different role labels (e.g., "Vendor" vs "vendor"), the agreement check will fail. This is mitigated by the party role extraction step which normalizes role labels before they are injected into both prompts.
