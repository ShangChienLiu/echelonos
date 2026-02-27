# Tutorial 01 -- Infrastructure: Configuration, Logging, LLM & OCR Clients

> **Linear ticket:** AKS-20 -- Infrastructure: Pydantic Settings, structlog, LLM client wrappers, OCR client

---

## Table of Contents

1. [Overview](#overview)
2. [Configuration Management (`config.py`)](#configuration-management-configpy)
   - [File Walkthrough](#config-file-walkthrough)
   - [How Settings Are Loaded](#how-settings-are-loaded)
   - [The Singleton Pattern](#the-singleton-pattern)
3. [LLM Client Abstraction (`llm/`)](#llm-client-abstraction-llm)
   - [Package Init (`llm/__init__.py`)](#llm-package-init)
   - [Claude Client (`llm/claude_client.py`)](#claude-client)
4. [OCR Client (`ocr/`)](#ocr-client-ocr)
   - [Package Init (`ocr/__init__.py`)](#ocr-package-init)
   - [Mistral OCR Client (`ocr/mistral_client.py`)](#mistral-ocr-client)
5. [Structured Logging (structlog)](#structured-logging-structlog)
6. [Retry Logic (tenacity)](#retry-logic-tenacity)
7. [Key Takeaways](#key-takeaways)
8. [Watch Out For](#watch-out-for)

---

## Overview

The infrastructure layer provides three foundational services consumed by every pipeline stage:

1. **Configuration** -- centralized environment-variable-based settings via `pydantic-settings`
2. **LLM Client** -- thin wrapper around the Anthropic SDK for Claude structured output
3. **OCR Client** -- Mistral OCR wrapper with table-to-markdown conversion

These modules are deliberately thin. They do not contain business logic -- they exist to provide configured, ready-to-use clients to the stage modules. This separation means stages can be tested with mock clients injected via function parameters.

---

## Configuration Management (`config.py`)

**File:** `/Users/shangchienliu/Github-local/echelonos/config.py`

Full path: `src/echelonos/config.py`

### Config File Walkthrough

```python
"""Application configuration via environment variables."""           # Line 1

from pydantic_settings import BaseSettings                           # Line 3
```

**Line 3:** The single import. `pydantic_settings.BaseSettings` is the foundation. It automatically reads environment variables and `.env` files, validates types, and provides sensible defaults. This replaces the common pattern of scattering `os.environ.get()` calls throughout the codebase.

```python
class Settings(BaseSettings):                                        # Line 6
    # PostgreSQL
    postgres_host: str = "localhost"                                  # Line 8
    postgres_port: int = 5432                                        # Line 9
    postgres_db: str = "echelonos"                                   # Line 10
    postgres_user: str = "echelonos"                                 # Line 11
    postgres_password: str = "echelonos_dev"                         # Line 12
```

**Lines 8-12 -- PostgreSQL settings:** Each field has a default that works for local development with `docker-compose.yml`. In production, these are overridden by environment variables. Note that `pydantic-settings` automatically maps `POSTGRES_HOST`, `POSTGRES_PORT`, etc. (case-insensitive) to these fields.

```python
    # Anthropic (Claude - extraction & verification)
    anthropic_api_key: str = ""                                      # Line 15
    anthropic_model: str = "claude-opus-4-6"                         # Line 16
```

**Lines 15-16 -- Anthropic settings:** The API key defaults to empty string. This means the application will import successfully even without an API key -- it will only fail when a stage actually tries to call the API. The default model is `claude-opus-4-6`, used for all extraction, classification, and verification tasks.

```python
    # Mistral (OCR)
    mistral_api_key: str = ""                                        # Line 19
    mistral_ocr_model: str = "mistral-ocr-latest"                    # Line 20
```

**Lines 19-20 -- Mistral OCR settings:** API key and model for the Mistral OCR service. The model defaults to `mistral-ocr-latest`.

```python
    # Prefect
    prefect_api_url: str = "http://localhost:4200/api"               # Line 27
```

**Line 27 -- Prefect API URL:** Points to the local Prefect server started by `docker-compose.yml`.

```python
    @property
    def database_url(self) -> str:                                   # Line 30
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def async_database_url(self) -> str:                             # Line 37
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
```

**Lines 29-41 -- Computed database URLs:** Two `@property` methods assemble the full database connection URL from the individual components. The sync URL uses the default `psycopg2` driver (`postgresql://`). The async URL uses `asyncpg` (`postgresql+asyncpg://`). This dual-URL pattern is standard in projects that use both synchronous SQLAlchemy (for migrations, scripts) and asynchronous SQLAlchemy (for the Prefect flow or FastAPI endpoints).

**Design decision:** Why properties instead of fields? Because the URL is derived from other fields. If you set `POSTGRES_HOST=db.prod.internal`, the URL automatically updates. If these were plain fields, you would need to remember to set both the individual components AND the full URL.

```python
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}  # Line 43
```

**Line 43 -- `.env` file support:** This tells pydantic-settings to look for a `.env` file in the current working directory. Values in `.env` are overridden by actual environment variables (env vars take precedence). The encoding is explicitly set to UTF-8 to avoid platform-specific issues.

```python
settings = Settings()                                                # Line 46
```

**Line 46 -- Module-level singleton:** A single `Settings` instance is created at import time. Every module that does `from echelonos.config import settings` gets the same object. This is the configuration singleton pattern.

### How Settings Are Loaded

The loading order (highest priority first):

1. **Environment variables** -- `export ANTHROPIC_API_KEY=sk-ant-...`
2. **`.env` file** -- `ANTHROPIC_API_KEY=sk-ant-...` in a file named `.env`
3. **Default values** -- defined in the class (e.g., `anthropic_model: str = "claude-opus-4-6"`)

Pydantic-settings handles this automatically. You do not need to call `load_dotenv()` or parse anything manually.

### The Singleton Pattern

```python
# In config.py:
settings = Settings()

# In any other module:
from echelonos.config import settings
print(settings.anthropic_model)  # "claude-opus-4-6"
```

This works because Python modules are singletons. The first time `config.py` is imported, `Settings()` is evaluated and the result is stored. Every subsequent import of `settings` returns the same object. There is no re-parsing of environment variables.

**Testing tip:** To override settings in tests, you can either:
- Set environment variables before importing (`monkeypatch.setenv(...)`)
- Pass mock clients to stage functions (all stage functions accept optional client parameters)

---

## LLM Client Abstraction (`llm/`)

### LLM Package Init

**File:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/llm/__init__.py`

```python
"""LLM client integrations."""                                       # Line 1
```

This file is intentionally empty beyond the docstring. The LLM package does not re-export anything to the top level. Consumers import directly from the client module:

```python
from echelonos.llm.claude_client import get_anthropic_client, extract_with_structured_output
```

### Claude Client

**File:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/llm/claude_client.py`

```python
"""Claude client for document classification and obligation verification.

Uses Anthropic's tool_use API for structured output (classification) and
free-form messages for verification tasks.
"""

from __future__ import annotations

import json
import anthropic
import structlog

from echelonos.config import settings

log = structlog.get_logger(__name__)
```

**Lines 1-16:** Imports the Anthropic SDK, structlog, and the settings singleton.

```python
def get_anthropic_client() -> anthropic.Anthropic:                     # Line 19
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)     # Line 20
```

**Lines 19-20 -- Client factory:** Creates a new Anthropic client using the API key from settings. Same factory pattern as all other clients -- creates a new instance on every call, making testing trivial via mock injection.

```python
def extract_with_structured_output(
    client: anthropic.Anthropic,
    system_prompt: str,
    user_prompt: str,
    response_format: type,
):                                                                     # Line 23
    """Extract structured data using Claude with tool_use."""
```

**Lines 23-80 -- Structured output extraction:** This is the core function used by Stages 2, 3, and 5 for all LLM calls. Key points:

- **Line 50:** Converts the Pydantic model into a JSON Schema for the tool definition using `response_format.model_json_schema()`.
- **Lines 56-62:** Wraps the schema as a tool named `"structured_output"`. Claude's tool_use API guarantees the response matches the schema.
- **Lines 64-71:** Uses `client.messages.create()` with `tool_choice={"type": "tool", "name": tool_name}` to force Claude to call the tool, ensuring structured output.
- **Line 65:** The model is pulled from settings (`settings.anthropic_model`), defaulting to `claude-opus-4-6`.
- **Line 67:** System prompt is passed via the `system` parameter (separate from messages).
- **Line 76:** Extracts the tool call input and validates it against the Pydantic model using `response_format.model_validate(block.input)`.

The module also contains a `verify_extraction()` utility function for standalone verification tasks, though Stage 3 now uses the dual extraction ensemble approach instead.

---

## OCR Client (`ocr/`)

### OCR Package Init

**File:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/ocr/__init__.py`

```python
"""OCR integration for document processing (Mistral OCR)."""          # Line 1
```

Same pattern as the LLM package -- empty init, direct imports from `mistral_client`.

### Mistral OCR Client

**File:** `/Users/shangchienliu/Github-local/echelonos/src/echelonos/ocr/mistral_client.py`

This module handles OCR and table extraction using Mistral's OCR API.

```python
"""Mistral OCR client for document text extraction with table preservation."""

from __future__ import annotations
import base64
import mimetypes
from pathlib import Path

import structlog
from mistralai import Mistral

from echelonos.config import settings
```

**Lines 1-12:** Imports the Mistral SDK, base64 for encoding files, and the settings singleton. The `Mistral` class is the official Python client.

```python
def get_mistral_client() -> Mistral:                                       # Line 29
    """Create and return a Mistral client from application settings."""
    return Mistral(api_key=settings.mistral_api_key)                       # Line 31
```

**Lines 29-31 -- Client factory:** Same factory pattern as the LLM client.

```python
def analyze_document(client: Mistral, file_path: str) -> dict:            # Line 43
```

**Lines 43-137 -- Document analysis:** The main OCR function. Key points:

- **Lines 64-69:** Reads the file, base64-encodes it, and constructs a data URI (`data:{mime_type};base64,{data}`). Mistral accepts inline document uploads.
- **Lines 74-77:** Determines document type based on MIME -- images use `"image_url"`, other files use `"document_url"`.
- **Lines 79-83:** Calls `client.ocr.process()` with the configured model (`settings.mistral_ocr_model`, defaulting to `"mistral-ocr-latest"`).
- **Lines 88-129:** Parses the Mistral response into per-page dicts. Mistral returns everything as markdown. The parser splits each page's markdown into text and table parts by detecting lines that start and end with `|`.

**Table-to-markdown conversion:** Unlike Azure (which returns structured cell objects), Mistral already returns tables as markdown. The parser identifies table blocks by detecting consecutive lines matching the `| ... |` pattern and separates them from body text for downstream use.

**Confidence scores:** Mistral OCR does not provide per-page confidence scores. A reasonable default of 0.95 is used since Mistral OCR is generally high-quality for printed documents.

**Return format:**
```python
{"pages": [{"page_number": int, "text": str, "tables": [str], "confidence": float}], "total_pages": int}
```

**Why markdown tables?** Because downstream LLM stages (Claude) understand markdown tables natively. By preserving Mistral's table-as-markdown format, the pipeline feeds table data to LLMs in a format they can reason about effectively.

---

## Structured Logging (structlog)

The project uses **structlog** for all logging. You will see this pattern in every stage module:

```python
import structlog
log = structlog.get_logger(__name__)
```

Or the variant:

```python
logger = structlog.get_logger()
```

**Why structlog?**

1. **Key-value pairs:** Instead of `log.info(f"Found {count} files in {path}")`, you write `log.info("folder_scan_complete", count=count, path=path)`. This produces structured JSON that is machine-parseable and queryable in log aggregation systems.

2. **Contextual binding:** In `stage_0b_dedup.py` line 192, the logger is bound to a file path:
   ```python
   log = logger.bind(file_path=fp)
   ```
   All subsequent log calls from this bound logger include `file_path` automatically. This eliminates the need to repeat the same context in every log statement.

3. **Consistent event names:** Log events use dot-separated naming: `"pipeline.start"`, `"dedup.duplicate_found"`, `"claude_verification_complete"`. This makes log filtering easy: `grep "dedup\."` shows all dedup events.

**Common patterns in the codebase:**

```python
# Stage entry/exit logging
log.info("classifying_document", text_length=len(text))
log.info("classification_raw", doc_type=result.doc_type, confidence=result.confidence)

# Warning for suspicious conditions
log.warning("low_confidence_classification", doc_type=result.doc_type, confidence=result.confidence)

# Error for failures
log.error("mistral_ocr_error", file_path=file_path, doc_id=doc_id, error=str(exc))

# Debug for detailed tracing
log.debug("pdf_text_layer_check", file_path=file_path, avg_chars_per_page=round(avg, 1))
```

---

## Retry Logic (tenacity)

Retry logic is applied to external API calls (Mistral OCR, and potentially LLM calls). The primary example is in `stage_1_ocr.py`:

```python
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

@retry(
    retry=retry_if_exception_type((
        ConnectionError,
        TimeoutError,
        Exception,  # Mistral SDK may raise various HTTP errors
    )),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _call_mistral(client, file_path: str) -> dict:
    """Call Mistral OCR with retry logic."""
    return analyze_document(client, file_path)
```

**`retry_if_exception_type`:** Only retries on transient errors: `ConnectionError`, `TimeoutError`, and general HTTP errors from the Mistral SDK. Non-retryable errors raise immediately.

**`stop_after_attempt(3)`:** Maximum 3 attempts total (1 initial + 2 retries).

**`wait_exponential(multiplier=1, min=2, max=30)`:** Wait times are 2s, 4s, 8s, 16s, 30s (capped). The exponential backoff avoids hammering a rate-limited or overloaded service.

**`reraise=True`:** After all retries are exhausted, the original exception is re-raised (not a `RetryError`). This lets the caller handle the specific error type.

**Where else is retry used?** The Prefect `@task` decorator also provides retry logic at the orchestration level:
```python
@task(retries=2, retry_delay_seconds=30)
def stage_0a_validate(org_folder: str) -> list[dict]:
```
This is a coarser-grained retry that catches any exception from the entire task. The tenacity retry on `_call_mistral` is a finer-grained retry that targets specific transient API errors within a task.

---

## Key Takeaways

1. **pydantic-settings provides type-safe, validated configuration** with zero boilerplate. You declare fields with types and defaults, and it handles environment variable reading, `.env` file loading, and validation automatically. The `model_config` dict on line 43 is the only configuration needed.

2. **The client factory pattern** (`get_anthropic_client()`, `get_mistral_client()`) creates new client instances on each call. This is intentional -- it allows stages to accept pre-configured or mocked clients for testing via their optional `claude_client` and `mistral_client` parameters.

3. **Claude tool_use for structured output:** Claude's `tool_use` API guarantees schema conformance by converting Pydantic models into tool definitions. The `extract_with_structured_output()` function handles this conversion automatically, making it the single entry point for all structured LLM calls across Stages 2, 3, and 5.

4. **Table-to-markdown preservation** in the Mistral OCR client is critical for downstream LLM stages. Tables in contracts often contain obligation details (payment schedules, SLA thresholds, delivery dates). Mistral already returns tables as markdown, which the client separates from body text for downstream use.

5. **Two layers of retry:** Tenacity for API-level transient errors (exponential backoff, specific exception types) and Prefect `@task(retries=...)` for task-level retries (fixed delay, any exception). These layers are complementary, not redundant.

---

## Watch Out For

1. **Empty API keys are valid defaults:** The settings class defaults `anthropic_api_key` and `mistral_api_key` to empty strings. This means the application will start without errors even if no API keys are configured. You will only see failures when a stage tries to call the API. If you are debugging "connection refused" or "authentication failed" errors, check that your `.env` file or environment variables are set.

2. **`verify_extraction()` in `claude_client.py` is a utility, not the main verification path.** Stage 3 uses the dual extraction ensemble approach (two independent `extract_with_structured_output` calls with different prompts) for verification. The `verify_extraction()` function is a simpler standalone utility.

3. **`extract_with_structured_output()` returns a Pydantic model instance.** It uses Claude's tool_use API to force structured output, then validates the response with `response_format.model_validate()`. Callers treat the return value as the model type (e.g., `ClassificationResult`, `_ExtractionResponse`).

4. **Mistral OCR confidence is a placeholder.** Mistral OCR does not provide per-page confidence scores. The client uses a default of 0.95. If you need real confidence-based quality gating, you may need to implement a secondary quality check.

5. **No connection pooling for LLM clients.** Each call to `get_anthropic_client()` creates a new HTTP client. For a pipeline processing hundreds of documents, this means hundreds of TCP connections being opened and closed. The Anthropic SDK supports connection reuse. A future optimization would be to cache the client at the module level or pass a shared client through the pipeline.
