"""app/skill_render.py: spec -> chat-text rendering for the "inline" half of
TurnPlan.delivery (see app/agents.py and docs/agent-routing.md)."""
from app.skill_render import render_spec_as_chat_text


def test_unsupported_skill_returns_none():
    # ppt-generator (chat-unreachable by design) and brd-prd-generator
    # (deferred — more complex conditional-section shape) have no renderer
    # yet; callers must treat None as "fall back to generating the file."
    assert render_spec_as_chat_text("ppt-generator", {"title": "x"}) is None
    assert render_spec_as_chat_text("brd-prd-generator", {"title": "x"}) is None
    assert render_spec_as_chat_text("unknown-skill", {}) is None


def test_docx_spec_renders_title_and_sections():
    spec = {
        "title": "Q3 Board Update", "subtitle": "Prepared for the exec team",
        "sections": [
            {"heading": "Overview", "paragraphs": ["Revenue grew 22% quarter over quarter."]},
            {"heading": "Risks", "paragraphs": ["Supply chain delays.", "Hiring lag in EMEA."]},
        ],
    }
    text = render_spec_as_chat_text("docx-generator", spec)
    assert text.startswith("# Q3 Board Update")
    assert "*Prepared for the exec team*" in text
    assert "## Overview" in text
    assert "Revenue grew 22% quarter over quarter." in text
    assert "## Risks" in text
    assert "Supply chain delays." in text
    assert "Hiring lag in EMEA." in text


def test_docx_spec_handles_missing_optional_fields():
    # No subtitle, a section with no heading, a section with no paragraphs —
    # none of this should raise or render a literal "None"/"null".
    spec = {"title": "Minimal Doc", "sections": [{"paragraphs": ["Just some text."]}, {"heading": "Empty"}]}
    text = render_spec_as_chat_text("docx-generator", spec)
    assert "None" not in text
    assert "null" not in text
    assert "Just some text." in text
    assert "## Empty" in text


def test_docx_spec_with_no_sections_still_renders_title():
    text = render_spec_as_chat_text("docx-generator", {"title": "Bare Doc"})
    assert text == "# Bare Doc"


def test_pptx_spec_renders_title_slide_separately_from_slides_list():
    # The first `slides` entry is never the title slide (see the pptx skill's
    # own Workflow step 4) — the top-level title/subtitle render once, then
    # every real slide gets its own section.
    spec = {
        "title": "2026 Roadmap", "subtitle": "Product & Engineering",
        "slides": [
            {"layout": "bullets", "title": "Q1 Priorities", "bullets": ["Ship the new dashboard", "Reduce latency"]},
        ],
    }
    text = render_spec_as_chat_text("pptx", spec)
    assert text.startswith("# 2026 Roadmap")
    assert "*Product & Engineering*" in text
    assert "Slide 1 — Q1 Priorities" in text
    assert "- Ship the new dashboard" in text
    assert "- Reduce latency" in text


def test_pptx_spec_renders_two_column_layout():
    spec = {"title": "Deck", "slides": [{
        "layout": "two_column", "title": "Comparison",
        "left_heading": "Before", "left_bullets": ["Slow"],
        "right_heading": "After", "right_bullets": ["Fast"],
    }]}
    text = render_spec_as_chat_text("pptx", spec)
    assert "**Before**" in text
    assert "- Slow" in text
    assert "**After**" in text
    assert "- Fast" in text


def test_pptx_spec_renders_stat_callout():
    spec = {"title": "Deck", "slides": [{
        "layout": "stat_callout", "title": "Growth",
        "stats": [{"value": "42%", "label": "YoY revenue growth"}],
    }]}
    text = render_spec_as_chat_text("pptx", spec)
    assert "**42%** — YoY revenue growth" in text


def test_pptx_spec_renders_chart_slide():
    spec = {"title": "Deck", "slides": [{
        "layout": "chart", "title": "Sales",
        "chart": {"type": "bar", "categories": ["Q1", "Q2"], "series": [{"name": "Revenue", "values": [10, 20]}]},
    }]}
    text = render_spec_as_chat_text("pptx", spec)
    assert "Bar chart" in text
    assert "Q1, Q2" in text
    assert "Revenue: 10, 20" in text


def test_pptx_spec_handles_malformed_slide_entries_gracefully():
    # A non-dict entry in `slides` (a malformed/hallucinated spec) must not
    # crash rendering — just skip it.
    spec = {"title": "Deck", "slides": ["not a dict", {"layout": "bullets", "title": "Real slide", "bullets": ["ok"]}]}
    text = render_spec_as_chat_text("pptx", spec)
    assert "Real slide" in text
