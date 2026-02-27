"""Stage 0a: File Validation Gate.

Validates incoming contract files before they enter the extraction pipeline.
Checks for file existence, size, MIME type, corruption, and password protection.
"""

import os
import shutil
from pathlib import Path

import magic
import structlog
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError

log = structlog.get_logger(__name__)

# Accepted MIME types for contract documents.
ACCEPTED_MIME_TYPES: dict[str, str] = {
    "application/pdf": "PDF",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
    "application/msword": "DOC",
}


def _make_result(
    file_path: str,
    status: str,
    reason: str,
    original_format: str = "",
) -> dict:
    """Build a standardised validation result dict."""
    return {
        "file_path": file_path,
        "status": status,
        "reason": reason,
        "original_format": original_format,
    }


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


def _check_mime_type(file_path: str) -> tuple[str | None, dict | None]:
    """Check the MIME type is one we accept.

    Returns (original_format, error_result).  If error_result is not None the
    file is INVALID.
    """
    mime = _detect_mime_type(file_path)
    if mime not in ACCEPTED_MIME_TYPES:
        log.warning("unsupported_mime_type", file_path=file_path, mime_type=mime)
        return None, _make_result(
            file_path,
            "INVALID",
            f"Unsupported file type: {mime}",
        )
    return ACCEPTED_MIME_TYPES[mime], None


def _validate_pdf(file_path: str) -> dict | None:
    """PDF-specific checks: corruption and password protection.

    Returns a result dict only when the file is NOT valid (INVALID or
    NEEDS_PASSWORD).  Returns None when the PDF passes all checks.
    """
    try:
        reader = PdfReader(file_path)

        # Check for encryption / password protection.
        if reader.is_encrypted:
            # Try to decrypt with an empty password (some PDFs have owner-only
            # restrictions but can be opened without a user password).
            try:
                reader.decrypt("")
                # If decryption succeeds with empty password, treat as valid
                # but still flag it so downstream stages are aware.
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


def _validate_docx(file_path: str) -> dict | None:
    """DOCX-specific corruption check.

    Returns a result dict if the file is INVALID, else None.
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
    basic magic-byte sanity check only.  Returns a result dict if INVALID, else
    None.
    """
    try:
        with open(file_path, "rb") as fh:
            header = fh.read(8)
        # OLE2 Compound Document magic bytes
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
# Public API
# ---------------------------------------------------------------------------


def validate_file(file_path: str) -> dict:
    """Validate a single file for the extraction pipeline.

    Parameters
    ----------
    file_path:
        Absolute or relative path to the file to validate.

    Returns
    -------
    dict with keys:
        - file_path  (str)
        - status     ("VALID" | "INVALID" | "NEEDS_PASSWORD")
        - reason     (str)
        - original_format (str)  e.g. "PDF", "DOCX", "DOC"
    """
    log.info("validating_file", file_path=file_path)

    # 1. Existence & size check.
    err = _check_exists_and_size(file_path)
    if err is not None:
        return err

    # 2. MIME type check.
    original_format, err = _check_mime_type(file_path)
    if err is not None:
        return err

    # 3. Format-specific corruption / password checks.
    if original_format == "PDF":
        err = _validate_pdf(file_path)
        if err is not None:
            return err
    elif original_format == "DOCX":
        err = _validate_docx(file_path)
        if err is not None:
            return err
    elif original_format == "DOC":
        err = _validate_doc(file_path)
        if err is not None:
            return err

    log.info("file_valid", file_path=file_path, original_format=original_format)
    return _make_result(file_path, "VALID", "File passed all validation checks", original_format)


def convert_to_pdf(file_path: str, output_dir: str) -> str:
    """Prepare a file for the downstream PDF-based pipeline.

    * **PDF files** are copied as-is into *output_dir*.
    * **DOCX / DOC files** are verified and flagged for conversion.  Actual
      conversion to PDF would typically be performed by a system tool such as
      LibreOffice; for now the original file is copied and a ``*.needs_conversion``
      marker is written alongside it.

    Parameters
    ----------
    file_path:
        Path to the validated source file.
    output_dir:
        Directory where the processed file should be placed.

    Returns
    -------
    str – path to the processed (or copied) file inside *output_dir*.
    """
    src = Path(file_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    mime = _detect_mime_type(file_path)
    original_format = ACCEPTED_MIME_TYPES.get(mime, "UNKNOWN")

    if original_format == "PDF":
        dest = out / src.name
        shutil.copy2(str(src), str(dest))
        log.info("pdf_copied", src=file_path, dest=str(dest))
        return str(dest)

    # DOCX / DOC – copy and create a marker file for deferred conversion.
    dest = out / src.name
    shutil.copy2(str(src), str(dest))

    marker = dest.with_suffix(dest.suffix + ".needs_conversion")
    marker.write_text(f"Original format: {original_format}\nSource: {file_path}\n")
    log.info(
        "file_flagged_for_conversion",
        src=file_path,
        dest=str(dest),
        original_format=original_format,
    )
    return str(dest)


def validate_folder(folder_path: str) -> list[dict]:
    """Walk *folder_path* recursively and validate every file found.

    Parameters
    ----------
    folder_path:
        Path to the root folder to scan.

    Returns
    -------
    list[dict] – one validation result per file discovered.
    """
    results: list[dict] = []
    root = Path(folder_path)

    if not root.is_dir():
        log.error("folder_not_found", folder_path=folder_path)
        return results

    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in sorted(filenames):
            full_path = os.path.join(dirpath, fname)
            result = validate_file(full_path)
            results.append(result)

    log.info(
        "folder_validation_complete",
        folder_path=folder_path,
        total=len(results),
        valid=sum(1 for r in results if r["status"] == "VALID"),
        invalid=sum(1 for r in results if r["status"] == "INVALID"),
        needs_password=sum(1 for r in results if r["status"] == "NEEDS_PASSWORD"),
    )
    return results
