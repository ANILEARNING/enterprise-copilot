"""Design palettes for the pptx skill's generator (scripts/generate_pptx.py).

Ten named palettes (dominant/support/accent + a chart-color ramp + a card
tint for stat-callout backgrounds) — richer than skills/ppt-generator's five
fixed themes, deliberately NOT shared with that skill's own
scripts/theme_presets.py: these are two separate skill packages with
separate root_dirs, and app/skills.py's run_generation_script invokes each
skill's generator as a scrubbed-env subprocess with cwd=skill.root_dir, so a
cross-skill import wouldn't resolve even if it were architecturally
desirable (either skill's zip/directory can be deleted or re-uploaded
independently of the other).

Same "never raises, degrades to a sane default" contract as ppt-generator's
resolve_theme — an unrecognized palette/tone/icon name is a formatting
choice this module makes silently, never a reason to fail generation.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Palette:
    name: str
    background: str       # hex, no '#' — the deck's light/content background
    dominant: str          # hex — the color carrying the most visual weight (titles, dark grounds)
    support: str           # hex — body text / secondary elements
    accent: str            # hex — the one sharp accent (icons, chart highlight, callout numbers)
    card_tint: str         # hex — subtle background for stat-callout cards (never a literal accent stripe)
    chart_colors: tuple[str, ...] = field(default_factory=tuple)
    font_name: str = "Calibri"
    divider_background: str = ""  # hex — section-divider/title slide ground; defaults to `dominant` if unset

    def __post_init__(self) -> None:
        if not self.divider_background:
            object.__setattr__(self, "divider_background", self.dominant)


PALETTES: dict[str, Palette] = {
    "midnight_executive": Palette(
        name="midnight_executive", background="FFFFFF", dominant="1E2761", support="2B2B2B",
        accent="4A6FE3", card_tint="EEF1FC",
        chart_colors=("1E2761", "4A6FE3", "8FA8F0", "CADCFC"), font_name="Georgia",
    ),
    "forest_moss": Palette(
        name="forest_moss", background="FFFFFF", dominant="2C5F2D", support="33402F",
        accent="6B8E23", card_tint="EEF4E9",
        chart_colors=("2C5F2D", "5A8F3C", "97BC62", "C7DDAE"), font_name="Calibri",
    ),
    "coral_energy": Palette(
        name="coral_energy", background="FFFFFF", dominant="2F3C7E", support="3A3A3A",
        accent="F96167", card_tint="FDEEEC",
        chart_colors=("2F3C7E", "F96167", "F9A26C", "F9E795"), font_name="Verdana",
    ),
    "warm_terracotta": Palette(
        name="warm_terracotta", background="FFFFFF", dominant="B85042", support="4A3B33",
        accent="A7BEAE", card_tint="F6EDE9",
        chart_colors=("B85042", "D98B6F", "A7BEAE", "E7E8D1"), font_name="Georgia",
    ),
    "ocean_gradient": Palette(
        name="ocean_gradient", background="FFFFFF", dominant="21295C", support="1C3A4B",
        accent="1C7293", card_tint="EAF2F5",
        chart_colors=("21295C", "065A82", "1C7293", "9FD8DF"), font_name="Calibri",
    ),
    "charcoal_minimal": Palette(
        name="charcoal_minimal", background="FFFFFF", dominant="36454F", support="4A4A4A",
        accent="212121", card_tint="F2F2F2",
        chart_colors=("36454F", "6E7B85", "A9B4BC", "212121"), font_name="Calibri",
    ),
    "teal_trust": Palette(
        name="teal_trust", background="FFFFFF", dominant="028090", support="2F3A3A",
        accent="02C39A", card_tint="E7F6F3",
        chart_colors=("028090", "00A896", "02C39A", "A1E8D6"), font_name="Calibri",
    ),
    "berry_cream": Palette(
        name="berry_cream", background="FFFFFF", dominant="6D2E46", support="4A3138",
        accent="A26769", card_tint="F6EEE6",
        chart_colors=("6D2E46", "A26769", "C99A9A", "ECE2D0"), font_name="Georgia",
    ),
    "sage_calm": Palette(
        name="sage_calm", background="FFFFFF", dominant="50808E", support="3B4A4E",
        accent="69A297", card_tint="EDF3F1",
        chart_colors=("50808E", "69A297", "84B59F", "C3DCD0"), font_name="Calibri",
    ),
    "cherry_bold": Palette(
        name="cherry_bold", background="FFFFFF", dominant="990011", support="2B2B2B",
        accent="2F3C7E", card_tint="FCEDEE",
        chart_colors=("990011", "2F3C7E", "C94C4C", "8FA8F0"), font_name="Verdana",
    ),
}

DEFAULT_PALETTE = "midnight_executive"

# Tone -> suggested palette, used only when the user has no design preference
# — mirrors ppt-generator's TONE_TO_THEME mapping, same fallback role.
TONE_TO_PALETTE: dict[str, str] = {
    "formal": "midnight_executive",
    "casual": "coral_energy",
    "persuasive": "cherry_bold",
    "technical": "charcoal_minimal",
    "inspirational": "ocean_gradient",
}

# Aliases so the SKILL.md's own question options ("Midnight Executive", ...)
# and casual phrasing ("navy", "forest") both resolve without the caller
# needing to know the exact snake_case key.
_ALIASES: dict[str, str] = {
    "midnight executive": "midnight_executive",
    "forest & moss": "forest_moss",
    "forest and moss": "forest_moss",
    "coral energy": "coral_energy",
    "warm terracotta": "warm_terracotta",
    "ocean gradient": "ocean_gradient",
    "charcoal minimal": "charcoal_minimal",
    "teal trust": "teal_trust",
    "berry & cream": "berry_cream",
    "berry and cream": "berry_cream",
    "sage calm": "sage_calm",
    "cherry bold": "cherry_bold",
}

# Single-codepoint glyphs only — every one of these is in the Basic Latin /
# common symbol ranges every font PowerPoint ships covers, so there's no
# font-availability risk the way a multi-codepoint emoji sequence would
# carry. An icon key with no match here still renders (see
# generate_pptx.py's icon-circle builder) as a plain filled circle with no
# glyph — never a missing/broken-glyph box.
ICONS: dict[str, str] = {
    "check": "✓",     # ✓
    "target": "●",    # ●
    "growth": "▲",    # ▲
    "idea": "✦",      # ✦
    "warning": "!",
    "people": "•",    # • (kept deliberately plain — a two-person glyph has no safe single codepoint)
    "money": "$",
    "clock": "◔",     # ◔
}


def resolve_palette(name: str | None, tone: str | None = None) -> Palette:
    """Resolves a palette name (case-insensitive, alias-aware) to a Palette,
    falling back to the tone-based suggestion, then to DEFAULT_PALETTE.
    Never raises — an unrecognized name degrades to the default rather than
    failing generation, same contract as ppt-generator's resolve_theme."""
    if name:
        key = name.strip().lower()
        key = _ALIASES.get(key, key)
        if key in PALETTES:
            return PALETTES[key]
    if tone and tone.strip().lower() in TONE_TO_PALETTE:
        return PALETTES[TONE_TO_PALETTE[tone.strip().lower()]]
    return PALETTES[DEFAULT_PALETTE]


def resolve_icon(name: str | None) -> str | None:
    """A safe glyph for `name`, or None if unrecognized/absent — the caller
    renders a plain circle with no glyph in that case, never raises."""
    if not name:
        return None
    return ICONS.get(name.strip().lower())


def list_palettes() -> list[str]:
    return list(PALETTES.keys())


if __name__ == "__main__":
    for palette in PALETTES.values():
        print(f"{palette.name:20s} dominant=#{palette.dominant} accent=#{palette.accent} font={palette.font_name}")
