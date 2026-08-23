#!/usr/bin/env python3
"""pptx skill — builds a richer, layout-varied .pptx from a JSON slide spec.

Usage:
    python generate_pptx.py --input spec.json --output out.pptx
    cat spec.json | python generate_pptx.py --output out.pptx   # spec via stdin

Spec shape (see ../SKILL.md's Workflow section and
../reference/outline_conventions.md for how to build one from answers to
this skill's pre-flight questions):
{
  "title": "Presentation title",
  "subtitle": "Optional subtitle / author / date line",
  "tone": "formal",                 // optional, used only if "theme" is absent
  "theme": "midnight_executive",    // one of pptx_themes.PALETTES, optional
  "slides": [
    {"layout": "bullets", "title": "...", "bullets": ["...", "..."], "icon": "check"},
    {"layout": "two_column", "title": "...",
     "left_heading": "...", "left_bullets": [...],
     "right_heading": "...", "right_bullets": [...]},
    {"layout": "stat_callout", "title": "...", "stats": [{"value": "42%", "label": "..."}]},
    {"layout": "chart", "title": "...",
     "chart": {"type": "bar", "categories": [...], "series": [{"name": "...", "values": [1, 2, 3]}]}},
    {"layout": "section_divider", "title": "...", "subtitle": "..."}
  ]
}

`layout` is optional per slide — missing/unrecognized degrades to "bullets"
rather than raising, same never-fail posture as pptx_themes.resolve_palette.
The title slide is never a `slides` entry; it's always built from the
top-level `title`/`subtitle`.

Kept standalone (python-pptx + this skill's own pptx_themes.py only, no
other dependency) so it can be invoked directly by run_generation_script's
scrubbed-env subprocess (app/skills.py) without needing anything outside
this skill's own directory.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

sys.path.insert(0, str(Path(__file__).parent))
from pptx_themes import Palette, resolve_icon, resolve_palette  # noqa: E402

SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)
MARGIN = Inches(0.5)

_CHART_TYPE_MAP = {
    "bar": XL_CHART_TYPE.BAR_CLUSTERED,
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "line": XL_CHART_TYPE.LINE_MARKERS,
    "pie": XL_CHART_TYPE.PIE,
}


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color)


def _blank_slide(prs: Presentation):
    return prs.slides.add_slide(prs.slide_layouts[6])  # blank layout — every layout below positions manually


def _fill_background(slide, hex_color: str) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _rgb(hex_color)


def _textbox(slide, left, top, width, height, text: str, *, size_pt: int, color_hex: str,
             font_name: str, bold: bool = False, italic: bool = False,
             align: "PP_ALIGN" = PP_ALIGN.LEFT, anchor: "MSO_ANCHOR" = MSO_ANCHOR.TOP):
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(size_pt)
    run.font.name = font_name
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = _rgb(color_hex)
    return box


def _bullet_list(slide, left, top, width, height, items: list[str], *, size_pt: int,
                  color_hex: str, font_name: str):
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        run = p.add_run()
        run.text = f"•  {item}"
        run.font.size = Pt(size_pt)
        run.font.name = font_name
        run.font.color.rgb = _rgb(color_hex)
        p.space_after = Pt(10)
    return box


def _icon_circle(slide, center_x, center_y, diameter, icon_key: str | None, palette: Palette):
    """A filled circle at (center_x, center_y) with `diameter`, containing a
    centered glyph if `icon_key` resolves to one — otherwise a plain filled
    circle. Never raises on an unrecognized icon_key (resolve_icon's own
    contract)."""
    left = center_x - diameter // 2
    top = center_y - diameter // 2
    shape = slide.shapes.add_shape(MSO_SHAPE.OVAL, left, top, diameter, diameter)
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(palette.accent)
    shape.line.fill.background()
    glyph = resolve_icon(icon_key)
    tf = shape.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = glyph or ""
    run.font.size = Pt(int(Emu(diameter).inches * 28))
    run.font.bold = True
    run.font.color.rgb = _rgb("FFFFFF")
    return shape


# --- layouts ------------------------------------------------------------

def _build_title_slide(prs: Presentation, spec: dict, palette: Palette) -> None:
    slide = _blank_slide(prs)
    _fill_background(slide, palette.divider_background)
    title = str(spec.get("title") or "").strip()
    _textbox(slide, MARGIN, Inches(2.9), SLIDE_W - 2 * MARGIN, Inches(1.6), title,
              size_pt=40, color_hex="FFFFFF", font_name=palette.font_name, bold=True,
              align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    subtitle = str(spec.get("subtitle") or "").strip()
    if subtitle:
        _textbox(slide, MARGIN, Inches(4.5), SLIDE_W - 2 * MARGIN, Inches(0.8), subtitle,
                  size_pt=18, color_hex="EFEFEF", font_name=palette.font_name, italic=True,
                  align=PP_ALIGN.CENTER)


def _build_bullets_slide(prs: Presentation, slide_spec: dict, palette: Palette) -> None:
    slide = _blank_slide(prs)
    _fill_background(slide, palette.background)
    icon_key = slide_spec.get("icon")
    title_left = MARGIN
    if icon_key and resolve_icon(icon_key):
        _icon_circle(slide, MARGIN + Inches(0.35), Inches(0.75), Inches(0.7), icon_key, palette)
        title_left = MARGIN + Inches(0.9)
    title = str(slide_spec.get("title") or "").strip()
    _textbox(slide, title_left, Inches(0.5), SLIDE_W - MARGIN - title_left, Inches(0.7), title,
              size_pt=28, color_hex=palette.dominant, font_name=palette.font_name, bold=True)
    bullets = [str(b) for b in (slide_spec.get("bullets") or []) if str(b).strip()]
    if bullets:
        _bullet_list(slide, MARGIN, Inches(1.6), SLIDE_W - 2 * MARGIN, Inches(5.2), bullets,
                     size_pt=18, color_hex=palette.support, font_name=palette.font_name)


def _build_two_column_slide(prs: Presentation, slide_spec: dict, palette: Palette) -> None:
    slide = _blank_slide(prs)
    _fill_background(slide, palette.background)
    title = str(slide_spec.get("title") or "").strip()
    _textbox(slide, MARGIN, Inches(0.5), SLIDE_W - 2 * MARGIN, Inches(0.7), title,
              size_pt=28, color_hex=palette.dominant, font_name=palette.font_name, bold=True)

    col_w = Inches(5.7)
    left_x = MARGIN
    right_x = SLIDE_W - MARGIN - col_w

    for x, heading_key, bullets_key in ((left_x, "left_heading", "left_bullets"), (right_x, "right_heading", "right_bullets")):
        heading = str(slide_spec.get(heading_key) or "").strip()
        if heading:
            _textbox(slide, x, Inches(1.6), col_w, Inches(0.5), heading,
                      size_pt=18, color_hex=palette.accent, font_name=palette.font_name, bold=True)
        items = [str(b) for b in (slide_spec.get(bullets_key) or []) if str(b).strip()]
        if items:
            _bullet_list(slide, x, Inches(2.2), col_w, Inches(4.6), items,
                         size_pt=15, color_hex=palette.support, font_name=palette.font_name)


def _build_stat_callout_slide(prs: Presentation, slide_spec: dict, palette: Palette) -> None:
    slide = _blank_slide(prs)
    _fill_background(slide, palette.background)
    title = str(slide_spec.get("title") or "").strip()
    _textbox(slide, MARGIN, Inches(0.5), SLIDE_W - 2 * MARGIN, Inches(0.7), title,
              size_pt=28, color_hex=palette.dominant, font_name=palette.font_name, bold=True)

    stats = [s for s in (slide_spec.get("stats") or []) if isinstance(s, dict) and s.get("value")][:4]
    if not stats:
        return
    gap = Inches(0.4)
    usable_w = SLIDE_W - 2 * MARGIN - gap * (len(stats) - 1)
    card_w = usable_w // len(stats)
    card_h = Inches(2.6)
    top = Inches(2.6)
    for i, stat in enumerate(stats):
        left = MARGIN + i * (card_w + gap)
        card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, card_w, card_h)
        card.fill.solid()
        card.fill.fore_color.rgb = _rgb(palette.card_tint)
        card.line.fill.background()
        card.shadow.inherit = False
        tf = card.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = Inches(0.15)
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        p_value = tf.paragraphs[0]
        p_value.alignment = PP_ALIGN.CENTER
        run_value = p_value.add_run()
        run_value.text = str(stat.get("value") or "")
        run_value.font.size = Pt(48)
        run_value.font.bold = True
        run_value.font.name = palette.font_name
        run_value.font.color.rgb = _rgb(palette.accent)
        label = str(stat.get("label") or "").strip()
        if label:
            p_label = tf.add_paragraph()
            p_label.alignment = PP_ALIGN.CENTER
            run_label = p_label.add_run()
            run_label.text = label
            run_label.font.size = Pt(13)
            run_label.font.name = palette.font_name
            run_label.font.color.rgb = _rgb(palette.support)


def _build_chart_slide(prs: Presentation, slide_spec: dict, palette: Palette) -> None:
    slide = _blank_slide(prs)
    _fill_background(slide, palette.background)
    title = str(slide_spec.get("title") or "").strip()
    _textbox(slide, MARGIN, Inches(0.5), SLIDE_W - 2 * MARGIN, Inches(0.7), title,
              size_pt=28, color_hex=palette.dominant, font_name=palette.font_name, bold=True)

    chart_spec = slide_spec.get("chart") or {}
    categories = [str(c) for c in (chart_spec.get("categories") or [])]
    series_list = [s for s in (chart_spec.get("series") or []) if isinstance(s, dict)]
    if not categories or not series_list:
        _textbox(slide, MARGIN, Inches(2.5), SLIDE_W - 2 * MARGIN, Inches(1), "(no chart data provided)",
                  size_pt=16, color_hex=palette.support, font_name=palette.font_name, italic=True,
                  align=PP_ALIGN.CENTER)
        return

    chart_data = CategoryChartData()
    chart_data.categories = categories
    for s in series_list:
        values = [float(v) for v in (s.get("values") or []) if isinstance(v, (int, float))]
        # Pad/truncate to match category count so python-pptx doesn't choke
        # on a mismatched series length — a malformed spec degrades to zeros
        # rather than failing the whole slide.
        if len(values) < len(categories):
            values = values + [0.0] * (len(categories) - len(values))
        chart_data.add_series(str(s.get("name") or "Series"), values[: len(categories)])

    chart_type = _CHART_TYPE_MAP.get(str(chart_spec.get("type") or "").strip().lower(), XL_CHART_TYPE.COLUMN_CLUSTERED)
    graphic_frame = slide.shapes.add_chart(
        chart_type, MARGIN, Inches(1.5), SLIDE_W - 2 * MARGIN, Inches(5.3), chart_data,
    )
    chart = graphic_frame.chart
    chart.has_legend = len(series_list) > 1
    plot = chart.plots[0]
    plot.has_data_labels = True
    plot.data_labels.font.size = Pt(11)
    plot.data_labels.font.color.rgb = _rgb(palette.support)
    for i, series in enumerate(plot.series):
        color = palette.chart_colors[i % len(palette.chart_colors)] if palette.chart_colors else palette.accent
        series.format.fill.solid()
        series.format.fill.fore_color.rgb = _rgb(color)
    try:
        chart.category_axis.tick_labels.font.size = Pt(11)
        chart.category_axis.tick_labels.font.color.rgb = _rgb(palette.support)
        chart.value_axis.tick_labels.font.size = Pt(11)
        chart.value_axis.tick_labels.font.color.rgb = _rgb(palette.support)
    except (ValueError, AttributeError):
        pass  # a pie chart has no value/category axis — cosmetic only, never fatal


def _build_section_divider_slide(prs: Presentation, slide_spec: dict, palette: Palette) -> None:
    slide = _blank_slide(prs)
    _fill_background(slide, palette.divider_background)
    title = str(slide_spec.get("title") or "").strip()
    _textbox(slide, MARGIN, Inches(3.1), SLIDE_W - 2 * MARGIN, Inches(1.3), title,
              size_pt=34, color_hex="FFFFFF", font_name=palette.font_name, bold=True,
              align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    subtitle = str(slide_spec.get("subtitle") or "").strip()
    if subtitle:
        _textbox(slide, MARGIN, Inches(4.4), SLIDE_W - 2 * MARGIN, Inches(0.7), subtitle,
                  size_pt=16, color_hex="D9D9D9", font_name=palette.font_name, italic=True,
                  align=PP_ALIGN.CENTER)


_LAYOUT_BUILDERS = {
    "bullets": _build_bullets_slide,
    "two_column": _build_two_column_slide,
    "stat_callout": _build_stat_callout_slide,
    "chart": _build_chart_slide,
    "section_divider": _build_section_divider_slide,
}


def build_presentation(spec: dict) -> Presentation:
    if not str(spec.get("title") or "").strip():
        raise ValueError("spec.title is required and cannot be empty")
    slides_spec = spec.get("slides") or []

    palette = resolve_palette(spec.get("theme"), spec.get("tone"))
    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H

    _build_title_slide(prs, spec, palette)

    for slide_spec in slides_spec:
        if not isinstance(slide_spec, dict):
            continue
        title = str(slide_spec.get("title") or "").strip()
        has_content = any(
            slide_spec.get(k) for k in ("bullets", "left_bullets", "right_bullets", "stats", "chart", "subtitle")
        )
        if not title and not has_content:
            continue  # skip genuinely empty slide entries rather than emit a blank slide
        layout = str(slide_spec.get("layout") or "bullets").strip().lower()
        builder = _LAYOUT_BUILDERS.get(layout, _build_bullets_slide)  # unrecognized -> bullets, never raises
        builder(prs, slide_spec, palette)

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
