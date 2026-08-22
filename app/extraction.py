"""Extract stage of the RAG ingestion pipeline (extract -> clean -> chunk ->
metadata -> embed -> index; see docs/rag.md). v1 accepts plain text directly
(pasted, or a .txt/.md/.csv/.json/.log upload) and PDF via a base64-encoded
upload. Other binary formats (DOCX, etc.) remain future work.
"""
from __future__ import annotations

import base64
import binascii
import io
import os

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class ExtractionError(ValueError):
    """Any bad/unsupported upload. Routes turn this into an HTTP 400 — the
    message is written to be safe to show a client as-is (no internals)."""


def sanitize_filename(filename: str) -> str:
    """Strips any directory components/control characters. Documents are
    stored in-memory keyed by document_id, not by filename, so this isn't
    guarding a real path-traversal write today — it's guarding the filename
    from ever being trusted as a path if that changes, per the project's
    "sanitize uploaded files" rule."""
    name = os.path.basename(filename.strip().replace("\\", "/")).replace("\x00", "")
    if not name:
        raise ExtractionError("Filename is required.")
    return name


def extract_text(filename: str, content: str, content_encoding: str, max_bytes: int) -> str:
    """Returns plain text ready for the clean/chunk stages.

    content_encoding == "text": content is already plain text — no-op passthrough.
    content_encoding == "base64": content is a base64-encoded binary upload;
    only PDF is supported today, sniffed by extension or file signature.
    """
    if content_encoding == "text":
        return content
    if content_encoding != "base64":
        raise ExtractionError(f"Unsupported content_encoding: {content_encoding!r}")

    try:
        raw = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise ExtractionError("Uploaded content is not valid base64.")

    if len(raw) > max_bytes:
        raise ExtractionError(f"File exceeds the {max_bytes // (1024 * 1024)} MB upload limit.")

    is_pdf = filename.lower().endswith(".pdf") or raw[:5] == b"%PDF-"
    if not is_pdf:
        raise ExtractionError(
            "Unsupported file type for binary upload — only PDF is supported; "
            "use plain text (.txt/.md/.csv/.json/.log) for everything else."
        )

    try:
        reader = PdfReader(io.BytesIO(raw))
    except PdfReadError as exc:
        raise ExtractionError(f"Could not read this PDF: {exc}")

    if reader.is_encrypted:
        raise ExtractionError("This PDF is password-protected and can't be read.")

    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    text = "\n\n".join(p for p in pages if p)
    if not text:
        raise ExtractionError(
            "No extractable text found in this PDF — it may be a scanned/image-only "
            "document (OCR isn't supported in v1)."
        )
    return text
