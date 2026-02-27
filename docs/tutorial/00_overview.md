# Tutorial 00 -- Project Overview & Architecture

> **Related tickets:** This document provides foundational context for every Linear ticket in the AKS project.

---

## Table of Contents

1. [What is Echelonos?](#what-is-echelonos)
2. [The 8-Stage Pipeline](#the-8-stage-pipeline)
3. [Tech Stack](#tech-stack)
4. [Project Structure](#project-structure)
5. [Key Source Files Walkthrough](#key-source-files-walkthrough)
   - [pyproject.toml](#pyprojecttoml)
   - [src/echelonos/\_\_init\_\_.py](#srcechelonos__init__py)
   - [src/echelonos/stages/\_\_init\_\_.py](#srcechelonosstages__init__py)
   - [src/echelonos/flows/pipeline.py](#srcechelonos flowspipelinepy)
6. [Key Takeaways](#key-takeaways)
7. [Watch Out For](#watch-out-for)

---

## What is Echelonos?

Echelonos is an **automated contract obligation extraction pipeline**. It takes a folder of contract documents (PDFs, DOCX files, email attachments, spreadsheets, images) belonging to an organization, and produces a structured **Obligation Report** that lists every contractual obligation, who is responsible, what the deadline is, and what the source clause is -- with a complete evidence trail.

The pipeline is designed for legal and procurement teams who need to understand what obligations exist across hundreds of contracts, including how amendments modify or supersede earlier terms.

---

## The 8-Stage Pipeline

Echelonos processes documents through eight sequential stages. Each stage has a dedicated Python module under `src/echelonos/stages/`.

```
Stage 0a: Validation
    |
Stage 0b: Deduplication
    |
Stage 1:  OCR (Document Ingestion)
    |
Stage 2:  Classification
    |
Stage 3:  Extraction + Verification (Dual Claude Ensemble)
    |
Stage 4:  Document Linking
    |
Stage 5:  Amendment Resolution
    |
Stage 6:  Evidence Packaging
    |
Stage 7:  Report Generation
```

### Stage 0a -- Validation (`stage_0a_validation.py`)

**Purpose:** Gate-keep incoming files before they enter the pipeline. Checks file existence, size, MIME type, corruption, password protection, and OCR requirements. Handles container formats (MSG, EML, ZIP) by recursively extracting child files.

**Supported categories:**
- **DIRECT:** PDF, DOCX, DOC, RTF, HTML (text-extractable)
- **IMAGE:** PNG, JPG, TIFF (flagged as `needs_ocr=True`)
- **CONTAINER:** MSG, EML, ZIP (children extracted and returned)
- **SPECIAL:** XLSX, XLS (structured data, validated for readability)
- **REJECTED:** video, audio, executables, databases, unrecognized MIME types

**Output:** A list of validation result dicts, each with `status` ("VALID", "INVALID", "NEEDS_PASSWORD", "REJECTED"), `original_format`, `needs_ocr`, and `child_files`.

### Stage 0b -- Deduplication (`stage_0b_dedup.py`)

**Purpose:** Remove duplicate files using a 4-layer hash pipeline:

| Layer | Technique | What it catches |
|-------|-----------|-----------------|
| 1 | SHA-256 of raw bytes | Exact copies (different filenames) |
| 2 | SHA-256 of normalized text | Format variants (PDF vs. DOCX of same content) |
| 3 | MinHash + LSH (Jaccard similarity >= 0.85) | Near-duplicates (minor edits) |
| 4 | Blocking keys + structural fingerprint | **Protects** amendments/SOWs from being deduped against their parent MSA |

Layer 4 is a **guard**, not a dedup layer. It uses Claude-extracted blocking keys (vendor name, PO number, invoice number, amounts, dates) with regex fallback to ensure that two documents with similar text but different structural identities (e.g., an MSA and its first amendment) are never treated as duplicates.

### Stage 1 -- OCR / Document Ingestion (`stage_1_ocr.py`)

**Purpose:** Convert documents into per-page text using **Mistral OCR**. Tables are preserved as markdown. Each page gets an OCR confidence score. A quality gate flags pages below two thresholds:
- `LOW_CONFIDENCE_THRESHOLD = 0.60` -- produces a `LOW_OCR_QUALITY` flag
- `MEDIUM_CONFIDENCE_THRESHOLD = 0.85` -- produces a `MEDIUM_OCR_QUALITY` flag

**Retry logic:** The Mistral API call is wrapped with `tenacity` retry decorators (3 attempts, exponential backoff 2-30s) for transient HTTP errors, service errors, connection errors, and timeouts.

### Stage 2 -- Classification (`stage_2_classification.py`)

**Purpose:** Classify each document into a contract type (MSA, SOW, Amendment, Addendum, NDA, Order Form, Other) using **Claude** with structured output. Also extracts parties, effective date, and parent contract references.

**Dual check:** After the LLM classifies the document, a rule-based `classify_with_cross_check()` function applies regex patterns to catch misclassifications. For example, if the LLM says "MSA" but the text contains "hereby amends", it reclassifies to "Amendment".

### Stage 3 -- Extraction + Verification (`stage_3_extraction.py`)

**Purpose:** The core LLM stage. Uses a **dual independent Claude extraction ensemble**:

1. **Primary extraction:** Claude extracts obligations with structured output (obligation text, type, responsible party, counterparty, frequency, deadline, source clause, source page, confidence).
2. **Independent extraction:** A second Claude call with different prompt framing ("binding commitments" vs "obligations") extracts independently, without seeing the first result.
3. **Programmatic matching:** Obligations from both runs are paired by `source_clause` similarity using `difflib.SequenceMatcher`.
4. **Agreement check:** Matched pairs are compared on `obligation_type`, `responsible_party`, and `obligation_text` similarity.
5. **Grounding check:** Mechanical substring match verifies the cited source clause exists verbatim in the document.
6. **Chain-of-Verification (CoVe):** For DISAGREED or SOLO extractions, Claude generates verification questions, re-reads the document to answer them independently, then compares with the original extraction.

AGREED + grounded obligations are marked **VERIFIED**. DISAGREED/SOLO obligations require both grounding and CoVe to pass. Otherwise they are marked **UNVERIFIED**.

### Stage 4 -- Document Linking (`stage_4_linking.py`)

**Purpose:** Link child documents (Amendments, Addendums, SOWs) to their parent contracts. This stage is **pure SQL/logic** -- no LLM calls. It parses `parent_reference_raw` strings (e.g., "MSA dated January 10, 2023 between Acme and CDW") and matches against the organization's document corpus using date, type, and party overlap.

**Link statuses:**
- `LINKED` -- exactly one match
- `UNLINKED` -- zero matches (dangling reference)
- `AMBIGUOUS` -- multiple matches (requires human review)

Also supports **backfill**: when a late-arriving document is ingested, all existing dangling references are re-checked.

### Stage 5 -- Amendment Resolution (`stage_5_amendment.py`)

**Purpose:** Walks amendment chains (MSA -> Amendment #1 -> Amendment #2 -> ...) to determine whether each original obligation is ACTIVE, SUPERSEDED, or TERMINATED. Uses LLM clause comparison with a heuristic pre-filter (keyword overlap >= 20%) to avoid unnecessary LLM calls.

### Stage 6 -- Evidence Packaging (`stage_6_evidence.py`)

**Purpose:** Creates **append-only, immutable** evidence records that trace every obligation back to its source clause, extraction model, verification result, and amendment history. Status changes produce NEW evidence records rather than updating existing ones. The `EvidenceRecord` Pydantic model is `frozen=True`.

### Stage 7 -- Report Generation (`stage_7_report.py`)

**Purpose:** Builds the final deliverable consisting of three sections:
1. **Obligation Matrix** -- per-vendor obligation table sorted by status, type, and party
2. **Flag Report** -- actionable flags (UNVERIFIED=RED, AMBIGUOUS=ORANGE, UNLINKED=YELLOW, UNRESOLVED=YELLOW, LOW_CONFIDENCE=WHITE)
3. **Summary** -- aggregate counts by type, status, party, and flag severity

Exports to both Markdown and JSON formats.

---

## Tech Stack

| Category | Technology | Version Constraint | Purpose |
|----------|------------|-------------------|---------|
| **Language** | Python | >= 3.11 | Core runtime |
| **Orchestration** | Prefect | >= 3.0 | Flow/task orchestration, retries, observability |
| **Database** | PostgreSQL | 16 | Primary data store |
| **ORM** | SQLAlchemy | >= 2.0 | Database models and queries (async via asyncpg) |
| **Migrations** | Alembic | >= 1.13 | Schema migrations |
| **LLM (Extraction & Verification)** | Anthropic Claude | anthropic >= 0.40 | Dual extraction ensemble, classification, amendment resolution |
| **OCR** | Mistral OCR | mistralai >= 1.0 | PDF/image OCR with table preservation |
| **Config** | pydantic-settings | >= 2.0 | Environment variable configuration |
| **Logging** | structlog | >= 24.0 | Structured JSON logging |
| **Retry** | tenacity | >= 9.0 | Retry logic for API calls |
| **Near-dup detection** | datasketch (MinHash + LSH) | >= 1.6 | Locality-sensitive hashing for Jaccard similarity |
| **Document parsing** | pypdf, python-docx, openpyxl, extract-msg, Pillow | various | Format-specific readers |
| **Date parsing** | python-dateutil | >= 2.9 | Fuzzy date extraction |
| **API** | FastAPI + Uvicorn | >= 0.115 | REST API serving reports |
| **Frontend** | React + Tailwind CSS | (see frontend/package.json) | Dashboard UI |
| **Containerization** | Docker Compose | -- | PostgreSQL 16 + Prefect 3 server |

---

## Project Structure

```
echelonos/
|-- CLAUDE.md                          # Project rules (git workflow, test-first)
|-- pyproject.toml                     # Build config, dependencies, tool settings
|-- docker-compose.yml                 # PostgreSQL 16 + Prefect 3 server
|-- alembic.ini                        # Alembic migration configuration
|-- alembic/
|   |-- env.py                         # Alembic env (imports Base from models)
|   |-- script.py.mako                 # Migration template
|   `-- versions/                      # Migration scripts
|
|-- src/echelonos/
|   |-- __init__.py                    # Package docstring
|   |-- config.py                      # Pydantic Settings (env vars, DB URLs)
|   |
|   |-- db/
|   |   |-- __init__.py                # "Database models and session management"
|   |   |-- models.py                  # SQLAlchemy 2.0 ORM models (8 tables)
|   |   `-- session.py                 # Engine, SessionLocal, get_db()
|   |
|   |-- llm/
|   |   |-- __init__.py                # "LLM client integrations"
|   |   `-- claude_client.py           # Claude client + structured output (extraction & verification)
|   |
|   |-- ocr/
|   |   |-- __init__.py                # "OCR integration for document processing"
|   |   `-- mistral_client.py          # Mistral OCR client + table->MD
|   |
|   |-- stages/
|   |   |-- __init__.py                # Re-exports all public stage APIs
|   |   |-- stage_0a_validation.py     # File validation gate (~1000 lines)
|   |   |-- stage_0b_dedup.py          # 4-layer hash deduplication
|   |   |-- stage_1_ocr.py             # Mistral OCR + confidence quality gate
|   |   |-- stage_2_classification.py  # Claude classification + cross-check
|   |   |-- stage_3_extraction.py      # Dual Claude ensemble extraction + verification
|   |   |-- stage_4_linking.py         # Parent-child document linking
|   |   |-- stage_5_amendment.py       # Amendment chain resolution
|   |   |-- stage_6_evidence.py        # Immutable evidence packaging
|   |   `-- stage_7_report.py          # Report generation (matrix + flags)
|   |
|   |-- flows/
|   |   |-- __init__.py
|   |   `-- pipeline.py                # Prefect flow orchestrating all stages
|   |
|   |-- db/
|   |   |-- ...
|   |   `-- persist.py                 # Idempotent upsert functions for ingestion
|   |
|   `-- api/
|       |-- __init__.py
|       |-- app.py                     # FastAPI app (report + upload + pipeline + clear endpoints)
|       `-- demo_data.py               # Demo data fallback for API development
|
|-- frontend/                          # React + Tailwind dashboard
|   |-- src/
|   |   |-- App.tsx                    # Main app component
|   |   |-- main.tsx                   # Entry point
|   |   |-- types.ts                   # TypeScript type definitions
|   |   |-- mockData.ts               # Mock data matching API schema
|   |   |-- index.css                  # Tailwind base styles
|   |   `-- components/
|   |       |-- DocumentUpload.tsx      # File upload, Run/Stop Pipeline, clear database
|   |       |-- StatsCards.tsx          # Top-level stats cards
|   |       |-- ObligationTable.tsx     # Obligation matrix table
|   |       |-- FlagPanel.tsx           # Flag report panel
|   |       |-- EvidenceDrawer.tsx      # Evidence trail drawer
|   |       `-- SummaryCharts.tsx       # Summary charts
|   |-- package.json
|   |-- vite.config.ts
|   `-- tsconfig.json
|
|-- tests/
|   |-- conftest.py
|   |-- unit/                          # Unit tests
|   `-- e2e/                           # End-to-end tests
|
`-- docs/
    `-- tutorial/                      # This tutorial series
```

---

## Key Source Files Walkthrough

### `pyproject.toml`

**File:** `/Users/shangchienliu/Github-local/echelonos/pyproject.toml`

This is the single source of truth for the project's build system, dependencies, and tool configuration.

**Lines 1-3 -- Build system:**
```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```
The project uses **Hatch** as its build backend. This is a modern Python build system that replaced setuptools for many projects. The `[tool.hatch.build.targets.wheel]` section on line 54 tells Hatch to package `src/echelonos` into the wheel.

**Lines 5-43 -- Project metadata and dependencies:**
The `name` is `echelonos`, version `0.1.0`, and it requires Python 3.11+. Dependencies are organized by category with inline comments:
- **Orchestration:** `prefect>=3.0`
- **Database:** `sqlalchemy>=2.0`, `alembic>=1.13`, `asyncpg>=0.30`, `psycopg2-binary>=2.9`
- **LLM Clients:** `anthropic>=0.40`
- **OCR:** `mistralai>=1.0`
- **Document processing:** `pypdf`, `python-docx`, `python-magic`, `openpyxl`, `extract-msg`, `Pillow`
- **Hashing/dedup:** `datasketch>=1.6`
- **Config:** `pydantic>=2.0`, `pydantic-settings>=2.0`
- **Utilities:** `structlog>=24.0`, `tenacity>=9.0`
- **API:** `fastapi>=0.115`, `uvicorn[standard]>=0.32`

**Lines 45-52 -- Dev dependencies:**
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
Install with `pip install -e ".[dev]"`. Note `pytest-asyncio` with `asyncio_mode = "auto"` on line 59, meaning you do not need `@pytest.mark.asyncio` on every test.

**Lines 65-75 -- Tool configuration:**
Ruff targets Python 3.11 with a 120-character line length. MyPy runs in `strict = true` mode -- every function needs type annotations.

### `src/echelonos/__init__.py`

**File:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/__init__.py`

A single docstring:
```python
"""Echelonos - Automated contract obligation extraction pipeline."""
```
This file is intentionally minimal. It marks the directory as a Python package and provides a package-level docstring. No imports are pulled to the top level -- consumers import from sub-packages directly (e.g., `from echelonos.config import settings`).

### `src/echelonos/stages/__init__.py`

**File:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/stages/__init__.py`

This file serves as the **public API surface** for all pipeline stages. It re-exports the key functions and classes from each stage module so that external consumers can write:

```python
from echelonos.stages import classify_document, extract_and_verify, generate_report
```

**Lines 3-7 -- Stage 0a exports:**
```python
from echelonos.stages.stage_0a_validation import (
    convert_to_pdf,
    validate_file,
    validate_folder,
)
```

**Lines 8-12 -- Stage 2 exports:**
```python
from echelonos.stages.stage_2_classification import (
    ClassificationResult,
    classify_document,
    classify_with_cross_check,
)
```

**Lines 13-22 -- Stage 3 exports:**
All extraction and verification functions plus the `Obligation` and `ExtractionResult` models.

**Lines 23-30 -- Stage 5 exports:**
Amendment resolution including `build_amendment_chain`, `compare_clauses`, `resolve_all`, `resolve_amendment_chain`, `resolve_obligation`.

**Lines 31-38 -- Stage 6 exports:**
Evidence packaging functions and the `EvidenceRecord` and `VerificationResult` models.

**Lines 39-49 -- Stage 7 exports:**
Report generation including `generate_report`, `export_to_json`, `export_to_markdown`, plus the `ObligationReport`, `ObligationRow`, and `FlagItem` models.

**Design decision:** Notice that Stages 0b (dedup), 1 (OCR), and 4 (linking) are NOT re-exported here. This is because those stages are called internally by the Prefect flow, not by external consumers. The stages `__init__.py` exports only the "public API" that is useful outside of the orchestration layer.

### `src/echelonos/flows/pipeline.py`

**File:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/flows/pipeline.py`

This is the **Prefect flow** that orchestrates the full pipeline. It is currently partially implemented (Stages 0a and 0b are wired; Stages 1-7 are noted as TODO).

**Line 1:**
```python
"""Main pipeline flow orchestrating all 8 stages."""
```

**Lines 3-6 -- Imports:**
```python
from prefect import flow, task
import structlog

logger = structlog.get_logger()
```
The file imports Prefect's `flow` and `task` decorators and sets up structlog. Note: each stage function uses a **lazy import** (importing inside the function body) to avoid circular imports and to keep the import graph lightweight when only specific stages are needed.

**Lines 9-14 -- Stage 0a as a Prefect task:**
```python
@task(retries=2, retry_delay_seconds=30)
def stage_0a_validate(org_folder: str) -> list[dict]:
    """Stage 0a: Validate all files in an organization folder."""
    from echelonos.stages.stage_0a_validation import validate_folder
    return validate_folder(org_folder)
```
Key points:
- `retries=2` means if validation fails (e.g., filesystem hiccup), Prefect will retry up to 2 more times with a 30-second delay.
- The import is **inside** the function body (lazy import). This pattern keeps the module importable even if a stage's dependencies are not installed.

**Lines 17-22 -- Stage 0b as a Prefect task:**
```python
@task(retries=1)
def stage_0b_dedup(valid_files: list[dict]) -> list[dict]:
    """Stage 0b: Deduplicate files using 4-layer hash pipeline."""
    from echelonos.stages.stage_0b_dedup import deduplicate_files
    return deduplicate_files(valid_files)
```
Only 1 retry for dedup since it is a CPU-bound operation that is unlikely to benefit from retrying.

**Lines 25-45 -- The main flow:**
```python
@flow(name="echelonos-pipeline", log_prints=True)
def run_pipeline(org_folder: str) -> dict:
```
The `@flow` decorator names this flow `"echelonos-pipeline"` in the Prefect UI. `log_prints=True` captures all `print()` statements as Prefect log entries (in addition to structlog).

The flow body:
1. Calls `stage_0a_validate(org_folder)` and filters for `"VALID"` files (line 32).
2. Passes valid files to `stage_0b_dedup()` to get unique files (line 36).
3. Returns a summary dict with counts (lines 40-45).
4. **Line 39:** A comment marks where Stages 1-7 will be wired as implementation progresses.

**Design decision -- Prefect's `@task` vs `@flow`:** Each stage is a `@task` so Prefect tracks its execution, retries, duration, and artifacts independently. The overall pipeline is a `@flow` so it gets its own dashboard entry and can be triggered via the Prefect API or scheduled via deployments.

---

## Key Takeaways

1. **Modular stage design:** Each of the 8 stages is a self-contained Python module with its own public API. Stages communicate via plain dicts and Pydantic models -- no global state.

2. **Dual Claude ensemble verification:** Two independent Claude calls extract obligations in parallel using different prompt framings. Extractions are paired, compared for agreement, and verified through programmatic matching, grounding checks, and Chain-of-Verification. This reduces hallucinations through consensus and independent re-reading.

3. **4-layer dedup with structural protection:** The dedup pipeline does not just catch exact copies. MinHash + LSH (Jaccard similarity) catches near-duplicates, while the blocking keys layer (Layer 4) uses Claude-extracted document metadata to prevent amendments from being incorrectly deduped against their parent contracts.

4. **Append-only evidence:** The evidence table has no `updated_at` column. Status changes create new rows. This is an audit-trail design that ensures the full history of every obligation is preserved.

5. **Lazy imports in Prefect tasks:** Stage modules are imported inside task function bodies, not at module level. This keeps the pipeline module lightweight and avoids import errors when optional dependencies are missing.

6. **Pure functions + orchestration:** Stage modules are pure (accept dicts, return dicts). The Prefect flow layer handles all database I/O and orchestration concerns. This makes stages independently testable.

---

## Watch Out For

1. **`datetime.utcnow()` deprecation:** The models in `db/models.py` use `datetime.utcnow` for default timestamps. This function is deprecated in Python 3.12+. A future migration should switch to `datetime.now(timezone.utc)` (Stage 7 already does this -- see `stage_7_report.py` line 484).

2. **Pipeline orchestration:** The Prefect flow in `flows/pipeline.py` wires Stages 0a and 0b. The API layer in `app.py` orchestrates the full pipeline (Stages 0a through 7) via a background thread with cancellation support.

3. **API with database fallback:** The FastAPI app queries the real PostgreSQL database for organizations, documents, and obligations. If an organization is not found in the database, it falls back to demo data. The `/api/upload`, `/api/pipeline/run`, and `/api/pipeline/stop` endpoints orchestrate the full pipeline with database persistence.

4. **Container extraction is one-level:** `validate_folder()` does NOT recursively validate children extracted from container files. The docstring explicitly states this (line 977-979). Callers must inspect `child_files` and validate children separately.

5. **MinHash + LSH parameters:** The MinHash-based near-duplicate detection uses tunable LSH band and row parameters with a Jaccard similarity threshold of 0.85. If you see false positives or false negatives in dedup, check the MinHash configuration in `stage_0b_dedup.py`.
