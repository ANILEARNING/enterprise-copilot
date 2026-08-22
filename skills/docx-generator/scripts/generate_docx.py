#!/usr/bin/env python3
"""docx-generator skill — builds a styled .docx from a JSON document spec.

Usage:
    python generate_docx.py --input spec.json --output out.docx
    cat spec.json | python generate_docx.py --output out.docx   # spec via stdin

Spec shape (see ../reference/outline_conventions.md for how to build one from
answers to the skill's pre-flight questions):
{
  "title": "Document title",
  "subtitle": "Optional subtitle / author / date line",
  "tone": "formal",                 // optional, used only if "style" is absent
  "style": "corporate",             // one of style_presets.STYLES, optional
  "sections": [
    {"heading": "Executive Summary", "paragraphs": ["...", "..."]},
    {"heading": "Background", "paragraphs": ["..."]}
  ]
}

Standalone, dependency-light script (python-docx only) so it can be invoked
directly by a human, by a test, or by the agent's tool-call layer without any
of this skill's other machinery — mirrors ppt-generator/scripts/generate_ppt.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

sys.path.insert(0, str(Path(__file__).parent))
from style_presets import Style, resolve_style  # noqa: E402


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color)


def _style_named(document: Document, style_name: str, font_name: str, color_hex: str, size_pt: int, bold: bool = False):
    style = document.styles[style_name]
    style.font.name = font_name
    style.font.size = Pt(size_pt)
    style.font.color.rgb = _rgb(color_hex)
    style.font.bold = bold
    return style


def build_document(spec: dict) -> Document:
    if not spec.get("title", "").strip():
        raise ValueError("spec.title is required and cannot be empty")
    sections_spec = spec.get("sections") or []

    style = resolve_style(spec.get("style"), spec.get("tone"))
    document = Document()

    # base styles apply document-wide; headings/title are restyled on top
    _style_named(document, "Normal", style.body_font, style.body_color, style.body_size_pt)
    _style_named(document, "Title", style.heading_font, style.heading_color, style.heading_size_pt, bold=True)
    _style_named(document, "Heading 1", style.heading_font, style.heading_color,
                 int(style.heading_size_pt * 0.7), bold=True)

    # --- title page ---
    document.add_heading(spec["title"], level=0)
    subtitle = spec.get("subtitle", "").strip()
    if subtitle:
        p = document.add_paragraph()
        run = p.add_run(subtitle)
        run.italic = True
        run.font.color.rgb = _rgb(style.accent_color)
        run.font.size = Pt(style.body_size_pt + 1)

    # a simple accent rule under the title/subtitle block
    rule = document.add_paragraph()
    rule_run = rule.add_run("—" * 20)
    rule_run.font.color.rgb = _rgb(style.accent_color)
    rule.alignment = WD_ALIGN_PARAGRAPH.LEFT

    # --- sections ---
    used_sections = 0
    for section_spec in sections_spec:
        heading = (section_spec.get("heading") or "").strip()
        paragraphs = [p for p in (section_spec.get("paragraphs") or []) if str(p).strip()]
        if not heading and not paragraphs:
            continue  # skip a genuinely empty section entry rather than emit a blank heading
        used_sections += 1

        if heading:
            document.add_heading(heading, level=1)
        for paragraph_text in paragraphs:
            document.add_paragraph(str(paragraph_text))

    if used_sections == 0:
        raise ValueError("spec.sections produced no usable content (every entry was empty)")

    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, help="Path to the JSON spec (default: read from stdin)")
    parser.add_argument("--output", type=Path, required=True, help="Path to write the .docx file")
    args = parser.parse_args()

    raw = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: input is not valid JSON: {exc}", file=sys.stderr)
        return 1

    try:
        document = build_document(spec)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    document.save(args.output)
    print(f"wrote {len(document.paragraphs)} paragraphs to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
