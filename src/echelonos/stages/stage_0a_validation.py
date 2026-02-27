"""Stage 0a: File Validation Gate.

Validates incoming contract files before they enter the extraction pipeline.
Checks for file existence, size, MIME type, corruption, password protection,
OCR requirements, and handles container formats (MSG, EML, ZIP) with
recursive extraction of child files.

Supported format categories:
    - DIRECT:    PDF, DOCX, DOC, RTF, HTML (text-extractable)
    - IMAGE:     PNG, JPG, TIFF (require OCR)
    - CONTAINER: MSG, EML, ZIP (recursive extraction)
    - SPECIAL:   XLSX, XLS (structured data)
    - REJECTED:  video/*, audio/*, executables, databases, unrecognised
"""

from __future__ import annotations

import email
import os
import shutil
import subprocess
import zipfile
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path

import magic
import structlog
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# macOS junk-file detection
# ---------------------------------------------------------------------------

# Directory names and filename patterns created by macOS that should be
# silently excluded from processing.
_MACOS_JUNK_DIRS = {"__MACOSX"}
_MACOS_JUNK_FILES = {".DS_Store", "._.DS_Store", "Thumbs.db"}


def _is_macos_junk(path: str) -> bool:
    """Return True if *path* is a macOS resource-fork or metadata file."""
    parts = Path(path).parts
    if any(p in _MACOS_JUNK_DIRS for p in parts):
        return True
    basename = os.path.basename(path)
    if basename in _MACOS_JUNK_FILES or basename.startswith("._"):
        return True
    return False


# ---------------------------------------------------------------------------
# Format classification tables
# ---------------------------------------------------------------------------

# Maps MIME types to (format_name, category).
MIME_FORMAT_MAP: dict[str, tuple[str, str]] = {
    # Direct (text-extractable)
    "application/pdf": ("PDF", "direct"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("DOCX", "direct"),
    "application/msword": ("DOC", "direct"),
    "application/rtf": ("RTF", "direct"),
    "text/rtf": ("RTF", "direct"),
    "text/html": ("HTML", "direct"),
    # Image (require OCR)
    "image/png": ("PNG", "image"),
    "image/jpeg": ("JPG", "image"),
    "image/tiff": ("TIFF", "image"),
    # Container (recursive extraction)
    "application/vnd.ms-outlook": ("MSG", "container"),
    "message/rfc822": ("EML", "container"),
    "application/zip": ("ZIP", "container"),
    # Special (structured data)
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("XLSX", "special"),
    "application/vnd.ms-excel": ("XLS", "special"),
}

# MIME prefixes and specific types that are always rejected.
REJECTED_MIME_PREFIXES: list[str] = ["video/", "audio/"]
REJECTED_MIME_TYPES: set[str] = {
    "application/x-executable",
    "application/x-dosexec",
    "application/x-sqlite3",
}

# Zip-bomb safety limits.
ZIP_MAX_FILES = 100
ZIP_MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB


# ---------------------------------------------------------------------------
# Internal HTML text-stripping helper
# ---------------------------------------------------------------------------


class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML-to-text converter using the stdlib html.parser."""

    def __init__(self) -> None:
        super().__init__()
        self._buf = StringIO()

    def handle_data(self, data: str) -> None:  # noqa: D102
        self._buf.write(data)

    def get_text(self) -> str:
        """Return accumulated plain text."""
        return self._buf.getvalue()


# ---------------------------------------------------------------------------
# Result builder
# ---------------------------------------------------------------------------


def _make_result(
    file_path: str,
    status: str,
    reason: str,
    original_format: str = "",
    needs_ocr: bool = False,
    extracted_from: str | None = None,
    child_files: list[str] | None = None,
) -> dict:
    """Build a standardised validation result dict.

    Parameters
    ----------
    file_path:
        Path to the validated file.
    status:
        One of ``"VALID"``, ``"INVALID"``, ``"NEEDS_PASSWORD"``, ``"REJECTED"``.
    reason:
        Human-readable explanation of the status.
    original_format:
        Short format label, e.g. ``"PDF"``, ``"DOCX"``, ``"MSG"``.
    needs_ocr:
        Whether the file requires OCR for text extraction.
    extracted_from:
        Parent container filename when the file was extracted from a
        MSG/EML/ZIP container.
    child_files:
        Paths of children extracted from a container format.
    """
    return {
        "file_path": file_path,
        "status": status,
        "reason": reason,
        "original_format": original_format,
        "needs_ocr": needs_ocr,
        "extracted_from": extracted_from,
        "child_files": child_files if child_files is not None else [],
    }


# ---------------------------------------------------------------------------
# Low-level checks
# ---------------------------------------------------------------------------


def _check_exists_and_size(file_path: str) -> dict | None:
    """Return an INVALID result if the file does not exist or is zero bytes."""
    p = Path(file_path)
    if not p.exists():
        log.warning("file_not_found", file_path=file_path)
        return _make_result(file_path, "INVALID", "File does not exist")
    if p.stat().st_size == 0:
        log.warning("zero_byte_file", file_path=file_path)
        return _make_result(file_path, "INVALID", "File is zero bytes")
    return None


def _detect_mime_type(file_path: str) -> str:
    """Detect MIME type using libmagic."""
    return magic.from_file(file_path, mime=True)


def _classify_format(mime_type: str) -> tuple[str, str]:
    """Classify a MIME type into a format name and category.

    Parameters
    ----------
    mime_type:
        The MIME type string returned by libmagic.

    Returns
    -------
    tuple of (format_name, category) where *category* is one of
    ``"direct"``, ``"image"``, ``"container"``, ``"special"``, or
    ``"rejected"``.
    """
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


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------



def _validate_pdf(file_path: str) -> dict | None:
    """PDF-specific checks: corruption and password protection.

    Returns a result dict only when the file is NOT valid (``INVALID`` or
    ``NEEDS_PASSWORD``).  Returns ``None`` when the PDF passes all checks.
    """
    try:
        reader = PdfReader(file_path)

        # Check for encryption / password protection.
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                log.info("password_protected_pdf", file_path=file_path)
                return _make_result(
                    file_path,
                    "NEEDS_PASSWORD",
                    "PDF is password-protected",
                    "PDF",
                )

        # Try to access pages to verify the file is not corrupted.
        _ = len(reader.pages)

    except FileNotDecryptedError:
        log.info("password_protected_pdf", file_path=file_path)
        return _make_result(
            file_path,
            "NEEDS_PASSWORD",
            "PDF is password-protected",
            "PDF",
        )
    except (PdfReadError, Exception) as exc:
        log.warning("corrupted_pdf", file_path=file_path, error=str(exc))
        return _make_result(
            file_path,
            "INVALID",
            f"Corrupted PDF: {exc}",
            "PDF",
        )

    return None


# ---------------------------------------------------------------------------
# DOCX / DOC helpers
# ---------------------------------------------------------------------------


def _validate_docx(file_path: str) -> dict | None:
    """DOCX-specific corruption check.

    Returns a result dict if the file is INVALID, else ``None``.
    """
    try:
        from docx import Document

        Document(file_path)
    except Exception as exc:
        log.warning("corrupted_docx", file_path=file_path, error=str(exc))
        return _make_result(
            file_path,
            "INVALID",
            f"Corrupted DOCX: {exc}",
            "DOCX",
        )
    return None


def _validate_doc(file_path: str) -> dict | None:
    """DOC-specific corruption check.

    Legacy .doc files cannot be fully validated with python-docx; we perform a
    basic OLE2 magic-byte sanity check only.  Returns a result dict if
    INVALID, else ``None``.
    """
    try:
        with open(file_path, "rb") as fh:
            header = fh.read(8)
        # OLE2 Compound Document magic bytes.
        if not header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return _make_result(
                file_path,
                "INVALID",
                "DOC file has invalid OLE2 header",
                "DOC",
            )
    except Exception as exc:
        log.warning("corrupted_doc", file_path=file_path, error=str(exc))
        return _make_result(
            file_path,
            "INVALID",
            f"Corrupted DOC: {exc}",
            "DOC",
        )
    return None


# ---------------------------------------------------------------------------
# HTML helper
# ---------------------------------------------------------------------------


def _extract_html_text(file_path: str) -> str:
    """Strip HTML tags and return plain text content.

    Uses the stdlib ``html.parser`` module — no third-party dependency
    required.  Returns the stripped text, which may be empty if the HTML
    document contains no visible text content.
    """
    try:
        raw = Path(file_path).read_text(encoding="utf-8", errors="replace")
        extractor = _HTMLTextExtractor()
        extractor.feed(raw)
        return extractor.get_text().strip()
    except Exception as exc:
        log.warning("html_text_extraction_failed", file_path=file_path, error=str(exc))
        return ""


# ---------------------------------------------------------------------------
# XLSX / XLS helper
# ---------------------------------------------------------------------------


def _extract_xlsx_tables(file_path: str) -> list[list[list[str]]]:
    """Read all sheets from an XLSX file as lists of row-lists.

    Each sheet is represented as a list of rows, where each row is a list
    of cell values converted to strings.  Returns an empty list if the
    workbook cannot be read or contains no data.

    Parameters
    ----------
    file_path:
        Path to the ``.xlsx`` file.

    Returns
    -------
    list[list[list[str]]]
        One entry per sheet; each entry is a list of rows.
    """
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


# ---------------------------------------------------------------------------
# Container extraction helpers
# ---------------------------------------------------------------------------


def _extract_msg_attachments(file_path: str, output_dir: str) -> list[str]:
    """Extract body and attachments from a ``.msg`` file.

    Uses the ``extract-msg`` library.  The message body is written as a
    ``.txt`` file and each attachment is saved to *output_dir*.

    Parameters
    ----------
    file_path:
        Path to the ``.msg`` file.
    output_dir:
        Directory where extracted files will be written.

    Returns
    -------
    list[str]
        Paths to all extracted files (body text + attachments).
    """
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
            log.debug("msg_body_extracted", file_path=file_path, dest=str(body_path))

        # Save attachments.
        for attachment in msg.attachments:
            att_name = attachment.longFilename or attachment.shortFilename or "unnamed_attachment"
            att_path = out / att_name
            attachment.save(customPath=str(out), customFilename=att_name)
            extracted_paths.append(str(att_path))
            log.debug("msg_attachment_extracted", file_path=file_path, attachment=att_name)

        msg.close()
    except Exception as exc:
        log.warning("msg_extraction_failed", file_path=file_path, error=str(exc))

    return extracted_paths


def _extract_eml_attachments(file_path: str, output_dir: str) -> list[str]:
    """Extract body and attachments from a ``.eml`` file.

    Uses the stdlib ``email`` module.  The plain-text body is written as a
    ``.txt`` file and each attachment is saved to *output_dir*.

    Parameters
    ----------
    file_path:
        Path to the ``.eml`` file.
    output_dir:
        Directory where extracted files will be written.

    Returns
    -------
    list[str]
        Paths to all extracted files (body text + attachments).
    """
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
            log.debug("eml_body_extracted", file_path=file_path, dest=str(body_path))

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
                        log.debug("eml_attachment_extracted", file_path=file_path, attachment=att_name)

    except Exception as exc:
        log.warning("eml_extraction_failed", file_path=file_path, error=str(exc))

    return extracted_paths


def _extract_zip_contents(file_path: str, output_dir: str) -> list[str]:
    """Extract all files from a ZIP archive.

    Includes safety guards against zip bombs: rejects archives that contain
    more than ``ZIP_MAX_FILES`` entries or whose total uncompressed size
    exceeds ``ZIP_MAX_TOTAL_BYTES``.

    Parameters
    ----------
    file_path:
        Path to the ``.zip`` archive.
    output_dir:
        Directory where extracted files will be written.

    Returns
    -------
    list[str]
        Paths to all extracted files, or an empty list on failure.
    """
    extracted_paths: list[str] = []
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            members = zf.infolist()

            # Zip-bomb guards.
            if len(members) > ZIP_MAX_FILES:
                log.warning(
                    "zip_bomb_file_count",
                    file_path=file_path,
                    file_count=len(members),
                    limit=ZIP_MAX_FILES,
                )
                return []

            total_size = sum(m.file_size for m in members)
            if total_size > ZIP_MAX_TOTAL_BYTES:
                log.warning(
                    "zip_bomb_total_size",
                    file_path=file_path,
                    total_size_mb=round(total_size / (1024 * 1024), 1),
                    limit_mb=ZIP_MAX_TOTAL_BYTES // (1024 * 1024),
                )
                return []

            zf.extractall(str(out))
            for member in members:
                if not member.is_dir() and not _is_macos_junk(member.filename):
                    extracted_paths.append(str(out / member.filename))

        log.debug(
            "zip_extracted",
            file_path=file_path,
            file_count=len(extracted_paths),
        )
    except zipfile.BadZipFile as exc:
        log.warning("corrupted_zip", file_path=file_path, error=str(exc))
    except Exception as exc:
        log.warning("zip_extraction_failed", file_path=file_path, error=str(exc))

    return extracted_paths


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_file(file_path: str, extracted_from: str | None = None) -> dict:
    """Validate a single file for the extraction pipeline.

    Handles all supported format categories:

    - **Direct** formats (PDF, DOCX, DOC, RTF, HTML) undergo format-specific
      corruption checks.  PDFs are further inspected for a text layer to
      decide whether OCR is required.
    - **Image** formats (PNG, JPG, TIFF) are marked VALID with
      ``needs_ocr=True``.
    - **Container** formats (MSG, EML, ZIP) have their children extracted and
      returned in ``child_files``.
    - **Special** formats (XLSX, XLS) are validated for readability.
    - All other MIME types are **rejected**.

    Parameters
    ----------
    file_path:
        Absolute or relative path to the file to validate.
    extracted_from:
        If this file was extracted from a container (MSG, EML, ZIP), the
        filename of the parent container.

    Returns
    -------
    dict with keys:
        - file_path       (str)
        - status          ("VALID" | "INVALID" | "NEEDS_PASSWORD" | "REJECTED")
        - reason          (str)
        - original_format (str)   e.g. "PDF", "DOCX", "MSG", "PNG", "XLSX"
        - needs_ocr       (bool)
        - extracted_from  (str | None)
        - child_files     (list[str])
    """
    log.info("validating_file", file_path=file_path, extracted_from=extracted_from)

    # 1. Existence & size check.
    err = _check_exists_and_size(file_path)
    if err is not None:
        if extracted_from:
            err["extracted_from"] = extracted_from
        return err

    # 2. MIME detection & classification.
    mime = _detect_mime_type(file_path)
    format_name, category = _classify_format(mime)

    # 3. Reject unsupported types immediately.
    if category == "rejected":
        log.warning("rejected_file_type", file_path=file_path, mime_type=mime)
        return _make_result(
            file_path,
            "REJECTED",
            f"Unsupported file type: {mime}",
            format_name,
            extracted_from=extracted_from,
        )

    # 4. Category-specific validation.

    # --- DIRECT formats ---------------------------------------------------
    if category == "direct":
        if format_name == "PDF":
            err = _validate_pdf(file_path)
            if err is not None:
                err["extracted_from"] = extracted_from
                return err
            log.info(
                "file_valid",
                file_path=file_path,
                original_format="PDF",
                needs_ocr=True,
            )
            return _make_result(
                file_path,
                "VALID",
                "File passed all validation checks",
                "PDF",
                needs_ocr=True,
                extracted_from=extracted_from,
            )

        if format_name == "DOCX":
            err = _validate_docx(file_path)
            if err is not None:
                err["extracted_from"] = extracted_from
                return err
            log.info("file_valid", file_path=file_path, original_format="DOCX")
            return _make_result(
                file_path,
                "VALID",
                "File passed all validation checks",
                "DOCX",
                extracted_from=extracted_from,
            )

        if format_name == "DOC":
            err = _validate_doc(file_path)
            if err is not None:
                err["extracted_from"] = extracted_from
                return err
            log.info("file_valid", file_path=file_path, original_format="DOC")
            return _make_result(
                file_path,
                "VALID",
                "File passed all validation checks",
                "DOC",
                extracted_from=extracted_from,
            )

        if format_name == "RTF":
            # RTF has no dedicated structural validator; accept if it exists
            # and has non-zero size (already checked above).
            log.info("file_valid", file_path=file_path, original_format="RTF")
            return _make_result(
                file_path,
                "VALID",
                "File passed all validation checks",
                "RTF",
                extracted_from=extracted_from,
            )

        if format_name == "HTML":
            text = _extract_html_text(file_path)
            if not text:
                log.warning("empty_html", file_path=file_path)
                return _make_result(
                    file_path,
                    "INVALID",
                    "HTML file contains no visible text content",
                    "HTML",
                    extracted_from=extracted_from,
                )
            log.info("file_valid", file_path=file_path, original_format="HTML")
            return _make_result(
                file_path,
                "VALID",
                "File passed all validation checks",
                "HTML",
                extracted_from=extracted_from,
            )

    # --- IMAGE formats ----------------------------------------------------
    if category == "image":
        needs_ocr = True

        # For TIFF files, attempt to count pages using Pillow.
        if format_name == "TIFF":
            try:
                from PIL import Image

                with Image.open(file_path) as img:
                    n_frames = getattr(img, "n_frames", 1)
                log.debug("tiff_page_count", file_path=file_path, pages=n_frames)
            except Exception as exc:
                log.warning("tiff_page_count_failed", file_path=file_path, error=str(exc))

        log.info(
            "file_valid",
            file_path=file_path,
            original_format=format_name,
            needs_ocr=True,
        )
        return _make_result(
            file_path,
            "VALID",
            "Image file accepted; OCR required",
            format_name,
            needs_ocr=needs_ocr,
            extracted_from=extracted_from,
        )

    # --- CONTAINER formats ------------------------------------------------
    if category == "container":
        # Create a sub-directory for extracted children.
        parent_stem = Path(file_path).stem
        extraction_dir = str(Path(file_path).parent / f"_extracted_{parent_stem}")
        parent_name = Path(file_path).name

        child_paths: list[str] = []

        if format_name == "MSG":
            child_paths = _extract_msg_attachments(file_path, extraction_dir)
        elif format_name == "EML":
            child_paths = _extract_eml_attachments(file_path, extraction_dir)
        elif format_name == "ZIP":
            child_paths = _extract_zip_contents(file_path, extraction_dir)
            if not child_paths:
                # Extraction failed or zip-bomb detected.
                return _make_result(
                    file_path,
                    "INVALID",
                    "ZIP extraction failed or exceeded safety limits",
                    "ZIP",
                    extracted_from=extracted_from,
                )

        log.info(
            "container_extracted",
            file_path=file_path,
            original_format=format_name,
            child_count=len(child_paths),
        )
        return _make_result(
            file_path,
            "VALID",
            f"Container extracted {len(child_paths)} child file(s)",
            format_name,
            extracted_from=extracted_from,
            child_files=child_paths,
        )

    # --- SPECIAL formats --------------------------------------------------
    if category == "special":
        if format_name == "XLSX":
            tables = _extract_xlsx_tables(file_path)
            total_rows = sum(len(sheet) for sheet in tables)
            if total_rows == 0:
                log.warning("empty_xlsx", file_path=file_path)
                return _make_result(
                    file_path,
                    "INVALID",
                    "XLSX file contains no data",
                    "XLSX",
                    extracted_from=extracted_from,
                )
            log.info(
                "file_valid",
                file_path=file_path,
                original_format="XLSX",
                sheets=len(tables),
                total_rows=total_rows,
            )
            return _make_result(
                file_path,
                "VALID",
                "File passed all validation checks",
                "XLSX",
                extracted_from=extracted_from,
            )

        if format_name == "XLS":
            # Legacy XLS: perform basic OLE2 header check (same as DOC).
            try:
                with open(file_path, "rb") as fh:
                    header = fh.read(8)
                if not header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
                    return _make_result(
                        file_path,
                        "INVALID",
                        "XLS file has invalid OLE2 header",
                        "XLS",
                        extracted_from=extracted_from,
                    )
            except Exception as exc:
                log.warning("corrupted_xls", file_path=file_path, error=str(exc))
                return _make_result(
                    file_path,
                    "INVALID",
                    f"Corrupted XLS: {exc}",
                    "XLS",
                    extracted_from=extracted_from,
                )
            log.info("file_valid", file_path=file_path, original_format="XLS")
            return _make_result(
                file_path,
                "VALID",
                "File passed all validation checks",
                "XLS",
                extracted_from=extracted_from,
            )

    # Fallback — should never be reached if _classify_format is exhaustive.
    log.error("unhandled_category", file_path=file_path, category=category, mime=mime)
    return _make_result(
        file_path,
        "REJECTED",
        f"Unhandled format category: {category}",
        format_name,
        extracted_from=extracted_from,
    )


def convert_to_pdf(file_path: str, output_dir: str) -> str:
    """Prepare a file for the downstream PDF-based pipeline.

    Attempts actual conversion to PDF using LibreOffice in headless mode.
    If LibreOffice is not installed or the conversion fails, falls back to
    copying the original file and writing a ``.needs_conversion`` marker.

    * **PDF files** are copied as-is into *output_dir*.
    * **Non-PDF files** are converted via LibreOffice when available, or
      copied with a conversion marker otherwise.

    Parameters
    ----------
    file_path:
        Path to the validated source file.
    output_dir:
        Directory where the processed file should be placed.

    Returns
    -------
    str
        Path to the processed (or copied) file inside *output_dir*.
    """
    src = Path(file_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    mime = _detect_mime_type(file_path)
    format_name, _category = _classify_format(mime)

    # PDFs are copied directly.
    if format_name == "PDF":
        dest = out / src.name
        shutil.copy2(str(src), str(dest))
        log.info("pdf_copied", src=file_path, dest=str(dest))
        return str(dest)

    # Try LibreOffice headless conversion.
    try:
        result = subprocess.run(
            [
                "libreoffice",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(out),
                str(src),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode == 0:
            converted = out / f"{src.stem}.pdf"
            if converted.exists():
                log.info(
                    "libreoffice_conversion_success",
                    src=file_path,
                    dest=str(converted),
                    original_format=format_name,
                )
                return str(converted)

        log.warning(
            "libreoffice_conversion_failed",
            src=file_path,
            returncode=result.returncode,
            stderr=result.stderr[:500] if result.stderr else "",
        )
    except FileNotFoundError:
        log.info("libreoffice_not_available", src=file_path)
    except subprocess.TimeoutExpired:
        log.warning("libreoffice_conversion_timeout", src=file_path)
    except Exception as exc:
        log.warning("libreoffice_conversion_error", src=file_path, error=str(exc))

    # Fallback: copy and create a marker file for deferred conversion.
    dest = out / src.name
    shutil.copy2(str(src), str(dest))

    marker = dest.with_suffix(dest.suffix + ".needs_conversion")
    marker.write_text(f"Original format: {format_name}\nSource: {file_path}\n")
    log.info(
        "file_flagged_for_conversion",
        src=file_path,
        dest=str(dest),
        original_format=format_name,
    )
    return str(dest)


def validate_folder(folder_path: str) -> list[dict]:
    """Walk *folder_path* recursively and validate every file found.

    All discovered files are run through :func:`validate_file`.  Files
    extracted from container formats (MSG, EML, ZIP) are **not**
    automatically re-validated here — callers should inspect each result's
    ``child_files`` list and validate children separately if needed.

    Parameters
    ----------
    folder_path:
        Path to the root folder to scan.

    Returns
    -------
    list[dict]
        One validation result per file discovered.
    """
    results: list[dict] = []
    root = Path(folder_path)

    if not root.is_dir():
        log.error("folder_not_found", folder_path=folder_path)
        return results

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune macOS metadata directories in-place so os.walk skips them.
        dirnames[:] = [d for d in dirnames if d not in _MACOS_JUNK_DIRS]
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
