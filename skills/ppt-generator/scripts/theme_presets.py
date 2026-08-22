"""Design themes for the ppt-generator skill.

Each theme is a small, self-contained spec (colors + font) that
`generate_ppt.py` applies to every slide. Kept separate from the
generation script so the skill's design system can be read, referenced,
or extended (see ../reference/design_themes.md) without touching the
script that builds the .pptx file.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str
    background: str      # hex, no '#'
    title_color: str
    body_color: str
    accent_color: str
    font_name: str
    title_size_pt: int = 40
    body_size_pt: int = 20


THEMES: dict[str, Theme] = {
    "minimal": Theme(
        name="minimal", background="FFFFFF", title_color="1A1A1A", body_color="404040",
        accent_color="2F6FED", font_name="Calibri",
    ),
    "corporate": Theme(
        name="corporate", background="F4F6FA", title_color="0B2545", body_color="1B2A4A",
        accent_color="13315C", font_name="Georgia",
    ),
    "vibrant": Theme(
        name="vibrant", background="1B1035", title_color="FFFFFF", body_color="E8E1FF",
        accent_color="FF6B6B", font_name="Verdana",
    ),
    "dark": Theme(
        name="dark", background="121212", title_color="FFFFFF", body_color="D0D0D0",
        accent_color="4ADE80", font_name="Calibri",
    ),
    "playful": Theme(
        name="playful", background="FFF7E6", title_color="D6336C", body_color="4A3728",
        accent_color="FFB703", font_name="Comic Sans MS",
    ),
}

DEFAULT_THEME = "minimal"

# Tone -> suggested theme, used by the skill workflow when the user answers
# the "tone" question but skips (or has no opinion on) the "design" question.
TONE_TO_THEME: dict[str, str] = {
    "formal": "corporate",
    "casual": "playful",
    "persuasive": "vibrant",
    "technical": "minimal",
    "inspirational": "dark",
}


def resolve_theme(theme_name: str | None, tone: str | None = None) -> Theme:
    """Resolves a theme name (case-insensitive) to a Theme, falling back to
    the tone-based suggestion, then to DEFAULT_THEME. Never raises — an
    unrecognized theme name degrades to the default rather than failing
    generation."""
    if theme_name and theme_name.strip().lower() in THEMES:
        return THEMES[theme_name.strip().lower()]
    if tone and tone.strip().lower() in TONE_TO_THEME:
        return THEMES[TONE_TO_THEME[tone.strip().lower()]]
    return THEMES[DEFAULT_THEME]


def list_themes() -> list[str]:
    return list(THEMES.keys())


if __name__ == "__main__":
    for theme in THEMES.values():
        print(f"{theme.name:10s} bg=#{theme.background} title=#{theme.title_color} "
              f"accent=#{theme.accent_color} font={theme.font_name}")
