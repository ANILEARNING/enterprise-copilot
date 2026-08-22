#!/usr/bin/env python3
"""brd-prd-generator skill — builds a BRD/PRD as .docx and/or .pdf from a
JSON spec.

Usage:
    python generate_brd.py --input spec.json --output out
    cat spec.json | python generate_brd.py --output out   # spec via stdin

`--output` is a BASE path with no extension — this script appends the real
extension itself for each format it writes (out.docx, out.pdf), since one
run can produce more than one file (see app/skills.py:MULTI_FORMAT_OUTPUT /
run_generation_script, which invokes this script exactly once and collects
whichever "<output>.<ext>" files exist afterward).

Spec shape (see ../reference/elicitation_and_modeling_conventions.md for how
to build one from answers to the skill's pre-flight questions):
{
  "meta": {"project_name": "...", "doc_type": "BRD (...)", "objective": "...",
            "stakeholders": "..."},
  "elicitation_summary": "One paragraph.",
  "output_formats": ["docx", "pdf"],       // which files to write; defaults to ["docx"]
  "style": "formal",                        // one of style_presets.STYLES, optional
  "sections": {
    "user_stories": {"stories": [{"persona": "...", "goal": "...", "benefit": "...",
                       "acceptance_criteria": [{"given": "...", "when": "...", "then": "..."}]}]},
    "workflow": {"current_state": ["..."], "future_state": ["..."], "summary": "..."},
    "swot": {"strengths": ["..."], "weaknesses": ["..."], "opportunities": ["..."],
             "threats": ["..."], "gap_analysis": "..."},
    "use_cases": {"use_cases": [{"name": "...", "actor": "...", "preconditions": "...",
                    "main_flow": ["..."], "alternate_flow": "...", "postconditions": "..."}]},
    "data_integration": {"systems": ["..."], "data_flow": "..."},
    "roi": {"costs": [{"item": "...", "amount": "..."}],
            "benefits": [{"item": "...", "amount": "..."}], "payback_note": "..."},
    "traceability": {"rows": [{"req_id": "...", "description": "...", "source": "...",
                       "test_case_id": "...", "status": "..."}]}
  }
}
Every key under "sections" is optional — only the sections the user
selected are present, and only those are rendered.

Standalone script: only depends on python-docx, reportlab, and
scripts/style_presets.py — invokable directly by a human, a test, or the
agent's tool-call layer without any of this skill's other machinery,
mirroring skills/docx-generator/scripts/generate_docx.py and
skills/ppt-generator/scripts/generate_ppt.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from style_presets import Style, resolve_style  # noqa: E402

SECTION_TITLES = {
    "user_stories": "User Stories & Acceptance Criteria",
    "workflow": "Process / Workflow Mapping",
    "swot": "SWOT & Gap Analysis",
    "use_cases": "Use Case Specifications",
    "data_integration": "Data & Integration Mapping",
    "roi": "ROI / Cost-Benefit Analysis",
    "traceability": "Requirements Traceability Matrix",
}
# Rendering order — independent of dict insertion order in the spec, so a
# reordered/regenerated spec still renders consistently.
SECTION_ORDER = list(SECTION_TITLES)


# Section keys as spelled out in SKILL.md's `sections` question options —
# an LLM asked to "match the spec shape" doesn't always use the short
# internal key (roi, traceability, ...); it sometimes uses the
# human-readable label it was just shown instead. Accept both.
_SECTION_LABEL_TO_KEY = {label: key for key, label in SECTION_TITLES.items()}


def _stringify(value) -> str:
    """Coerces any JSON value to display text — an LLM asked for a string
    field sometimes returns a list or nested object instead (e.g.
    meta.stakeholders as ["Support Ops", "IT"] rather than "Support Ops,
    IT"). Never let a raw Python repr leak into the document."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ", ".join(_stringify(v) for v in value if v not in (None, ""))
    if isinstance(value, dict):
        # Most likely an elicitation_summary/description returned as a
        # structured object instead of prose — join its own string-ish
        # values into one readable line rather than showing {'a': 'b'}.
        parts = [_stringify(v) for v in value.values() if v not in (None, "", {}, [])]
        return " ".join(parts)
    return str(value)


def _normalize_spec(spec: dict) -> dict:
    """Defensive normalization pass, run before validation/rendering — an
    LLM's JSON, even when it parses, doesn't always match the documented
    shape exactly (wrong section key spelling, a string field returned as a
    list/object, ...). This never raises; it coerces toward the documented
    shape so a close-but-imperfect model response still renders a real,
    readable document instead of silently dropping sections or leaking a
    Python repr into the text."""
    meta = dict(spec.get("meta") or {})
    for field in ("project_name", "doc_type", "objective", "stakeholders"):
        if field in meta:
            meta[field] = _stringify(meta[field])
    spec["meta"] = meta
    spec["elicitation_summary"] = _stringify(spec.get("elicitation_summary"))

    raw_sections = spec.get("sections") or {}
    if isinstance(raw_sections, dict):
        normalized = {}
        for key, value in raw_sections.items():
            resolved_key = key if key in SECTION_TITLES else _SECTION_LABEL_TO_KEY.get(key, key)
            if resolved_key in SECTION_TITLES and isinstance(value, dict):
                normalized[resolved_key] = _SECTION_NORMALIZERS.get(resolved_key, lambda v: v)(value)
        spec["sections"] = normalized
    else:
        spec["sections"] = {}
    return spec


def _normalize_money_items(raw, *item_key_aliases: str, amount_key_aliases=("amount", "cost", "value")) -> list[dict]:
    """Coerces a list of {item/amount}-shaped dicts, tolerant of an LLM's
    alternate key names (e.g. cost_items/benefit_items each holding
    {item, amount, currency} instead of the documented {item, amount})."""
    out = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        item = next((entry[k] for k in item_key_aliases if entry.get(k)), "")
        amount = next((entry[k] for k in amount_key_aliases if entry.get(k)), "")
        currency = entry.get("currency", "")
        amount_text = _stringify(amount)
        if currency and currency.lower() not in amount_text.lower():
            amount_text = f"{amount_text} {currency}".strip()
        out.append({"item": _stringify(item), "amount": amount_text})
    return out


def _normalize_roi(section: dict) -> dict:
    costs = section.get("costs") if isinstance(section.get("costs"), list) else section.get("cost_items")
    benefits = section.get("benefits") if isinstance(section.get("benefits"), list) else section.get("benefit_items")
    payback = section.get("payback_note")
    if not payback and section.get("payback_period_years") not in (None, ""):
        payback = f"Estimated payback period: {_stringify(section['payback_period_years'])} years."
    return {
        "costs": _normalize_money_items(costs, "item", "name", "description"),
        "benefits": _normalize_money_items(benefits, "item", "name", "description"),
        "payback_note": _stringify(payback),
    }


def _normalize_traceability(section: dict) -> dict:
    rows = []
    for row in section.get("rows") or []:
        if not isinstance(row, dict):
            continue
        rows.append({
            "req_id": _stringify(row.get("req_id") or row.get("requirement_id") or row.get("id")),
            "description": _stringify(row.get("description")),
            "source": _stringify(row.get("source")),
            "test_case_id": _stringify(row.get("test_case_id") or row.get("test_id")),
            "status": _stringify(row.get("status")) or "Not Started",
        })
    return {"rows": rows}


def _normalize_generic_strings(section: dict) -> dict:
    """Fallback normalizer for every section without a bespoke one above —
    recursively coerces any stray non-string leaf value to text (a list
    field staying a list, a dict field staying a dict, but any scalar that
    should be a string gets _stringify'd) so an unexpected type anywhere
    else in the spec degrades to readable text instead of a crash or a
    raw repr. Section-specific renderers still read known keys directly;
    this only guards against surprising *types*, not missing keys."""
    def coerce(value):
        if isinstance(value, dict):
            return {k: coerce(v) for k, v in value.items()}
        if isinstance(value, list):
            return [coerce(v) for v in value]
        return value
    return coerce(section)


_SECTION_NORMALIZERS = {
    "roi": _normalize_roi,
    "traceability": _normalize_traceability,
    "user_stories": _normalize_generic_strings,
    "workflow": _normalize_generic_strings,
    "swot": _normalize_generic_strings,
    "use_cases": _normalize_generic_strings,
    "data_integration": _normalize_generic_strings,
}


def _validate(spec: dict) -> dict:
    spec = _normalize_spec(spec)
    meta = spec.get("meta") or {}
    if not str(meta.get("project_name", "")).strip():
        raise ValueError("spec.meta.project_name is required and cannot be empty")
    sections = spec.get("sections") or {}
    if not isinstance(sections, dict):
        raise ValueError("spec.sections must be an object")
    return spec


# --- DOCX builder --------------------------------------------------------------

def _build_docx(spec: dict, style: Style, output_path: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    def rgb(hex_color: str) -> RGBColor:
        return RGBColor.from_string(hex_color)

    def style_named(document: Document, style_name: str, font_name: str, color_hex: str,
                     size_pt: int, bold: bool = False):
        s = document.styles[style_name]
        s.font.name = font_name
        s.font.size = Pt(size_pt)
        s.font.color.rgb = rgb(color_hex)
        s.font.bold = bold
        return s

    def add_table(document: Document, headers: list[str], rows: list[list[str]]) -> None:
        table = document.add_table(rows=1, cols=len(headers))
        table.style = "Light Grid Accent 1"
        for cell, header in zip(table.rows[0].cells, headers):
            cell.text = str(header)
            for p in cell.paragraphs:
                for run in p.runs:
                    run.font.bold = True
        for row_values in rows:
            cells = table.add_row().cells
            for cell, value in zip(cells, row_values):
                cell.text = str(value)

    meta = spec.get("meta") or {}
    sections = spec.get("sections") or {}
    document = Document()

    style_named(document, "Normal", style.body_font, style.body_color, style.body_size_pt)
    style_named(document, "Title", style.heading_font, style.heading_color, style.heading_size_pt, bold=True)
    style_named(document, "Heading 1", style.heading_font, style.heading_color,
                int(style.heading_size_pt * 0.75), bold=True)
    style_named(document, "Heading 2", style.heading_font, style.heading_color,
                int(style.heading_size_pt * 0.6), bold=True)

    # --- title page ---
    project_name = meta.get("project_name", "")
    doc_type = meta.get("doc_type", "Requirements Document")
    document.add_heading(project_name, level=0)
    subtitle = document.add_paragraph()
    subtitle_run = subtitle.add_run(doc_type)
    subtitle_run.italic = True
    subtitle_run.font.color.rgb = rgb(style.accent_color)
    subtitle_run.font.size = Pt(style.body_size_pt + 1)
    rule = document.add_paragraph()
    rule_run = rule.add_run("—" * 20)
    rule_run.font.color.rgb = rgb(style.accent_color)
    rule.alignment = WD_ALIGN_PARAGRAPH.LEFT

    if meta.get("stakeholders"):
        p = document.add_paragraph()
        p.add_run("Stakeholders: ").bold = True
        p.add_run(str(meta["stakeholders"]))

    # --- elicitation summary / scope ---
    if spec.get("elicitation_summary"):
        document.add_heading("Executive Summary", level=1)
        document.add_paragraph(str(spec["elicitation_summary"]))
    if meta.get("objective"):
        document.add_heading("Scope & Objective", level=1)
        document.add_paragraph(str(meta["objective"]))

    # --- sections ---
    for key in SECTION_ORDER:
        section = sections.get(key)
        if not section:
            continue
        document.add_heading(SECTION_TITLES[key], level=1)

        if key == "user_stories":
            for story in section.get("stories") or []:
                document.add_heading(
                    f"As a {story.get('persona', '')}, I want {story.get('goal', '')}, "
                    f"so that {story.get('benefit', '')}",
                    level=2,
                )
                ac = story.get("acceptance_criteria") or []
                if ac:
                    add_table(document, ["Given", "When", "Then"],
                              [[c.get("given", ""), c.get("when", ""), c.get("then", "")] for c in ac])

        elif key == "workflow":
            if section.get("summary"):
                document.add_paragraph(str(section["summary"]))
            document.add_heading("Current State", level=2)
            for step in section.get("current_state") or []:
                document.add_paragraph(str(step), style="List Number")
            document.add_heading("Future State", level=2)
            for step in section.get("future_state") or []:
                document.add_paragraph(str(step), style="List Number")

        elif key == "swot":
            for quadrant, label in (("strengths", "Strengths"), ("weaknesses", "Weaknesses"),
                                     ("opportunities", "Opportunities"), ("threats", "Threats")):
                document.add_heading(label, level=2)
                for item in section.get(quadrant) or []:
                    document.add_paragraph(str(item), style="List Bullet")
            if section.get("gap_analysis"):
                document.add_heading("Gap Analysis", level=2)
                document.add_paragraph(str(section["gap_analysis"]))

        elif key == "use_cases":
            for uc in section.get("use_cases") or []:
                document.add_heading(uc.get("name", "Use Case"), level=2)
                p = document.add_paragraph()
                p.add_run("Actor: ").bold = True
                p.add_run(str(uc.get("actor", "")))
                p2 = document.add_paragraph()
                p2.add_run("Preconditions: ").bold = True
                p2.add_run(str(uc.get("preconditions", "")))
                if uc.get("main_flow"):
                    document.add_paragraph("Main Flow:").runs[0].bold = True
                    for step in uc["main_flow"]:
                        document.add_paragraph(str(step), style="List Number")
                if uc.get("alternate_flow"):
                    p3 = document.add_paragraph()
                    p3.add_run("Alternate Flow: ").bold = True
                    p3.add_run(str(uc["alternate_flow"]))
                if uc.get("postconditions"):
                    p4 = document.add_paragraph()
                    p4.add_run("Postconditions: ").bold = True
                    p4.add_run(str(uc["postconditions"]))

        elif key == "data_integration":
            if section.get("systems"):
                document.add_heading("Systems & APIs", level=2)
                for item in section["systems"]:
                    document.add_paragraph(str(item), style="List Bullet")
            if section.get("data_flow"):
                document.add_heading("Data Flow", level=2)
                document.add_paragraph(str(section["data_flow"]))

        elif key == "roi":
            costs = section.get("costs") or []
            benefits = section.get("benefits") or []
            if costs:
                document.add_heading("Costs", level=2)
                add_table(document, ["Item", "Amount"], [[c.get("item", ""), c.get("amount", "")] for c in costs])
            if benefits:
                document.add_heading("Benefits", level=2)
                add_table(document, ["Item", "Amount"],
                          [[b.get("item", ""), b.get("amount", "")] for b in benefits])
            if section.get("payback_note"):
                document.add_paragraph(str(section["payback_note"]))

        elif key == "traceability":
            rows = section.get("rows") or []
            if rows:
                add_table(
                    document,
                    ["Req ID", "Description", "Source", "Test Case ID", "Status"],
                    [[r.get("req_id", ""), r.get("description", ""), r.get("source", ""),
                      r.get("test_case_id", ""), r.get("status", "")] for r in rows],
                )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)


# --- PDF builder (reportlab) ----------------------------------------------------

def _build_pdf(spec: dict, style: Style, output_path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "BrdTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        textColor=colors.HexColor(f"#{style.heading_color}"), fontSize=style.heading_size_pt,
    )
    h1_style = ParagraphStyle(
        "BrdH1", parent=styles["Heading1"], fontName="Helvetica-Bold",
        textColor=colors.HexColor(f"#{style.heading_color}"), fontSize=int(style.heading_size_pt * 0.75),
        spaceBefore=14, spaceAfter=6,
    )
    h2_style = ParagraphStyle(
        "BrdH2", parent=styles["Heading2"], fontName="Helvetica-Bold",
        textColor=colors.HexColor(f"#{style.accent_color}"), fontSize=int(style.heading_size_pt * 0.6),
        spaceBefore=10, spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "BrdBody", parent=styles["Normal"], fontName="Helvetica",
        textColor=colors.HexColor(f"#{style.body_color}"), fontSize=style.body_size_pt, leading=style.body_size_pt + 4,
    )
    subtitle_style = ParagraphStyle(
        "BrdSubtitle", parent=body_style, textColor=colors.HexColor(f"#{style.accent_color}"), fontName="Helvetica-Oblique",
    )

    def escape(text: str) -> str:
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def para(text: str, s: ParagraphStyle = body_style) -> Paragraph:
        return Paragraph(escape(text), s)

    def bold_label_para(label: str, value: str, s: ParagraphStyle = body_style) -> Paragraph:
        # `label` is always a literal string this script wrote (never user
        # content) — safe to leave as real markup; `value` is spec content
        # (ultimately from a user's answer) and must be escaped so a stray
        # "<"/"&" in it can't break the paragraph's XML.
        return Paragraph(f"<b>{label}</b> {escape(value)}", s)

    def bullet_list(items: list[str]) -> ListFlowable:
        return ListFlowable([ListItem(para(item)) for item in items], bulletType="bullet")

    def data_table(headers: list[str], rows: list[list[str]]) -> Table:
        table_data = [[para(h, ParagraphStyle("hdr", parent=body_style, textColor=colors.white, fontName="Helvetica-Bold"))
                        for h in headers]]
        table_data += [[para(v) for v in row] for row in rows]
        table = Table(table_data, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(f"#{style.accent_color}")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        return table

    meta = spec.get("meta") or {}
    sections = spec.get("sections") or {}
    story: list = []

    story.append(para(meta.get("project_name", ""), title_style))
    story.append(para(meta.get("doc_type", "Requirements Document"), subtitle_style))
    if meta.get("stakeholders"):
        story.append(Spacer(1, 6))
        story.append(bold_label_para("Stakeholders:", meta["stakeholders"]))
    story.append(Spacer(1, 12))

    if spec.get("elicitation_summary"):
        story.append(para("Executive Summary", h1_style))
        story.append(para(spec["elicitation_summary"]))
    if meta.get("objective"):
        story.append(para("Scope & Objective", h1_style))
        story.append(para(meta["objective"]))

    for key in SECTION_ORDER:
        section = sections.get(key)
        if not section:
            continue
        story.append(para(SECTION_TITLES[key], h1_style))

        if key == "user_stories":
            for s in section.get("stories") or []:
                story.append(para(
                    f"As a {s.get('persona', '')}, I want {s.get('goal', '')}, "
                    f"so that {s.get('benefit', '')}", h2_style,
                ))
                ac = s.get("acceptance_criteria") or []
                if ac:
                    story.append(data_table(
                        ["Given", "When", "Then"],
                        [[c.get("given", ""), c.get("when", ""), c.get("then", "")] for c in ac],
                    ))
                    story.append(Spacer(1, 6))

        elif key == "workflow":
            if section.get("summary"):
                story.append(para(section["summary"]))
            if section.get("current_state"):
                story.append(para("Current State", h2_style))
                story.append(bullet_list([str(s) for s in section["current_state"]]))
            if section.get("future_state"):
                story.append(para("Future State", h2_style))
                story.append(bullet_list([str(s) for s in section["future_state"]]))

        elif key == "swot":
            for quadrant, label in (("strengths", "Strengths"), ("weaknesses", "Weaknesses"),
                                     ("opportunities", "Opportunities"), ("threats", "Threats")):
                items = section.get(quadrant) or []
                if items:
                    story.append(para(label, h2_style))
                    story.append(bullet_list([str(i) for i in items]))
            if section.get("gap_analysis"):
                story.append(para("Gap Analysis", h2_style))
                story.append(para(section["gap_analysis"]))

        elif key == "use_cases":
            for uc in section.get("use_cases") or []:
                story.append(para(uc.get("name", "Use Case"), h2_style))
                story.append(bold_label_para("Actor:", uc.get("actor", "")))
                story.append(bold_label_para("Preconditions:", uc.get("preconditions", "")))
                if uc.get("main_flow"):
                    story.append(para("Main Flow:"))
                    story.append(bullet_list([str(s) for s in uc["main_flow"]]))
                if uc.get("alternate_flow"):
                    story.append(bold_label_para("Alternate Flow:", uc["alternate_flow"]))
                if uc.get("postconditions"):
                    story.append(bold_label_para("Postconditions:", uc["postconditions"]))
                story.append(Spacer(1, 6))

        elif key == "data_integration":
            if section.get("systems"):
                story.append(para("Systems & APIs", h2_style))
                story.append(bullet_list([str(s) for s in section["systems"]]))
            if section.get("data_flow"):
                story.append(para("Data Flow", h2_style))
                story.append(para(section["data_flow"]))

        elif key == "roi":
            costs = section.get("costs") or []
            benefits = section.get("benefits") or []
            if costs:
                story.append(para("Costs", h2_style))
                story.append(data_table(["Item", "Amount"], [[c.get("item", ""), c.get("amount", "")] for c in costs]))
                story.append(Spacer(1, 6))
            if benefits:
                story.append(para("Benefits", h2_style))
                story.append(data_table(["Item", "Amount"], [[b.get("item", ""), b.get("amount", "")] for b in benefits]))
                story.append(Spacer(1, 6))
            if section.get("payback_note"):
                story.append(para(section["payback_note"]))

        elif key == "traceability":
            rows = section.get("rows") or []
            if rows:
                story.append(data_table(
                    ["Req ID", "Description", "Source", "Test Case ID", "Status"],
                    [[r.get("req_id", ""), r.get("description", ""), r.get("source", ""),
                      r.get("test_case_id", ""), r.get("status", "")] for r in rows],
                ))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output_path), pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch, topMargin=0.9 * inch, bottomMargin=0.9 * inch,
    )
    doc.build(story)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, help="Path to the JSON spec (default: read from stdin)")
    parser.add_argument("--output", type=Path, required=True,
                         help="Base output path (no extension) — this script appends .docx/.pdf itself")
    args = parser.parse_args()

    raw = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: input is not valid JSON: {exc}", file=sys.stderr)
        return 1

    try:
        spec = _validate(spec)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    formats = spec.get("output_formats") or ["docx"]
    style = resolve_style(spec.get("style"))
    written = []
    for fmt in formats:
        target = args.output.with_name(args.output.name + f".{fmt}")
        try:
            if fmt == "docx":
                _build_docx(spec, style, target)
            elif fmt == "pdf":
                _build_pdf(spec, style, target)
            else:
                print(f"warning: unrecognized output format {fmt!r}, skipping", file=sys.stderr)
                continue
        except Exception as exc:  # noqa: BLE001 - one format's failure shouldn't stop the other from writing
            print(f"error building {fmt}: {exc}", file=sys.stderr)
            continue
        written.append(target)

    if not written:
        print("error: no output files were produced", file=sys.stderr)
        return 1

    print(f"wrote {', '.join(str(p) for p in written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
