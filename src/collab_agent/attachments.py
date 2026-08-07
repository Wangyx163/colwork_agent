from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import tempfile
from pathlib import Path
from typing import Any


MAX_ATTACHMENT_TEXT_CHARACTERS = 40_000
MAX_TOTAL_ATTACHMENT_TEXT_CHARACTERS = 80_000

# Raw upload limits. The browser applies the same total, but a client that
# bypasses the page must hit the same wall before any file is decoded.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENT_COUNT = 10


class AttachmentExtractionError(RuntimeError):
    pass


class AttachmentLimitError(ValueError):
    """A submission exceeds the raw upload limits and must not be decoded."""


def _encoded_byte_length(data_url: str) -> int:
    """Size of the decoded payload, derived without allocating it."""

    _, separator, encoded = data_url.partition(",")
    if not separator:
        return 0
    encoded = encoded.strip()
    padding = len(encoded) - len(encoded.rstrip("="))
    return max(0, (len(encoded) * 3) // 4 - padding)


def assert_within_upload_limits(files: list[Any]) -> None:
    """Reject oversized submissions before base64 decoding or PDF parsing."""

    if len(files) > MAX_ATTACHMENT_COUNT:
        raise AttachmentLimitError(
            f"最多只能上传 {MAX_ATTACHMENT_COUNT} 个附件"
        )
    total = 0
    for item in files:
        if not isinstance(item, dict):
            continue
        data_url = item.get("data")
        size = (
            _encoded_byte_length(data_url) if isinstance(data_url, str) else 0
        )
        declared = item.get("size")
        if isinstance(declared, int) and not isinstance(declared, bool):
            size = max(size, declared)
        if size > MAX_ATTACHMENT_BYTES:
            name = str(item.get("name") or "附件")
            raise AttachmentLimitError(
                f"附件 {name} 超过单文件 {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB 上限"
            )
        total += size
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise AttachmentLimitError(
                f"附件总大小超过 {MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)}MB 上限"
            )


def _decode_data_url(value: str) -> tuple[str, bytes]:
    header, separator, encoded = value.partition(",")
    if not separator or not header.startswith("data:") or ";base64" not in header:
        raise AttachmentExtractionError("attachment data must be a base64 data URL")
    mime_type = header[5:].split(";", 1)[0].lower()
    try:
        return mime_type, base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise AttachmentExtractionError("attachment base64 is invalid") from error


def _clean_text(value: str) -> str:
    value = value.replace("\x00", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


OFFICE_MIME_SUFFIXES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}
OFFICE_FILE_SUFFIXES = (".docx", ".xlsx", ".pptx")


def _office_suffix(
    mime_type: str, declared_type: str, filename: str
) -> str | None:
    """Resolve an Office format from mime type or filename, or None."""

    for candidate in (mime_type, declared_type):
        suffix = OFFICE_MIME_SUFFIXES.get(candidate)
        if suffix:
            return suffix
    for suffix in OFFICE_FILE_SUFFIXES:
        if filename.endswith(suffix):
            return suffix
    return None


def _extract_office_text(raw: bytes, suffix: str) -> str:
    """Convert an Office document to text via MarkItDown.

    MarkItDown is an optional dependency: it stays offline for these formats and
    performs no model call, but if it is not installed the caller degrades to
    UNSUPPORTED exactly as before rather than failing the submission.
    """

    try:
        from markitdown import MarkItDown
    except ImportError as error:
        raise AttachmentExtractionError(
            f"{suffix} support requires markitdown; install markitdown[docx,xlsx,pptx]"
        ) from error
    # MarkItDown selects a converter from the stream's filename extension, so a
    # temporary file with the right suffix is the reliable way to route it.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"attachment{suffix}"
        path.write_bytes(raw)
        result = MarkItDown(enable_plugins=False).convert(str(path))
    return _clean_text(result.text_content or "")


def extract_attachment_text(file_payload: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "name": file_payload.get("name"),
        "size": file_payload.get("size"),
        "type": file_payload.get("type"),
    }
    data_url = file_payload.get("data")
    if not isinstance(data_url, str) or not data_url:
        return {**metadata, "extraction_status": "NO_DATA", "extracted_text": ""}
    try:
        mime_type, raw = _decode_data_url(data_url)
        metadata["content_sha256"] = hashlib.sha256(raw).hexdigest()
        declared_type = str(file_payload.get("type") or "").lower()
        filename = str(file_payload.get("name") or "").lower()
        if mime_type == "application/pdf" or declared_type == "application/pdf" or filename.endswith(".pdf"):
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw), strict=False)
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception as error:  # pypdf exposes backend-specific errors
                    raise AttachmentExtractionError("PDF is encrypted") from error
            pages: list[str] = []
            for page in reader.pages:
                pages.append(page.extract_text() or "")
            text = _clean_text("\n\n".join(pages))
        elif mime_type.startswith("text/") or declared_type.startswith("text/"):
            text = _clean_text(raw.decode("utf-8", errors="replace"))
        elif _office_suffix(mime_type, declared_type, filename):
            text = _extract_office_text(
                raw, _office_suffix(mime_type, declared_type, filename)
            )
        else:
            return {
                **metadata,
                "extraction_status": "UNSUPPORTED",
                "extracted_text": "",
            }
    except Exception as error:  # malformed user files can raise parser-specific errors
        return {
            **metadata,
            "extraction_status": "FAILED",
            "extraction_error": str(error)[:200],
            "extracted_text": "",
        }
    truncated = len(text) > MAX_ATTACHMENT_TEXT_CHARACTERS
    return {
        **metadata,
        "extraction_status": "EXTRACTED" if text else "EMPTY",
        "text_characters": len(text),
        "truncated": truncated,
        "extracted_text": text[:MAX_ATTACHMENT_TEXT_CHARACTERS],
    }


def extract_attachments(files: list[Any]) -> list[dict[str, Any]]:
    assert_within_upload_limits(files)
    extracted: list[dict[str, Any]] = []
    remaining = MAX_TOTAL_ATTACHMENT_TEXT_CHARACTERS
    for item in files:
        if not isinstance(item, dict):
            continue
        result = extract_attachment_text(item)
        text = result.get("extracted_text") or ""
        if len(text) > remaining:
            result["extracted_text"] = text[:remaining]
            result["truncated"] = True
        remaining -= len(result.get("extracted_text") or "")
        extracted.append(result)
    return extracted
