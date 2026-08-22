#!/usr/bin/env python3
"""ppt-generator skill — builds a themed .pptx from a JSON slide spec.

Usage:
    python generate_ppt.py --input spec.json --output out.pptx
    cat spec.json | python generate_ppt.py --output out.pptx   # spec via stdin

Spec shape (see ../reference/outline_conventions.md for how to build one from
answers to the skill's pre-flight questions):
{
  "title": "Presentation title",
  "subtitle": "Optional subtitle / author / date line",
  "tone": "formal",                 // optional, used only if "theme" is absent
  "theme": "corporate",             // one of theme_presets.THEMES, optional
  "slides": [
    {"title": "Agenda", "bullets": ["Point one", "Point two"]},
    {"title": "Section heading", "bullets": ["...", "..."]}
  ]
}

Kept as a standalone, dependency-light script (python-pptx only) so it can be
invoked directly by a human, by a test, or by the agent's tool-call layer
without any of this skill's other machinery.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

sys.path.insert(0, str(Path(__file__).parent))
from theme_presets import Theme, resolve_theme  # noqa: E402


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color)


def _set_background(slide, theme: Theme) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _rgb(theme.background)


def _style_text_frame(text_frame, color_hex: str, font_name: str, size_pt: int, bold: bool = False) -> None:
    for paragraph in text_frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = Pt(size_pt)
            run.font.name = font_name
            run.font.bold = bold
            run.font.color.rgb = _rgb(color_hex)


def build_presentation(spec: dict) -> Presentation:
    if not spec.get("title", "").strip():
        raise ValueError("spec.title is required and cannot be empty")
    slides_spec = spec.get("slides") or []

    theme = resolve_theme(spec.get("theme"), spec.get("tone"))
    prs = Presentation()
    # python-pptx's default template is 4:3, which letterboxes on essentially
    # every current display/projector. 16:9 (13.333in x 7.5in) is PowerPoint's
    # own default for new decks — set it before any slide is added so the
    # layouts lay out against the final canvas size.
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # --- title slide ---
    title_layout = prs.slide_layouts[0]
    title_slide = prs.slides.add_slide(title_layout)
    _set_background(title_slide, theme)
    title_slide.shapes.title.text = spec["title"]
    _style_text_frame(title_slide.shapes.title.text_frame, theme.title_color, theme.font_name,
                       theme.title_size_pt, bold=True)
    subtitle = spec.get("subtitle", "")
    if subtitle and len(title_slide.placeholders) > 1:
        subtitle_ph = title_slide.placeholders[1]
        subtitle_ph.text = subtitle
        _style_text_frame(subtitle_ph.text_frame, theme.body_color, theme.font_name, theme.body_size_pt)

    # --- content slides ---
    content_layout = prs.slide_layouts[1]
    for slide_spec in slides_spec:
        slide_title = (slide_spec.get("title") or "").strip()
        bullets = [b for b in (slide_spec.get("bullets") or []) if str(b).strip()]
        if not slide_title and not bullets:
            continue  # skip genuinely empty slide entries rather than emit a blank slide

        slide = prs.slides.add_slide(content_layout)
        _set_background(slide, theme)

        if slide.shapes.title is not None:
            slide.shapes.title.text = slide_title or " "
            _style_text_frame(slide.shapes.title.text_frame, theme.accent_color, theme.font_name,
                               int(theme.title_size_pt * 0.6), bold=True)

        body_ph = next((p for p in slide.placeholders if p.placeholder_format.idx == 1), None)
        if body_ph is not None and bullets:
            text_frame = body_ph.text_frame
            text_frame.clear()
            for i, bullet in enumerate(bullets):
                paragraph = text_frame.paragraphs[0] if i == 0 else text_frame.add_paragraph()
                paragraph.text = str(bullet)
                paragraph.level = 0
            _style_text_frame(text_frame, theme.body_color, theme.font_name, theme.body_size_pt)

    if len(prs.slides) == 1:
        raise ValueError("spec.slides produced no usable content slides (every entry was empty)")

    return prs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, help="Path to the JSON spec (default: read from stdin)")
    parser.add_argument("--output", type=Path, required=True, help="Path to write the .pptx file")
    args = parser.parse_args()

    raw = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: input is not valid JSON: {exc}", file=sys.stderr)
        return 1

    try:
        prs = build_presentation(spec)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    prs.save(args.output)
    print(f"wrote {len(prs.slides)} slides to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
