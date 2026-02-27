# Stage 0a: File Validation & Normalization

> **Linear ticket:** AKS-11
>
> **Source file:** `src/echelonos/stages/stage_0a_validation.py` (1014 lines)
> **Test file:** `tests/e2e/test_stage_0a_validation.py` (704 lines)
> **Shared fixtures:** `tests/conftest.py` (186 lines)

---

## Table of Contents

1. [Purpose and Role in the Pipeline](#1-purpose-and-role-in-the-pipeline)
2. [Format Categories](#2-format-categories)
3. [Module-Level Constants and Imports](#3-module-level-constants-and-imports)
4. [Result Schema](#4-result-schema)
5. [Function-by-Function Walkthrough](#5-function-by-function-walkthrough)
   - 5.1 [`_classify_format()`](#51-_classify_format--mime-type-detection)
   - 5.2 [`_check_exists_and_size()`](#52-_check_exists_and_size--pre-flight-guard)
   - 5.3 [`_detect_mime_type()`](#53-_detect_mime_type--libmagic-wrapper)
   - 5.4 [`_validate_pdf()`](#54-_validate_pdf--corruption--password-check)
   - 5.5 [`_validate_pdf()`](#55-_validate_pdf--corruption--password-check)
   - 5.6 [`_validate_docx()` and `_validate_doc()`](#56-_validate_docx-and-_validate_doc--word-document-checks)
   - 5.7 [`_extract_html_text()`](#57-_extract_html_text--html-parsing)
   - 5.8 [`_extract_xlsx_tables()`](#58-_extract_xlsx_tables--excel-table-extraction)
   - 5.9 [`_extract_msg_attachments()`](#59-_extract_msg_attachments--outlook-msg-extraction)
   - 5.10 [`_extract_eml_attachments()`](#510-_extract_eml_attachments--email-attachment-extraction)
   - 5.11 [`_extract_zip_contents()`](#511-_extract_zip_contents--zip-extraction-with-bomb-protection)
   - 5.12 [`validate_file()`](#512-validate_file--main-entry-point)
   - 5.13 [`convert_to_pdf()`](#513-convert_to_pdf--libreoffice-headless-conversion)
   - 5.14 [`validate_folder()`](#514-validate_folder--batch-processing)
6. [Status Values Reference](#6-status-values-reference)
7. [Test Coverage Walkthrough](#7-test-coverage-walkthrough)
8. [Shared Test Fixtures (conftest.py)](#8-shared-test-fixtures-conftestpy)
9. [Key Takeaways](#9-key-takeaways)
10. [Watch Out For](#10-watch-out-for)

---

## 1. Purpose and Role in the Pipeline

Stage 0a is the **first gate** in the Echelonos contract obligation extraction pipeline. Before any text extraction, OCR, or NLP processing occurs, every incoming file must pass through this validation stage. It answers three critical questions:

1. **Can we process this file at all?** (existence, size, corruption, password protection)
2. **What kind of file is it?** (MIME detection and format classification)
3. **What additional work is needed?** (OCR for images, child extraction for containers, conversion for non-PDF formats)

The module docstring at lines 1-14 of `src/echelonos/stages/stage_0a_validation.py` summarizes the five format categories and the overall purpose:

```python
"""Stage 0a: File Validation Gate.

Validates incoming contract files before they enter the extraction pipeline.
Checks for file existence, size, MIME type, corruption, password protection,
OCR requirements, and handles container formats (MSG, EML, ZIP) with
recursive extraction of child files.
"""
```

---

## 2. Format Categories

The module classifies every incoming file into one of **five** categories. Each category follows a different validation and processing path.

| Category | Formats | Behavior |
|---|---|---|
| **DIRECT** | PDF, DOCX, DOC, RTF, HTML | Text-extractable documents. Undergo format-specific corruption checks. PDFs always set `needs_ocr=True` (Mistral OCR handles both scanned and text-based PDFs). |
| **IMAGE** | PNG, JPG, TIFF | Always marked `needs_ocr=True`. TIFF files also get a page-count check via Pillow. |
| **CONTAINER** | MSG, EML, ZIP | Recursively extract child files into a sub-directory. Children are listed in `child_files` for downstream re-validation. |
| **SPECIAL** | XLSX, XLS | Structured tabular data. XLSX is validated for non-empty content via openpyxl; XLS gets an OLE2 magic-byte check. |
| **REJECTED** | `video/*`, `audio/*`, executables, databases, anything unrecognised | Immediately rejected. No further processing. |

These categories are defined in the `MIME_FORMAT_MAP` dictionary at lines 39-58 and the rejection lists at lines 61-66.

---

## 3. Module-Level Constants and Imports

**File:** `src/echelonos/stages/stage_0a_validation.py`, lines 16-73.

### External dependencies

| Library | Import (line) | Purpose |
|---|---|---|
| `python-magic` | `import magic` (line 27) | MIME type detection via libmagic |
| `pypdf` | `from pypdf import PdfReader` (line 29) | PDF reading, text extraction, encryption detection |
| `structlog` | `import structlog` (line 28) | Structured logging throughout the module |
| `html.parser` | `from html.parser import HTMLParser` (line 23) | HTML text stripping (stdlib, zero extra deps) |
| `openpyxl` | Lazy import inside `_extract_xlsx_tables()` | XLSX reading |
| `extract-msg` | Lazy import inside `_extract_msg_attachments()` | Outlook MSG parsing |
| `python-docx` | Lazy import inside `_validate_docx()` | DOCX corruption check |
| `Pillow` | Lazy import inside image validation | TIFF page counting |

**Design decision:** Heavy optional libraries (`openpyxl`, `extract_msg`, `python-docx`, `Pillow`) are imported lazily inside the functions that need them. This keeps startup fast and allows the module to load even if some optional deps are missing.

### Key constants

```python
ZIP_MAX_FILES = 100                          # line 91
ZIP_MAX_TOTAL_BYTES = 500 * 1024 * 1024      # line 92 — 500 MB
```

- `ZIP_MAX_FILES` and `ZIP_MAX_TOTAL_BYTES` are zip-bomb safety limits.

Note that the earlier `PDF_TEXT_CHAR_THRESHOLD` constant has been removed. PDFs now always set `needs_ocr=True` because Mistral OCR handles both scanned and text-based PDFs effectively, making the text-layer detection heuristic unnecessary.

### The MIME format map

Lines 39-58 define `MIME_FORMAT_MAP`, a `dict[str, tuple[str, str]]` mapping MIME type strings to `(format_name, category)` tuples:

```python
MIME_FORMAT_MAP: dict[str, tuple[str, str]] = {
    "application/pdf": ("PDF", "direct"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("DOCX", "direct"),
    "application/msword": ("DOC", "direct"),
    "application/rtf": ("RTF", "direct"),
    "text/rtf": ("RTF", "direct"),
    "text/html": ("HTML", "direct"),
    "image/png": ("PNG", "image"),
    "image/jpeg": ("JPG", "image"),
    "image/tiff": ("TIFF", "image"),
    "application/vnd.ms-outlook": ("MSG", "container"),
    "message/rfc822": ("EML", "container"),
    "application/zip": ("ZIP", "container"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("XLSX", "special"),
    "application/vnd.ms-excel": ("XLS", "special"),
}
```

Note that RTF has **two** MIME entries (`application/rtf` and `text/rtf`) because different versions of libmagic may report either one.

### Rejection lists

Lines 61-66:

```python
REJECTED_MIME_PREFIXES: list[str] = ["video/", "audio/"]
REJECTED_MIME_TYPES: set[str] = {
    "application/x-executable",
    "application/x-dosexec",
    "application/x-sqlite3",
}
```

Any MIME type starting with `video/` or `audio/` is rejected. Additionally, executables (`x-executable`, `x-dosexec`) and SQLite databases (`x-sqlite3`) are explicitly blocked.

---

## 4. Result Schema

Every validation call returns a dict with exactly **seven keys**. The `_make_result()` helper at lines 101-138 enforces this structure:

```python
{
    "file_path":       str,              # Path to the validated file
    "status":          str,              # "VALID" | "INVALID" | "NEEDS_PASSWORD" | "REJECTED"
    "reason":          str,              # Human-readable explanation
    "original_format": str,              # e.g. "PDF", "DOCX", "MSG", "PNG", "XLSX"
    "needs_ocr":       bool,             # True if OCR is required for text extraction
    "extracted_from":  str | None,       # Parent container filename, or None
    "child_files":     list[str],        # Paths of extracted children (containers only)
}
```

The `extracted_from` field provides **provenance tracking**: when a file was extracted from an MSG, EML, or ZIP container, this field records the parent container's filename. The `child_files` field defaults to an empty list for non-container formats.

The test file validates this schema in the `TestNewOutputFields` class (test file lines 629-703) and via the `_assert_result_schema()` helper (test file lines 93-96):

```python
EXPECTED_RESULT_KEYS = {
    "file_path", "status", "reason", "original_format",
    "needs_ocr", "extracted_from", "child_files",
}
```

---

## 5. Function-by-Function Walkthrough

### 5.1 `_classify_format()` -- MIME Type Detection

**Lines 163-191.** This is the routing function that maps a MIME type string to a `(format_name, category)` tuple.

```python
def _classify_format(mime_type: str) -> tuple[str, str]:
    # Check explicit map first.
    if mime_type in MIME_FORMAT_MAP:
        return MIME_FORMAT_MAP[mime_type]

    # Check rejected prefixes.
    for prefix in REJECTED_MIME_PREFIXES:
        if mime_type.startswith(prefix):
            return (mime_type, "rejected")

    # Check rejected exact types.
    if mime_type in REJECTED_MIME_TYPES:
        return (mime_type, "rejected")

    # Anything else is also rejected.
    return (mime_type, "rejected")
```

**Logic flow:**
1. **Known format lookup** (line 178): O(1) dict lookup in `MIME_FORMAT_MAP`. If found, return immediately.
2. **Prefix rejection** (lines 182-184): Iterate `REJECTED_MIME_PREFIXES` and check `startswith`. Catches all `video/*` and `audio/*` types.
3. **Exact rejection** (lines 187-188): O(1) set lookup in `REJECTED_MIME_TYPES`.
4. **Default rejection** (line 191): Anything not in the map is rejected. This is the **safe default** -- unknown formats are never silently accepted.

**Design decision:** The fallback to "rejected" at line 191 is a deliberate security posture. The pipeline never processes a format it does not explicitly understand. The returned `format_name` for rejected types is the raw MIME string itself (e.g., `"application/octet-stream"`), providing diagnostic clarity.

**Test coverage:** `TestClassifyFormat` (test file lines 495-530) tests PDF, DOCX, PNG, MSG, XLSX, video, and unknown MIME types:

```python
def test_classify_pdf(self) -> None:
    assert _classify_format("application/pdf") == ("PDF", "direct")

def test_classify_unknown(self) -> None:
    fmt, category = _classify_format("application/octet-stream")
    assert category == "rejected"
```

---

### 5.2 `_check_exists_and_size()` -- Pre-Flight Guard

**Lines 146-155.** Returns an INVALID result dict if the file does not exist or is zero bytes; returns `None` if the file passes.

```python
def _check_exists_and_size(file_path: str) -> dict | None:
    p = Path(file_path)
    if not p.exists():
        log.warning("file_not_found", file_path=file_path)
        return _make_result(file_path, "INVALID", "File does not exist")
    if p.stat().st_size == 0:
        log.warning("zero_byte_file", file_path=file_path)
        return _make_result(file_path, "INVALID", "File is zero bytes")
    return None
```

**Design decision:** The `None`-return-on-success pattern is used throughout the module for early-exit checks. When the caller receives `None`, it proceeds to the next check. When it receives a dict, it short-circuits and returns that dict immediately.

**Test coverage:** `test_zero_byte_file_rejected` and `test_nonexistent_file_rejected` in `TestValidateFilePdf` (test file lines 174-191).

---

### 5.3 `_detect_mime_type()` -- libmagic Wrapper

**Lines 158-160.** A one-liner that wraps the `python-magic` library:

```python
def _detect_mime_type(file_path: str) -> str:
    return magic.from_file(file_path, mime=True)
```

This reads the file's magic bytes (not the extension) to determine the true MIME type. This is critical for security: a file named `contract.pdf` that is actually a JPEG will be correctly classified as `image/jpeg`.

**Watch out:** The `python-magic` package requires the system library `libmagic` to be installed. On macOS this is `brew install libmagic`; on Debian/Ubuntu it is `apt-get install libmagic1`.

---

### 5.4 `_validate_pdf()` -- Corruption & Password Check

**Note:** The earlier `_check_pdf_text_layer()` function has been removed. PDFs now always set `needs_ocr=True` because Mistral OCR handles both scanned and text-based PDFs uniformly. This simplifies the PDF validation path -- `_validate_pdf()` only checks for corruption and password protection, and all valid PDFs are routed through OCR.

---

### 5.5 `_validate_pdf()` -- Corruption & Password Check

**Lines 240-282.** PDF-specific validation that checks for corruption and password protection.

```python
def _validate_pdf(file_path: str) -> dict | None:
    try:
        reader = PdfReader(file_path)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return _make_result(file_path, "NEEDS_PASSWORD", "PDF is password-protected", "PDF")
        _ = len(reader.pages)  # Verify pages are accessible
    except FileNotDecryptedError:
        return _make_result(file_path, "NEEDS_PASSWORD", "PDF is password-protected", "PDF")
    except (PdfReadError, Exception) as exc:
        return _make_result(file_path, "INVALID", f"Corrupted PDF: {exc}", "PDF")
    return None
```

**Logic flow:**
1. Try to open with `PdfReader`.
2. If encrypted, try decrypting with an empty password (line 252). Some PDFs are "encrypted" but have no actual password. If the empty-password decrypt fails, return `NEEDS_PASSWORD`.
3. Try accessing `len(reader.pages)` at line 263 to trigger reading of the page tree. A corrupted PDF will throw here.
4. Catch `FileNotDecryptedError` (line 265) separately for password-protected PDFs.
5. Catch `PdfReadError` and generic exceptions (line 273) for corrupted files.

**Design decision:** The function returns `None` on success (following the early-exit pattern) rather than a "VALID" dict. This allows `validate_file()` to layer on additional checks (like the text-layer check) after PDF-specific validation passes.

---

### 5.6 `_validate_docx()` and `_validate_doc()` -- Word Document Checks

**Lines 290-336.**

**`_validate_docx()` (lines 290-307):** Opens the file with `python-docx` to verify structural integrity. If the constructor throws, the DOCX is corrupted.

```python
def _validate_docx(file_path: str) -> dict | None:
    try:
        from docx import Document
        Document(file_path)
    except Exception as exc:
        return _make_result(file_path, "INVALID", f"Corrupted DOCX: {exc}", "DOCX")
    return None
```

**`_validate_doc()` (lines 310-336):** Legacy `.doc` files use the OLE2 Compound Document format. Since `python-docx` cannot read `.doc` files, this function performs a basic magic-byte check:

```python
def _validate_doc(file_path: str) -> dict | None:
    try:
        with open(file_path, "rb") as fh:
            header = fh.read(8)
        if not header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return _make_result(file_path, "INVALID", "DOC file has invalid OLE2 header", "DOC")
    except Exception as exc:
        return _make_result(file_path, "INVALID", f"Corrupted DOC: {exc}", "DOC")
    return None
```

The magic bytes `\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1` are the OLE2 Compound Document header. This same check is reused for XLS files at lines 841-843.

---

### 5.7 `_extract_html_text()` -- HTML Parsing

**Lines 344-358.** Strips all HTML tags and returns plain text content.

```python
def _extract_html_text(file_path: str) -> str:
    try:
        raw = Path(file_path).read_text(encoding="utf-8", errors="replace")
        extractor = _HTMLTextExtractor()
        extractor.feed(raw)
        return extractor.get_text().strip()
    except Exception as exc:
        log.warning("html_text_extraction_failed", file_path=file_path, error=str(exc))
        return ""
```

This uses the `_HTMLTextExtractor` class (lines 81-93), a minimal subclass of stdlib's `HTMLParser`:

```python
class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._buf = StringIO()

    def handle_data(self, data: str) -> None:
        self._buf.write(data)

    def get_text(self) -> str:
        return self._buf.getvalue()
```

**Design decision:** The module uses `html.parser` from the standard library rather than BeautifulSoup or lxml. This avoids an extra dependency for what is a simple tag-stripping operation. The `errors="replace"` parameter on `read_text()` (line 352) handles encoding issues gracefully.

**How it is used:** `validate_file()` calls `_extract_html_text()` for HTML files at line 720. If the returned text is empty (line 721), the file is marked INVALID with reason "HTML file contains no visible text content."

**Test coverage:** `TestValidateFileHtml` (test file lines 285-313) tests both valid HTML (with text content) and empty HTML (structural tags only, no visible text).

---

### 5.8 `_extract_xlsx_tables()` -- Excel Table Extraction

**Lines 366-397.** Reads all sheets from an XLSX file using openpyxl and returns them as nested lists.

```python
def _extract_xlsx_tables(file_path: str) -> list[list[list[str]]]:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        tables: list[list[list[str]]] = []
        for sheet in wb.worksheets:
            rows: list[list[str]] = []
            for row in sheet.iter_rows(values_only=True):
                rows.append([str(cell) if cell is not None else "" for cell in row])
            tables.append(rows)
        wb.close()
        return tables
    except Exception as exc:
        log.warning("xlsx_read_failed", file_path=file_path, error=str(exc))
        return []
```

**Return type:** `list[list[list[str]]]` -- one entry per sheet, each entry is a list of rows, each row is a list of cell values as strings.

**Key flags:**
- `read_only=True` (line 386): Uses openpyxl's read-only mode for memory efficiency with large spreadsheets.
- `data_only=True` (line 386): Returns computed values rather than formulas. If a cell contains `=SUM(A1:A10)`, this returns the numeric result, not the formula string.

**How it is used:** `validate_file()` calls this at line 812 and checks `total_rows = sum(len(sheet) for sheet in tables)`. If `total_rows == 0` (line 814), the XLSX is INVALID.

**Test coverage:** `TestValidateFileXlsx` (test file lines 357-386) tests both a valid XLSX with data and an empty XLSX.

---

### 5.9 `_extract_msg_attachments()` -- Outlook MSG Extraction

**Lines 405-453.** Extracts the body text and all attachments from an Outlook `.msg` file.

```python
def _extract_msg_attachments(file_path: str, output_dir: str) -> list[str]:
    extracted_paths: list[str] = []
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        import extract_msg
        msg = extract_msg.Message(file_path)
        msg_name = Path(file_path).stem

        # Save body text.
        body = msg.body or ""
        if body.strip():
            body_path = out / f"{msg_name}_body.txt"
            body_path.write_text(body, encoding="utf-8")
            extracted_paths.append(str(body_path))

        # Save attachments.
        for attachment in msg.attachments:
            att_name = attachment.longFilename or attachment.shortFilename or "unnamed_attachment"
            att_path = out / att_name
            attachment.save(customPath=str(out), customFilename=att_name)
            extracted_paths.append(str(att_path))

        msg.close()
    except Exception as exc:
        log.warning("msg_extraction_failed", file_path=file_path, error=str(exc))

    return extracted_paths
```

**Key details:**
- The message body is saved as `{msg_stem}_body.txt` (line 436). This ensures the email body text is available for downstream processing.
- Attachment filenames prefer `longFilename` over `shortFilename` (line 443), falling back to `"unnamed_attachment"`.
- The function uses `extract-msg` library (lazy import at line 428).
- On any failure, it logs a warning and returns whatever was extracted so far (possibly an empty list).

---

### 5.10 `_extract_eml_attachments()` -- Email Attachment Extraction

**Lines 456-523.** Extracts the body and attachments from `.eml` files using Python's stdlib `email` module.

```python
def _extract_eml_attachments(file_path: str, output_dir: str) -> list[str]:
    extracted_paths: list[str] = []
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        raw = Path(file_path).read_bytes()
        msg = email.message_from_bytes(raw)
        eml_name = Path(file_path).stem

        # Extract plain-text body.
        body_parts: list[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                disposition = str(part.get("Content-Disposition", ""))
                if content_type == "text/plain" and "attachment" not in disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_parts.append(payload.decode(charset, errors="replace"))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body_parts.append(payload.decode(charset, errors="replace"))

        body_text = "\n".join(body_parts).strip()
        if body_text:
            body_path = out / f"{eml_name}_body.txt"
            body_path.write_text(body_text, encoding="utf-8")
            extracted_paths.append(str(body_path))

        # Extract attachments.
        if msg.is_multipart():
            for part in msg.walk():
                disposition = str(part.get("Content-Disposition", ""))
                if "attachment" in disposition:
                    att_name = part.get_filename() or "unnamed_attachment"
                    att_path = out / att_name
                    payload = part.get_payload(decode=True)
                    if payload:
                        att_path.write_bytes(payload)
                        extracted_paths.append(str(att_path))
    except Exception as exc:
        log.warning("eml_extraction_failed", file_path=file_path, error=str(exc))

    return extracted_paths
```

**Key details:**
- **Zero external dependencies:** Uses only Python's stdlib `email` module.
- **Body extraction** (lines 484-498): Walks the MIME tree. For multipart messages, it collects all `text/plain` parts that are NOT attachments (checked via `Content-Disposition`). For single-part messages, it reads the payload directly.
- **Charset handling** (lines 492-493): Falls back to UTF-8 if no charset is specified, with `errors="replace"` for resilience.
- **Attachment extraction** (lines 508-518): Walks the MIME tree again, this time looking for parts with `"attachment"` in their `Content-Disposition` header.

**Design decision:** The function makes **two passes** over the MIME tree: once for body text, once for attachments. This is simpler and more readable than trying to handle both in a single pass.

---

### 5.11 `_extract_zip_contents()` -- ZIP Extraction with Bomb Protection

**Lines 526-588.** Extracts all files from a ZIP archive with safety guards against zip bombs.

```python
def _extract_zip_contents(file_path: str, output_dir: str) -> list[str]:
    extracted_paths: list[str] = []
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            members = zf.infolist()

            # Zip-bomb guards.
            if len(members) > ZIP_MAX_FILES:
                log.warning("zip_bomb_file_count", ...)
                return []

            total_size = sum(m.file_size for m in members)
            if total_size > ZIP_MAX_TOTAL_BYTES:
                log.warning("zip_bomb_total_size", ...)
                return []

            zf.extractall(str(out))
            for member in members:
                if not member.is_dir():
                    extracted_paths.append(str(out / member.filename))
    except zipfile.BadZipFile as exc:
        log.warning("corrupted_zip", file_path=file_path, error=str(exc))
    except Exception as exc:
        log.warning("zip_extraction_failed", file_path=file_path, error=str(exc))

    return extracted_paths
```

**Zip-bomb protection (lines 553-571):**
1. **File count check** (line 554): Rejects archives with more than `ZIP_MAX_FILES` (100) entries.
2. **Total size check** (line 564): Sums the **uncompressed** size of all members via `m.file_size` and rejects if it exceeds `ZIP_MAX_TOTAL_BYTES` (500 MB).

Both checks happen **before** `extractall()` is called, so no bytes are written to disk if the limits are exceeded.

**Directory and junk filtering** (line 554): Only non-directory members that are not macOS junk files are included in the returned paths (`if not member.is_dir() and not _is_macos_junk(member.filename)`). The `_is_macos_junk()` helper (lines 45-53) excludes `__MACOSX` directories, `.DS_Store` files, and `._` resource fork files that macOS creates inside ZIP archives. This prevents these metadata artifacts from being treated as contract documents.

**How it is used in `validate_file()`** (lines 770-787): If `_extract_zip_contents()` returns an empty list, the ZIP is marked INVALID with reason "ZIP extraction failed or exceeded safety limits."

**Test coverage:** `TestValidateFileContainer` (test file lines 394-434) includes `test_zip_extracts_children` (normal ZIP with 2 files) and `test_zip_bomb_rejected` (mocked archive with 150 entries).

---

### 5.12 `validate_file()` -- Main Entry Point

**Lines 596-877.** This is the primary public function. It orchestrates the entire validation flow for a single file.

```python
def validate_file(file_path: str, extracted_from: str | None = None) -> dict:
```

**Parameters:**
- `file_path`: Path to the file to validate.
- `extracted_from`: If this file was extracted from a container, the parent container's filename. Used for provenance tracking.

**Flow (step by step):**

1. **Existence & size check** (lines 632-637):
   ```python
   err = _check_exists_and_size(file_path)
   if err is not None:
       if extracted_from:
           err["extracted_from"] = extracted_from
       return err
   ```

2. **MIME detection & classification** (lines 640-641):
   ```python
   mime = _detect_mime_type(file_path)
   format_name, category = _classify_format(mime)
   ```

3. **Reject unsupported types** (lines 644-652):
   ```python
   if category == "rejected":
       return _make_result(file_path, "REJECTED", f"Unsupported file type: {mime}", ...)
   ```

4. **DIRECT formats** (lines 636-715): Format-specific checks for PDF, DOCX, DOC, RTF, HTML.
   - **PDF** (lines 637-655): `_validate_pdf()` for corruption/password. All valid PDFs now always set `needs_ocr=True` -- the earlier text-layer threshold check has been removed since Mistral OCR handles both scanned and text-based PDFs.
   - **DOCX** (lines 657-669): `_validate_docx()` for corruption.
   - **DOC** (lines 671-683): `_validate_doc()` for OLE2 header.
   - **RTF** (lines 685-695): No structural validator; accepted if it exists and is non-zero.
   - **HTML** (lines 697-715): `_extract_html_text()` must return non-empty text.

5. **IMAGE formats** (lines 740-767): Always `needs_ocr=True`. TIFF files get an optional page-count check via Pillow.

6. **CONTAINER formats** (lines 748-787): Creates a temporary directory via `tempfile.mkdtemp()` (not a subdirectory of the source folder) to avoid polluting the original folder and inflating file counts on subsequent runs. Dispatches to `_extract_msg_attachments()`, `_extract_eml_attachments()`, or `_extract_zip_contents()`. Returns extracted child paths in `child_files`. macOS junk files (`__MACOSX`, `.DS_Store`, `._` resource forks) are automatically excluded during ZIP extraction.

7. **SPECIAL formats** (lines 810-867): XLSX gets `_extract_xlsx_tables()` with empty-data check. XLS gets OLE2 header check.

8. **Fallback** (lines 870-877): Should never be reached. Logs an error and returns REJECTED.

**Design decision on container extraction directory** (lines 752-753):
```python
parent_stem = Path(file_path).stem
extraction_dir = tempfile.mkdtemp(prefix=f"_extracted_{parent_stem}_")
```
Container children are now extracted into a system temp directory (via `tempfile.mkdtemp()`) rather than a subdirectory next to the source file. This avoids polluting the original folder and prevents extracted files from being re-discovered on subsequent `validate_folder()` runs. The `_extracted_` prefix in the temp directory name preserves diagnostic clarity.

---

### 5.13 `convert_to_pdf()` -- LibreOffice Headless Conversion

**Lines 880-970.** Converts non-PDF files to PDF format using LibreOffice in headless mode, with a graceful fallback.

```python
def convert_to_pdf(file_path: str, output_dir: str) -> str:
```

**Flow:**
1. **PDF passthrough** (lines 911-915): If the file is already a PDF, copy it to `output_dir` and return.
2. **LibreOffice conversion attempt** (lines 918-950):
   ```python
   result = subprocess.run(
       ["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", str(out), str(src)],
       capture_output=True, text=True, timeout=120,
   )
   ```
   - 120-second timeout prevents hanging on complex documents.
   - Catches `FileNotFoundError` if LibreOffice is not installed (line 951).
   - Catches `TimeoutExpired` (line 953).
3. **Fallback** (lines 958-970): If LibreOffice is unavailable or fails, copies the original file and writes a `.needs_conversion` marker file:
   ```python
   marker = dest.with_suffix(dest.suffix + ".needs_conversion")
   marker.write_text(f"Original format: {format_name}\nSource: {file_path}\n")
   ```

**Test coverage:** `TestConvertToPdf` (test file lines 538-568):
- `test_pdf_copied_as_is`: Verifies PDFs are copied verbatim.
- `test_docx_conversion_fallback`: Verifies the `.needs_conversion` marker is created when LibreOffice is absent.
- `test_output_directory_created`: Verifies nested output dirs are created.

---

### 5.14 `validate_folder()` -- Batch Processing

**Lines 973-1013.** Walks a directory recursively and validates every file.

```python
def validate_folder(folder_path: str) -> list[dict]:
    results: list[dict] = []
    root = Path(folder_path)

    if not root.is_dir():
        log.error("folder_not_found", folder_path=folder_path)
        return results

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune macOS metadata and leftover extraction directories.
        dirnames[:] = [
            d for d in dirnames
            if d not in _MACOS_JUNK_DIRS and not d.startswith("_extracted_")
        ]
        for fname in sorted(filenames):
            full_path = os.path.join(dirpath, fname)
            if _is_macos_junk(full_path):
                continue
            result = validate_file(full_path)
            results.append(result)

    log.info(
        "folder_validation_complete",
        folder_path=folder_path,
        total=len(results),
        valid=sum(1 for r in results if r["status"] == "VALID"),
        invalid=sum(1 for r in results if r["status"] == "INVALID"),
        needs_password=sum(1 for r in results if r["status"] == "NEEDS_PASSWORD"),
        rejected=sum(1 for r in results if r["status"] == "REJECTED"),
    )
    return results
```

**Key details:**
- Uses `os.walk()` for recursive traversal.
- **macOS junk file exclusion:** The `dirnames` list is pruned in-place to skip `__MACOSX` directories and leftover `_extracted_` directories. Individual files are checked via `_is_macos_junk()` to skip `.DS_Store`, `._` resource forks, and `Thumbs.db` files.
- Files within each directory are `sorted()` for deterministic ordering.
- **Container children are NOT re-validated.** The docstring explicitly states: "Files extracted from container formats (MSG, EML, ZIP) are not automatically re-validated here -- callers should inspect each result's `child_files` list and validate children separately if needed." This is a deliberate design choice to avoid infinite recursion (e.g., a ZIP inside a ZIP inside a ZIP).
- The summary log line reports counts of each status, which is useful for monitoring.

**Test coverage:** `TestValidateFolder` (test file lines 576-621):
- `test_folder_validation_mixed_files`: 4 files (PDF, DOCX, TXT, random binary) -> 2 VALID, 2 non-VALID.
- `test_folder_validation_empty_folder`: Empty directory returns `[]`.
- `test_folder_validation_nonexistent_folder`: Missing directory returns `[]` (no exception).
- `test_folder_validation_recursive`: Files in subdirectories are discovered.

---

## 6. Status Values Reference

| Status | Meaning | When Used |
|---|---|---|
| `VALID` | File passed all checks and is ready for processing | Healthy PDFs, DOCX, DOC, RTF, HTML (with text), images, containers (with children), XLSX/XLS with data |
| `INVALID` | File is unprocessable | Missing file, zero bytes, corrupted PDF/DOCX/DOC, empty HTML, empty XLSX, bad ZIP |
| `NEEDS_PASSWORD` | PDF is encrypted and cannot be decrypted | Password-protected PDFs |
| `REJECTED` | File type is not supported by the pipeline | Video, audio, executables, databases, unknown MIME types |

Note: A file with `status="VALID"` may still have `needs_ocr=True` (images, image-only PDFs). The `VALID` status means the file is processable, not that text has already been extracted.

---

## 7. Test Coverage Walkthrough

The test file at `tests/e2e/test_stage_0a_validation.py` is organized into 12 test classes:

| # | Class | Lines | What It Tests |
|---|---|---|---|
| 1 | `TestValidateFilePdf` | 104-249 | PDF with text, image-only PDF, zero-byte, nonexistent, corrupted, password-protected |
| 2 | `TestValidateFileDocx` | 257-278 | Valid DOCX, corrupted DOCX |
| 3 | `TestValidateFileHtml` | 285-313 | Valid HTML, empty HTML |
| 4 | `TestValidateFileImage` | 321-349 | PNG needs OCR, JPG needs OCR |
| 5 | `TestValidateFileXlsx` | 357-386 | Valid XLSX, empty XLSX |
| 6 | `TestValidateFileContainer` | 394-434 | ZIP extraction, zip-bomb rejection |
| 7 | `TestValidateFileRejected` | 442-466 | TXT rejected, CSV rejected, random binary rejected |
| 8 | `TestValidateFileProvenance` | 474-487 | `extracted_from` field set correctly, default is `None` |
| 9 | `TestClassifyFormat` | 495-530 | Direct unit tests for `_classify_format()` |
| 10 | `TestConvertToPdf` | 538-568 | PDF copy, DOCX fallback, output dir creation |
| 11 | `TestValidateFolder` | 576-621 | Mixed files, empty folder, nonexistent folder, recursive |
| 12 | `TestNewOutputFields` | 629-703 | All 7 keys present in VALID, INVALID, and NEEDS_PASSWORD results |

**Mocking strategy:** Tests extensively use `unittest.mock.patch` to control MIME detection and PDF reading. This is necessary because:
- libmagic may detect synthetic test files differently than real files.
- Minimal test PDFs have too few characters to pass the 50-char threshold, so `_check_pdf_text_layer` is mocked.
- Password-protected PDF testing requires simulating encryption without creating an actual encrypted file.

The test file imports internal helpers with graceful fallback (lines 25-48):

```python
try:
    from echelonos.stages.stage_0a_validation import _classify_format
except ImportError:
    _classify_format = None
```

Tests using these imports are guarded with `@pytest.mark.skipif(_classify_format is None, ...)`.

---

## 8. Shared Test Fixtures (conftest.py)

**File:** `tests/conftest.py` (186 lines).

### Minimal file byte constants

The conftest provides pre-computed byte sequences for creating test files:

| Constant | Lines | Description |
|---|---|---|
| `MINIMAL_PNG` | 14-19 | 1x1 white PNG pixel (~67 bytes) |
| `MINIMAL_JPG` | 22-43 | 1x1 white JPEG pixel (~283 bytes) |
| `MINIMAL_PDF_BYTES` | 46-77 | Single-page PDF with text "Test contract" |

### Fixtures

| Fixture | Lines | Returns | Description |
|---|---|---|---|
| `tmp_org_folder` | 86-90 | `Path` | Temp directory named `test_org` inside `tmp_path` |
| `sample_pdf` | 94-98 | `Path` | Minimal PDF written to `tmp_org_folder/sample.pdf` |
| `sample_docx` | 102-110 | `Path` | DOCX created with python-docx, one paragraph |
| `zero_byte_file` | 114-118 | `Path` | Empty file at `tmp_org_folder/empty.pdf` |
| `sample_html` | 127-142 | `Path` | HTML with heading and two paragraphs |
| `sample_xlsx` | 146-157 | `Path` | XLSX with 3 columns, 2 data rows |
| `sample_png` | 161-165 | `Path` | 1x1 PNG from `MINIMAL_PNG` |
| `sample_jpg` | 169-173 | `Path` | 1x1 JPG from `MINIMAL_JPG` |
| `sample_zip` | 177-185 | `Path` | ZIP containing a PDF and a TXT file |

All file-creating fixtures depend on `tmp_org_folder`, which itself depends on pytest's `tmp_path`. This ensures every test gets a fresh, isolated directory.

---

## 9. Key Takeaways

1. **Fail-safe defaults.** Unknown formats are rejected (never silently accepted). PDF text-layer check failures default to "needs OCR." This is a security-first design.

2. **Consistent output schema.** Every code path returns the same 7-key dictionary. This makes downstream stages simple: they always know what keys to expect.

3. **Separation of concerns.** Each format has its own validation function. The main `validate_file()` function is a clean dispatcher -- it detects the format, then delegates to the appropriate handler.

4. **Lazy imports for optional dependencies.** Heavy libraries are imported only when needed, keeping the module loadable even if some deps are missing.

5. **Provenance tracking.** The `extracted_from` and `child_files` fields create a tree structure that lets downstream stages trace any file back to its original container.

6. **Zip-bomb protection is pre-extraction.** Both the file count and total size checks happen before any bytes are written to disk.

---

## 10. Watch Out For

1. **libmagic system dependency.** The `python-magic` package requires the `libmagic` C library installed at the system level. Tests will fail if it is missing. On macOS: `brew install libmagic`. On Debian/Ubuntu: `apt-get install libmagic1`.

2. **PDFs always route through OCR.** The earlier per-page character threshold (`PDF_TEXT_CHAR_THRESHOLD`) has been removed. All valid PDFs now set `needs_ocr=True` and are processed by Mistral OCR. This simplifies the pipeline at the cost of running OCR on PDFs that already have a text layer, but Mistral OCR handles text-based PDFs efficiently.

3. **Container children need separate validation.** `validate_folder()` does NOT recursively validate children extracted from containers. The caller must iterate `child_files` and call `validate_file()` on each child. Forgetting this step means extracted attachments skip validation entirely.

4. **LibreOffice availability.** `convert_to_pdf()` gracefully falls back to copying + a marker file when LibreOffice is not installed. But if your deployment expects actual PDF conversion, ensure LibreOffice is available in the runtime environment.

5. **RTF has no structural validation.** Unlike PDF and DOCX, RTF files are accepted based solely on existence and non-zero size (lines 707-717). A corrupted RTF file will pass validation and may fail downstream.

6. **MIME detection vs. file extension.** The module uses `python-magic` (file content) not the file extension. A file named `contract.pdf` that is actually a JPEG will be classified as `image/jpeg`. This is correct behavior, but it means test files need valid magic bytes, not just the right extension. Many tests mock `_detect_mime_type` for this reason.

7. **XLS validation is minimal.** Legacy `.xls` files only get an OLE2 header check (8 bytes). A file with a valid header but corrupted content will pass Stage 0a and may fail in downstream stages.

8. **The `NEEDS_PASSWORD` status is PDF-only.** No other format has password-protection detection. An encrypted DOCX (which is actually a ZIP with encryption) would likely be caught as a corrupted DOCX or might pass validation and fail downstream.

9. **`_make_result()` and the `extracted_from` patching pattern.** When an error occurs for an extracted file (lines 635-637, 661-662, 682-683), the code manually patches `err["extracted_from"] = extracted_from` on the returned dict. This is because the early-exit error functions do not always have access to the `extracted_from` parameter. Be careful if adding new early-exit paths -- they must also include this patching.
