"""Document styles for the docx-generator skill.

Analogous to ppt-generator/scripts/theme_presets.py, but for a printed/read
document rather than a slide: styling here is typography and accent color,
not a colored background — a "dark" full-page background makes sense for a
slide, not for a document meant to be read or printed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Style:
    name: str
    heading_font: str
    heading_color: str   # hex, no '#'
    body_font: str
    body_color: str
    accent_color: str    # used for the title-page rule and pull quotes
    body_size_pt: int = 11
    heading_size_pt: int = 20


STYLES: dict[str, Style] = {
    "minimal": Style(
        name="minimal", heading_font="Calibri", heading_color="1A1A1A",
        body_font="Calibri", body_color="262626", accent_color="2F6FED",
    ),
    "corporate": Style(
        name="corporate", heading_font="Georgia", heading_color="0B2545",
        body_font="Calibri", body_color="1B2A4A", accent_color="13315C",
    ),
    "academic": Style(
        name="academic", heading_font="Cambria", heading_color="3B2F2F",
        body_font="Cambria", body_color="2B2320", accent_color="7A5C3E",
    ),
    "modern": Style(
        name="modern", heading_font="Verdana", heading_color="0F172A",
        body_font="Calibri", body_color="1E293B", accent_color="0EA5E9",
    ),
    "editorial": Style(
        name="editorial", heading_font="Georgia", heading_color="7A0C2E",
        body_font="Georgia", body_color="2A2A2A", accent_color="C81D4A",
    ),
}

DEFAULT_STYLE = "minimal"

# Tone -> suggested style, mirroring theme_presets.TONE_TO_THEME (see
# ppt-generator/reference/design_themes.md for the reasoning behind each pairing;
# the doc-specific pairings below are chosen for the same tone, adapted to a
# document rather than a slide).
TONE_TO_STYLE: dict[str, str] = {
    "formal": "corporate",
    "casual": "modern",
    "persuasive": "editorial",
    "technical": "minimal",
    "inspirational": "academic",
}


def resolve_style(style_name: str | None, tone: str | None = None) -> Style:
    """Resolves a style name (case-insensitive) to a Style, falling back to
    the tone-based suggestion, then to DEFAULT_STYLE. Never raises — an
    unrecognized style name degrades to the default rather than failing
    generation."""
    if style_name and style_name.strip().lower() in STYLES:
        return STYLES[style_name.strip().lower()]
    if tone and tone.strip().lower() in TONE_TO_STYLE:
        return STYLES[TONE_TO_STYLE[tone.strip().lower()]]
    return STYLES[DEFAULT_STYLE]


def list_styles() -> list[str]:
    return list(STYLES.keys())


if __name__ == "__main__":
    for style in STYLES.values():
        print(f"{style.name:10s} heading=#{style.heading_color} body=#{style.body_color} "
              f"accent=#{style.accent_color} fonts={style.heading_font}/{style.body_font}")
