"""Enterprise document styles shared by both renderers in generate_brd.py
(python-docx for .docx, reportlab for .pdf) — same Style-dataclass shape as
skills/docx-generator/scripts/style_presets.py, kept as its own copy rather
than a cross-skill import since each skill package is self-contained per
this project's convention (skills are uploadable/removable independently).

Business documents call for a narrower, more conservative palette than a
marketing doc or slide deck — no "playful"/"editorial" options here, just
three professional variants.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Style:
    name: str
    heading_font: str
    heading_color: str    # hex, no '#'
    body_font: str
    body_color: str
    accent_color: str     # table header fill, section rules
    table_header_text: str = "FFFFFF"  # text color on the accent-filled table header row
    body_size_pt: int = 10
    heading_size_pt: int = 18


STYLES: dict[str, Style] = {
    "formal": Style(
        name="formal", heading_font="Georgia", heading_color="0B2545",
        body_font="Calibri", body_color="1B2A4A", accent_color="13315C",
    ),
    "minimal": Style(
        name="minimal", heading_font="Calibri", heading_color="1A1A1A",
        body_font="Calibri", body_color="262626", accent_color="2F6FED",
    ),
    "corporate-blue": Style(
        name="corporate-blue", heading_font="Verdana", heading_color="0F172A",
        body_font="Calibri", body_color="1E293B", accent_color="0EA5E9",
    ),
}

DEFAULT_STYLE = "formal"


def resolve_style(style_name: str | None) -> Style:
    """Resolves a style name (case-insensitive) to a Style, falling back to
    DEFAULT_STYLE. Never raises — an unrecognized style name degrades to the
    default rather than failing generation, same posture as
    docx-generator's resolve_style."""
    if style_name and style_name.strip().lower() in STYLES:
        return STYLES[style_name.strip().lower()]
    return STYLES[DEFAULT_STYLE]


if __name__ == "__main__":
    for style in STYLES.values():
        print(f"{style.name:15s} heading=#{style.heading_color} body=#{style.body_color} "
              f"accent=#{style.accent_color} fonts={style.heading_font}/{style.body_font}")
