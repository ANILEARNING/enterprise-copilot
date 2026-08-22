"""Extract stage of the RAG ingestion pipeline (extract -> clean -> chunk ->
metadata -> embed -> index; see docs/rag.md).

Every format-specific extractor returns an `ExtractedDocument`: flat `text`
(back-compat — previews, size, anything that just wants "the content") plus
ordered `blocks` that carry document structure (headings, tables) for
app/retrieval.py's structure-aware chunking. A format with no structural
signal (plain text, JSON) returns a single flat paragraph block rather than
fabricating structure that isn't there — same honesty posture the PDF path
already had for encrypted/scanned documents.

Supported today: .txt/.md/.json/.log/.csv/.html (plain text, base64 not
required), .pdf/.docx/.xlsx (base64-encoded binary uploads), plus image-only
PDFs via Gemini Vision OCR when a vision-capable provider is configured.
"""
from __future__ import annotations

import base64
import binascii
import csv
import io
import os
from dataclasses import dataclass, field
from typing import Literal

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class ExtractionError(ValueError):
    """Any bad/unsupported upload. Routes turn this into an HTTP 400 — the
    message is written to be safe to show a client as-is (no internals)."""


@dataclass
class ExtractedBlock:
    """One structural unit of a document, in document order. `level` is a
    heading depth (1 = top-level/Title, 2 = h2, ...) for `kind == "heading"`,
    None otherwise. A "table" block's `text` is a lightweight row-per-line,
    tab-separated rendering (header row first when known) — enough for
    app/retrieval.py's chunker to keep it atomic and for the embedder/BM25
    to see its cell contents as text, without inventing a markdown-table
    dialect nothing else in this app needs to parse back out."""
    kind: Literal["heading", "paragraph", "table", "list_item"]
    text: str
    level: int | None = None


@dataclass
class ExtractedDocument:
    text: str
    blocks: list[ExtractedBlock] = field(default_factory=list)


def sanitize_filename(filename: str) -> str:
    """Strips any directory components/control characters. Documents are
    stored keyed by document_id, not by filename, so this isn't guarding a
    real path-traversal write today — it's guarding the filename from ever
    being trusted as a path if that changes, per the project's "sanitize
    uploaded files" rule."""
    name = os.path.basename(filename.strip().replace("\\", "/")).replace("\x00", "")
    if not name:
        raise ExtractionError("Filename is required.")
    return name


def _table_block_text(rows: list[list[str]]) -> str:
    return "\n".join("\t".join(str(cell).strip() for cell in row) for row in rows if any(str(c).strip() for c in row))


def _blocks_to_text(blocks: list[ExtractedBlock]) -> str:
    return "\n\n".join(b.text for b in blocks if b.text.strip())


# --- plain text (.txt/.json/.log/.csv/.html/.md) ---------------------------

_MD_HEADING_LEVELS = {f"{'#' * n} ": n for n in range(1, 7)}


def _extract_markdown(text: str) -> ExtractedDocument:
    """Trivial Markdown-heading pass so .md files get real `blocks`
    structure for heading-aware chunking, without a full CommonMark parser —
    only ATX-style (`# Heading`) headings are recognized; anything else
    (setext underlines, non-heading markup) is left as plain paragraph text,
    same honesty posture as everywhere else here: never fabricate structure
    that isn't unambiguously there."""
    blocks: list[ExtractedBlock] = []
    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        joined = "\n".join(paragraph_lines).strip()
        if joined:
            blocks.append(ExtractedBlock("paragraph", joined))
        paragraph_lines.clear()

    for line in text.split("\n"):
        stripped = line if line.endswith(" ") else line + " "
        level = next((lvl for prefix, lvl in _MD_HEADING_LEVELS.items() if stripped.startswith(prefix)), None)
        if level is not None:
            flush_paragraph()
            blocks.append(ExtractedBlock("heading", line[level + 1:].strip(), level=level))
        elif line.strip() == "":
            flush_paragraph()
        else:
            paragraph_lines.append(line)
    flush_paragraph()
    return ExtractedDocument(text=text, blocks=blocks or [ExtractedBlock("paragraph", text)])


def _extract_csv(text: str) -> ExtractedDocument:
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise ExtractionError(f"Could not parse this CSV: {exc}")
    if not rows:
        raise ExtractionError("This CSV file is empty.")
    table_text = _table_block_text(rows)
    if not table_text:
        raise ExtractionError("This CSV file has no non-empty cells.")
    return ExtractedDocument(text=table_text, blocks=[ExtractedBlock("table", table_text)])


def _extract_html(text: str) -> ExtractedDocument:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    root = soup.body or soup
    blocks: list[ExtractedBlock] = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table"]):
        content = el.get_text(" ", strip=True)
        if not content:
            continue
        if el.name.startswith("h") and el.name[1:].isdigit():
            blocks.append(ExtractedBlock("heading", content, level=int(el.name[1:])))
        elif el.name == "table":
            rows = [[cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
                    for tr in el.find_all("tr")]
            table_text = _table_block_text(rows)
            if table_text:
                blocks.append(ExtractedBlock("table", table_text))
        elif el.name == "li":
            blocks.append(ExtractedBlock("list_item", content))
        else:
            blocks.append(ExtractedBlock("paragraph", content))
    if not blocks:
        raise ExtractionError("No extractable text found in this HTML document.")
    return ExtractedDocument(text=_blocks_to_text(blocks), blocks=blocks)


# --- PDF (text layer + image-only via Gemini Vision) ------------------------

async def _extract_pdf(raw: bytes, vision_provider=None) -> ExtractedDocument:
    try:
        reader = PdfReader(io.BytesIO(raw))
    except PdfReadError as exc:
        raise ExtractionError(f"Could not read this PDF: {exc}")

    if reader.is_encrypted:
        raise ExtractionError("This PDF is password-protected and can't be read.")

    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    text = "\n\n".join(p for p in pages if p)
    if text:
        # pypdf's plain extract_text() has no reliable font-size/heading
        # signal to build real structural blocks from — one paragraph block
        # per non-empty page keeps chunking's paragraph-packing behavior
        # identical to before rather than fabricating headings that aren't
        # there (same honesty posture as the rest of this module).
        blocks = [ExtractedBlock("paragraph", p) for p in pages if p]
        return ExtractedDocument(text=text, blocks=blocks)

    # No text layer at all -> likely a scanned/image-only PDF. OCR via
    # Gemini Vision when a vision-capable provider is actually configured;
    # otherwise reject clearly rather than silently indexing nothing.
    if vision_provider is None:
        raise ExtractionError(
            "No extractable text found in this PDF — it appears to be scanned/image-only. "
            "Set AI_MODE=configured with a Gemini API key to enable OCR for image-only PDFs."
        )

    page_texts: list[str] = []
    for page in reader.pages:
        images = [img for img in page.images]
        if not images:
            continue
        # One page is typically one embedded raster image for a scanned
        # document — take the first/largest rather than sending every
        # embedded image (a scanned page can have small embedded artifacts
        # alongside the actual page scan).
        largest = max(images, key=lambda img: len(img.data))
        encoded = base64.b64encode(largest.data).decode("ascii")
        result = await vision_provider.complete(
            "Transcribe every word of visible text on this page, verbatim, in reading order. "
            "Output only the transcribed text — no commentary, no markdown formatting.",
            [], images=[{"data": encoded, "mime_type": "image/png"}],
        )
        page_text = result.text.strip()
        if page_text:
            page_texts.append(page_text)

    text = "\n\n".join(page_texts)
    if not text:
        raise ExtractionError(
            "No extractable text found in this PDF, and OCR found no readable text either — "
            "it may be blank or unreadably low-quality."
        )
    return ExtractedDocument(text=text, blocks=[ExtractedBlock("paragraph", p) for p in page_texts])


# --- DOCX --------------------------------------------------------------------

def _extract_docx(raw: bytes) -> ExtractedDocument:
    import zipfile

    import docx
    from docx.opc.exceptions import PackageNotFoundError

    try:
        document = docx.Document(io.BytesIO(raw))
    except (PackageNotFoundError, zipfile.BadZipFile, KeyError):
        raise ExtractionError("Could not read this DOCX file — it may be corrupted or not a real .docx.")

    blocks: list[ExtractedBlock] = []
    for item in document.iter_inner_content():
        if item.__class__.__name__ == "Table":
            rows = [[cell.text for cell in row.cells] for row in item.rows]
            table_text = _table_block_text(rows)
            if table_text:
                blocks.append(ExtractedBlock("table", table_text))
            continue
        content = item.text.strip()
        if not content:
            continue
        style_name = (item.style.name or "").strip() if item.style else ""
        if style_name.startswith("Heading "):
            try:
                level = int(style_name.split(" ", 1)[1])
            except ValueError:
                level = 1
            blocks.append(ExtractedBlock("heading", content, level=level))
        elif style_name == "Title":
            blocks.append(ExtractedBlock("heading", content, level=1))
        elif style_name.startswith("List"):
            blocks.append(ExtractedBlock("list_item", content))
        else:
            blocks.append(ExtractedBlock("paragraph", content))

    if not blocks:
        raise ExtractionError("No extractable text found in this DOCX file.")
    return ExtractedDocument(text=_blocks_to_text(blocks), blocks=blocks)


# --- XLSX --------------------------------------------------------------------

def _extract_xlsx(raw: bytes) -> ExtractedDocument:
    import zipfile

    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        workbook = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError) as exc:
        raise ExtractionError(f"Could not read this Excel file: {exc}")

    blocks: list[ExtractedBlock] = []
    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        rows = [
            ["" if cell is None else cell for cell in row]
            for row in sheet.iter_rows(values_only=True)
        ]
        table_text = _table_block_text(rows)
        if not table_text:
            continue
        blocks.append(ExtractedBlock("heading", sheet_name, level=1))
        blocks.append(ExtractedBlock("table", table_text))

    if not blocks:
        raise ExtractionError("This Excel file has no non-empty sheets.")
    return ExtractedDocument(text=_blocks_to_text(blocks), blocks=blocks)


# --- dispatch ------------------------------------------------------------------

# Extension -> whether it's a binary format expected as base64. Anything not
# listed here is treated as plain text (matches today's behavior for
# unrecognized extensions — never rejected just for an unfamiliar suffix).
_BINARY_EXTENSIONS = {".pdf", ".docx", ".xlsx"}


def is_binary_format(filename: str) -> bool:
    return any(filename.lower().endswith(ext) for ext in _BINARY_EXTENSIONS)


async def extract_document(
    filename: str, content: str, content_encoding: str, max_bytes: int, vision_provider=None,
) -> ExtractedDocument:
    """Returns the full ExtractedDocument (text + structural blocks). See
    extract_text() below for callers that only need flat text.

    content_encoding == "text": content is already plain text — dispatched
    by extension to a format-specific structural parser (.md/.csv/.html), or
    passed through flat for anything else (.txt/.json/.log/unrecognized).
    content_encoding == "base64": a binary upload, dispatched by extension/
    signature — PDF, DOCX, or XLSX today.

    vision_provider: an AIProvider (see app/providers.py) used only for
    image-only PDF OCR — None means "no vision capability configured," which
    makes an image-only PDF a clear rejection rather than a silent empty
    index, same posture as an encrypted PDF.
    """
    lowered = filename.lower()
    if content_encoding == "text":
        if lowered.endswith(".md"):
            return _extract_markdown(content)
        if lowered.endswith(".csv"):
            return _extract_csv(content)
        if lowered.endswith(".html") or lowered.endswith(".htm"):
            return _extract_html(content)
        return ExtractedDocument(text=content, blocks=[ExtractedBlock("paragraph", content)] if content.strip() else [])

    if content_encoding != "base64":
        raise ExtractionError(f"Unsupported content_encoding: {content_encoding!r}")

    try:
        raw = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise ExtractionError("Uploaded content is not valid base64.")

    if len(raw) > max_bytes:
        raise ExtractionError(f"File exceeds the {max_bytes // (1024 * 1024)} MB upload limit.")

    is_pdf = lowered.endswith(".pdf") or raw[:5] == b"%PDF-"
    is_xlsx = lowered.endswith(".xlsx")
    # DOCX and XLSX are both zip containers, so `PK` alone can't tell them
    # apart — is_xlsx is checked (and returned) first below, purely on
    # extension, so this broader signature fallback only ever applies once
    # xlsx has already been ruled out.
    is_docx = lowered.endswith(".docx") or raw[:2] == b"PK"

    if is_pdf:
        return await _extract_pdf(raw, vision_provider=vision_provider)
    if is_xlsx:
        return _extract_xlsx(raw)
    if is_docx:
        return _extract_docx(raw)

    raise ExtractionError(
        "Unsupported file type for binary upload — supported: PDF, DOCX, XLSX; "
        "use plain text (.txt/.md/.csv/.json/.html/.log) for everything else."
    )


async def extract_text(
    filename: str, content: str, content_encoding: str, max_bytes: int, vision_provider=None,
) -> str:
    """Back-compat flat-text entry point — most callers (previews, size
    calculation, anything that doesn't need block structure) just want
    this. See extract_document() for the full structural result."""
    return (await extract_document(filename, content, content_encoding, max_bytes, vision_provider)).text
