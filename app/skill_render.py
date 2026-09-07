"""Spec -> chat-text rendering: the "inline" half of TurnPlan.delivery (see
app/agents.py's `delivery` field and docs/agent-routing.md). A file-generating
skill's drafted spec (the same JSON draft_spec()/DeckBuilderOrchestrator
produce for scripts/generate_*.py) is not directly readable — it's shaped for
a generation script, not a person. When the router decides the user wants the
content shown in chat rather than as a downloadable file, something has to
turn that same spec into prose/markdown instead.

There is deliberately no single generic renderer: docx-generator's
`{title, sections: [{heading, paragraphs}]}` and pptx's `{title, slides:
[{layout, ...}]}` are structurally different enough (freeform prose sections
vs. a fixed vocabulary of slide layouts) that a shared renderer would either
under-render one shape or grow branchy trying to cover both. Each supported
skill gets its own small function instead; `render_spec_as_chat_text` is the
only thing callers need to know about.

Coverage is deliberately partial: only docx-generator and pptx (skills/
docx-generator, skills/pptx) render inline today. ppt-generator's spec shape
is a near-duplicate of pptx's (same slide-layout vocabulary, see skills/
ppt-generator/SKILL.md) but is chat-unreachable by design (chat_triggers: []
— only reached via the Skills tab's fixed pre-flight form, which never
consults TurnPlan.delivery), so it's out of scope rather than merely
deferred. brd-prd-generator's spec shape is genuinely more complex
(conditional sections depending on which document type/sections the user
picked) and is deferred to a follow-up pass. `render_spec_as_chat_text`
returns None for both, which every caller treats as "no renderer for this
skill" — fall back to generating the file exactly as if delivery had been
"file" all along, rather than silently producing nothing.
"""
from __future__ import annotations

_SLIDE_LAYOUT_LABELS = {
    "title": "Title slide",
    "bullets": "Bullets",
    "two_column": "Two columns",
    "chart": "Chart",
    "stat_callout": "Stat callout",
    "section_divider": "Section divider",
}


def _render_docx_spec(spec: dict) -> str:
    """docx-generator's spec shape (skills/docx-generator/SKILL.md):
    {"title", "subtitle", "tone", "style", "sections": [{"heading",
    "paragraphs": [...]}]}. Rendered as a plain Markdown document — a
    heading per section, paragraphs as-is — since that's already how the
    chat UI renders assistant messages (see static/app.js's markdown
    rendering)."""
    lines = [f"# {spec.get('title') or 'Untitled document'}"]
    subtitle = spec.get("subtitle")
    if subtitle:
        lines.append(f"*{subtitle}*")
    for section in spec.get("sections") or []:
        heading = section.get("heading")
        if heading:
            lines.append(f"\n## {heading}")
        for paragraph in section.get("paragraphs") or []:
            if paragraph:
                lines.append(f"\n{paragraph}")
    return "\n".join(lines).strip()


def _render_pptx_slide(slide: dict, index: int) -> list[str]:
    layout = slide.get("layout") or "bullets"
    label = _SLIDE_LAYOUT_LABELS.get(layout, layout)
    lines = [f"\n## Slide {index} — {slide.get('title') or label} _{label}_"]

    bullets = slide.get("bullets") or []
    if bullets:
        lines.extend(f"- {b}" for b in bullets)

    left_heading, right_heading = slide.get("left_heading"), slide.get("right_heading")
    if left_heading or right_heading:
        if left_heading:
            lines.append(f"\n**{left_heading}**")
            lines.extend(f"- {b}" for b in slide.get("left_bullets") or [])
        if right_heading:
            lines.append(f"\n**{right_heading}**")
            lines.extend(f"- {b}" for b in slide.get("right_bullets") or [])

    stats = slide.get("stats") or []
    if stats:
        lines.extend(
            f"- **{s.get('value', '')}** — {s.get('label', '')}" for s in stats if isinstance(s, dict)
        )

    chart = slide.get("chart")
    if isinstance(chart, dict):
        categories = chart.get("categories") or []
        series_list = chart.get("series") or []
        chart_desc = f"\n_{chart.get('type', 'chart').capitalize()} chart"
        if categories:
            chart_desc += f" — {', '.join(str(c) for c in categories)}_"
        else:
            chart_desc += "_"
        lines.append(chart_desc)
        for series in series_list:
            if isinstance(series, dict):
                values = ", ".join(str(v) for v in series.get("values") or [])
                lines.append(f"- {series.get('name', 'Series')}: {values}")

    return lines


def _render_pptx_spec(spec: dict) -> str:
    """pptx's spec shape (skills/pptx/SKILL.md): {"title", "subtitle",
    "tone", "theme", "slides": [{"layout", ...}]}. The first `slides` entry
    is never the title slide (that's built from the top-level title/subtitle
    — see the skill's own Workflow step 4), so it's rendered separately here
    too, then every real slide as its own Markdown section headed by its
    layout kind."""
    lines = [f"# {spec.get('title') or 'Untitled deck'}"]
    subtitle = spec.get("subtitle")
    if subtitle:
        lines.append(f"*{subtitle}*")
    for i, slide in enumerate(spec.get("slides") or [], start=1):
        if isinstance(slide, dict):
            lines.extend(_render_pptx_slide(slide, i))
    return "\n".join(lines).strip()


# skill_id -> renderer. Keyed by skill_id (not `output`, which "pptx" and
# "ppt-generator" share) since the spec shapes differ even where the file
# extension doesn't — see this module's docstring.
_RENDERERS = {
    "docx-generator": _render_docx_spec,
    "pptx": _render_pptx_spec,
}


def render_spec_as_chat_text(skill_id: str, spec: dict) -> str | None:
    """Renders a drafted spec as chat-ready Markdown, or None if this skill
    has no renderer yet — callers must treat None as "fall back to
    generating the file," never as an empty/failed render."""
    renderer = _RENDERERS.get(skill_id)
    if renderer is None:
        return None
    return renderer(spec)
