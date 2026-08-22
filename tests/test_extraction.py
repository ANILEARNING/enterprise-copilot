import base64
import io

import pytest
from pypdf import PdfWriter

from app.extraction import ExtractionError, extract_text, sanitize_filename

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

def test_extract_text_passthrough_for_plain_text():
    assert extract_text("notes.md", "hello world", "text", MAX_BYTES) == "hello world"


def test_extract_text_rejects_unknown_encoding():
    with pytest.raises(ExtractionError):
        extract_text("notes.md", "hello", "rot13", MAX_BYTES)


# --- PDF extraction ----------------------------------------------------------

def test_extract_text_reads_pdf_content():
    pdf_bytes = build_pdf("Hello enterprise pricing plan")
    text = extract_text("pricing.pdf", b64(pdf_bytes), "base64", MAX_BYTES)
    assert "Hello enterprise pricing plan" in text


def test_extract_text_sniffs_pdf_by_magic_bytes_without_pdf_extension():
    # filename doesn't end in .pdf, but the %PDF- signature is still detected
    pdf_bytes = build_pdf("Sniffed by content")
    text = extract_text("upload.bin", b64(pdf_bytes), "base64", MAX_BYTES)
    assert "Sniffed by content" in text


def test_extract_text_rejects_invalid_base64():
    with pytest.raises(ExtractionError):
        extract_text("a.pdf", "not-valid-base64!!!", "base64", MAX_BYTES)


def test_extract_text_rejects_oversized_upload():
    pdf_bytes = build_pdf("small")
    with pytest.raises(ExtractionError, match="exceeds"):
        extract_text("a.pdf", b64(pdf_bytes), "base64", max_bytes=10)


def test_extract_text_rejects_non_pdf_binary():
    with pytest.raises(ExtractionError, match="Unsupported file type"):
        extract_text("photo.png", b64(b"\x89PNG\r\n\x1a\nnot really a pdf"), "base64", MAX_BYTES)


def test_extract_text_rejects_encrypted_pdf():
    with pytest.raises(ExtractionError, match="password-protected"):
        extract_text("secret.pdf", b64(build_encrypted_pdf()), "base64", MAX_BYTES)


def test_extract_text_rejects_pdf_with_no_extractable_text():
    with pytest.raises(ExtractionError, match="scanned"):
        extract_text("scanned.pdf", b64(build_blank_pdf()), "base64", MAX_BYTES)


# --- filename sanitization --------------------------------------------------

def test_sanitize_filename_strips_directory_components():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("C:\\Users\\me\\report.pdf") == "report.pdf"


def test_sanitize_filename_rejects_empty():
    with pytest.raises(ExtractionError):
        sanitize_filename("   ")
