import base64
import io

import pytest
from pypdf import PdfWriter

from app.extraction import ExtractionError, extract_document, extract_text, sanitize_filename
from app.providers import AIProvider, ProviderResult

MAX_BYTES = 20 * 1024 * 1024


def build_pdf(text: str) -> bytes:
    """A minimal, hand-built single-page PDF with a real text content
    stream (correct xref/offsets) — enough for pypdf to parse and extract
    `text` from, without pulling in a heavier PDF-authoring dependency."""
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 200 200] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode())
        out.write(body)
        out.write(b"\nendobj\n")
    xref_offset = out.tell()
    n = len(objs) + 1
    out.write(f"xref\n0 {n}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode())
    return out.getvalue()


def build_blank_pdf() -> bytes:
    """A valid PDF with no text content — a stand-in for a scanned/image-only page."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def build_encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# --- plain text passthrough -----------------------------------------------

@pytest.mark.asyncio
async def test_extract_text_passthrough_for_plain_text():
    assert await extract_text("notes.txt", "hello world", "text", MAX_BYTES) == "hello world"


@pytest.mark.asyncio
async def test_extract_text_rejects_unknown_encoding():
    with pytest.raises(ExtractionError):
        await extract_text("notes.md", "hello", "rot13", MAX_BYTES)


# --- Markdown structural parsing --------------------------------------------

@pytest.mark.asyncio
async def test_extract_document_parses_markdown_headings():
    md = "# Title\n\nIntro paragraph.\n\n## Section One\n\nBody text here."
    doc = await extract_document("notes.md", md, "text", MAX_BYTES)
    kinds = [(b.kind, b.level, b.text) for b in doc.blocks]
    assert ("heading", 1, "Title") in kinds
    assert ("heading", 2, "Section One") in kinds
    assert ("paragraph", None, "Intro paragraph.") in kinds


@pytest.mark.asyncio
async def test_extract_document_markdown_without_headings_is_one_paragraph_block():
    doc = await extract_document("plain.md", "just some text\nwith a line break", "text", MAX_BYTES)
    assert len(doc.blocks) == 1
    assert doc.blocks[0].kind == "paragraph"


# --- CSV ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_document_csv_becomes_one_table_block():
    csv_text = "Name,Price\nWidget,9.99\nGadget,19.99"
    doc = await extract_document("prices.csv", csv_text, "text", MAX_BYTES)
    assert len(doc.blocks) == 1
    assert doc.blocks[0].kind == "table"
    assert "Widget" in doc.blocks[0].text and "9.99" in doc.blocks[0].text


@pytest.mark.asyncio
async def test_extract_document_rejects_empty_csv():
    with pytest.raises(ExtractionError):
        await extract_document("empty.csv", "", "text", MAX_BYTES)


# --- HTML ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_document_html_parses_headings_paragraphs_and_tables():
    html = (
        "<html><body><script>evil()</script>"
        "<h1>Report</h1><p>Summary text.</p>"
        "<table><tr><th>Col</th></tr><tr><td>Val</td></tr></table>"
        "</body></html>"
    )
    doc = await extract_document("report.html", html, "text", MAX_BYTES)
    kinds = [(b.kind, b.text) for b in doc.blocks]
    assert ("heading", "Report") in kinds
    assert ("paragraph", "Summary text.") in kinds
    assert any(b.kind == "table" and "Val" in b.text for b in doc.blocks)
    assert "evil()" not in doc.text  # script content must never leak into the indexed text


@pytest.mark.asyncio
async def test_extract_document_rejects_html_with_no_extractable_content():
    with pytest.raises(ExtractionError):
        await extract_document("blank.html", "<html><body><script>x()</script></body></html>", "text", MAX_BYTES)


# --- DOCX ----------------------------------------------------------------------

def build_docx(with_table: bool = True) -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("Policy Document", level=1)
    document.add_paragraph("This is the intro paragraph.")
    document.add_heading("Refunds", level=2)
    document.add_paragraph("Refunds are processed within 30 days.")
    if with_table:
        table = document.add_table(rows=2, cols=2)
        table.rows[0].cells[0].text = "Plan"
        table.rows[0].cells[1].text = "Price"
        table.rows[1].cells[0].text = "Pro"
        table.rows[1].cells[1].text = "100"
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_extract_document_docx_parses_headings_paragraphs_and_tables():
    doc = await extract_document("policy.docx", b64(build_docx()), "base64", MAX_BYTES)
    kinds = [(b.kind, b.level, b.text) for b in doc.blocks]
    assert ("heading", 1, "Policy Document") in kinds
    assert ("heading", 2, "Refunds") in kinds
    assert any(b.kind == "paragraph" and "intro paragraph" in b.text for b in doc.blocks)
    assert any(b.kind == "table" and "Pro" in b.text and "100" in b.text for b in doc.blocks)


@pytest.mark.asyncio
async def test_extract_document_docx_preserves_document_order():
    doc = await extract_document("policy.docx", b64(build_docx()), "base64", MAX_BYTES)
    # "Policy Document" heading must come before "Refunds" heading, which
    # must come before the table — iter_inner_content() order, not
    # paragraphs-then-tables order.
    kinds = [b.kind for b in doc.blocks]
    policy_idx = next(i for i, b in enumerate(doc.blocks) if b.text == "Policy Document")
    refunds_idx = next(i for i, b in enumerate(doc.blocks) if b.text == "Refunds")
    table_idx = next(i for i, b in enumerate(doc.blocks) if b.kind == "table")
    assert policy_idx < refunds_idx < table_idx


@pytest.mark.asyncio
async def test_extract_document_rejects_corrupt_docx():
    with pytest.raises(ExtractionError):
        await extract_document("broken.docx", b64(b"not a real docx file"), "base64", MAX_BYTES)


# --- XLSX ----------------------------------------------------------------------

def build_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Pricing"
    ws.append(["Plan", "Price"])
    ws.append(["Pro", 100])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_extract_document_xlsx_becomes_heading_plus_table_per_sheet():
    doc = await extract_document("pricing.xlsx", b64(build_xlsx()), "base64", MAX_BYTES)
    assert any(b.kind == "heading" and b.text == "Pricing" for b in doc.blocks)
    assert any(b.kind == "table" and "Pro" in b.text and "100" in b.text for b in doc.blocks)


@pytest.mark.asyncio
async def test_extract_document_rejects_corrupt_xlsx():
    with pytest.raises(ExtractionError):
        await extract_document("broken.xlsx", b64(b"not a real xlsx file"), "base64", MAX_BYTES)


# --- PDF extraction (text layer) ----------------------------------------------

@pytest.mark.asyncio
async def test_extract_text_reads_pdf_content():
    pdf_bytes = build_pdf("Hello enterprise pricing plan")
    text = await extract_text("pricing.pdf", b64(pdf_bytes), "base64", MAX_BYTES)
    assert "Hello enterprise pricing plan" in text


@pytest.mark.asyncio
async def test_extract_text_sniffs_pdf_by_magic_bytes_without_pdf_extension():
    pdf_bytes = build_pdf("Sniffed by content")
    text = await extract_text("upload.bin", b64(pdf_bytes), "base64", MAX_BYTES)
    assert "Sniffed by content" in text


@pytest.mark.asyncio
async def test_extract_text_rejects_invalid_base64():
    with pytest.raises(ExtractionError):
        await extract_text("a.pdf", "not-valid-base64!!!", "base64", MAX_BYTES)


@pytest.mark.asyncio
async def test_extract_text_rejects_oversized_upload():
    pdf_bytes = build_pdf("small")
    with pytest.raises(ExtractionError, match="exceeds"):
        await extract_text("a.pdf", b64(pdf_bytes), "base64", max_bytes=10)


@pytest.mark.asyncio
async def test_extract_text_rejects_non_pdf_binary():
    with pytest.raises(ExtractionError, match="Unsupported file type"):
        await extract_text("photo.png", b64(b"\x89PNG\r\n\x1a\nnot really a pdf"), "base64", MAX_BYTES)


@pytest.mark.asyncio
async def test_extract_text_rejects_encrypted_pdf():
    with pytest.raises(ExtractionError, match="password-protected"):
        await extract_text("secret.pdf", b64(build_encrypted_pdf()), "base64", MAX_BYTES)


@pytest.mark.asyncio
async def test_extract_text_rejects_pdf_with_no_extractable_text_and_no_vision_provider():
    with pytest.raises(ExtractionError, match="scanned"):
        await extract_text("scanned.pdf", b64(build_blank_pdf()), "base64", MAX_BYTES)


# --- image-only PDF OCR (Gemini Vision) ---------------------------------------

class FakeVisionProvider(AIProvider):
    """Stands in for GeminiProvider — asserts it was called with the page
    image and returns deterministic 'transcribed' text, no network call."""

    name = "fake-vision"

    def __init__(self):
        self.calls: list[dict] = []

    async def complete(self, prompt, history, max_tokens=None, json_mode=False, images=None):
        self.calls.append({"prompt": prompt, "images": images})
        return ProviderResult(text="Transcribed page text.", provider=self.name)


@pytest.mark.asyncio
async def test_extract_text_rejects_blank_pdf_even_with_vision_provider():
    # A truly blank page has no embedded image at all to send for OCR —
    # still a clear rejection, not a silent empty index.
    vision = FakeVisionProvider()
    with pytest.raises(ExtractionError):
        await extract_text("scanned.pdf", b64(build_blank_pdf()), "base64", MAX_BYTES, vision_provider=vision)
    assert vision.calls == []  # never even attempted — no image to send


@pytest.mark.asyncio
async def test_extract_text_ocrs_image_only_pdf_via_vision_provider(monkeypatch):
    # A scanned page's "text layer" is empty but it DOES have an embedded
    # raster image — monkeypatch pypdf's own `page.images` (exactly the
    # property _extract_pdf reads) rather than hand-crafting a real embedded
    # XObject image stream, since the point under test is _extract_pdf's own
    # OCR dispatch logic, not pypdf's image-parsing.
    import app.extraction as extraction_module

    class FakeImageFile:
        data = b"\x89PNG\r\n\x1a\nfake-png-bytes"

    monkeypatch.setattr(
        extraction_module.PdfReader, "pages",
        property(lambda self: [type("Page", (), {"extract_text": lambda s: "", "images": [FakeImageFile()]})()]),
    )
    vision = FakeVisionProvider()
    text = await extract_text("scanned.pdf", b64(build_blank_pdf()), "base64", MAX_BYTES, vision_provider=vision)
    assert text == "Transcribed page text."
    assert len(vision.calls) == 1
    assert vision.calls[0]["images"][0]["mime_type"] == "image/png"


@pytest.mark.asyncio
async def test_extract_text_rejects_image_pdf_when_ocr_finds_no_text(monkeypatch):
    import app.extraction as extraction_module

    class FakeImageFile:
        data = b"\x89PNG\r\n\x1a\nfake-png-bytes"

    monkeypatch.setattr(
        extraction_module.PdfReader, "pages",
        property(lambda self: [type("Page", (), {"extract_text": lambda s: "", "images": [FakeImageFile()]})()]),
    )

    class EmptyVisionProvider(AIProvider):
        name = "empty-vision"

        async def complete(self, prompt, history, max_tokens=None, json_mode=False, images=None):
            return ProviderResult(text="", provider=self.name)

    with pytest.raises(ExtractionError, match="OCR"):
        await extract_text(
            "scanned.pdf", b64(build_blank_pdf()), "base64", MAX_BYTES, vision_provider=EmptyVisionProvider(),
        )


# --- filename sanitization --------------------------------------------------

def test_sanitize_filename_strips_directory_components():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("C:\\Users\\me\\report.pdf") == "report.pdf"


def test_sanitize_filename_rejects_empty():
    with pytest.raises(ExtractionError):
        sanitize_filename("   ")
