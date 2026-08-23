---
name: pptx
description: >
  Creates a richer PowerPoint (.pptx) presentation than a basic title+bullets
  deck — native charts, two-column and stat-callout layouts, section
  dividers, and a topic-driven color palette — asking about content, tone,
  and design before generating it.
trigger: >
  The user asks to create/build/generate a presentation, slide deck, or
  PPT/PowerPoint, especially when they want charts, varied layouts, or a
  richer/more polished design than a simple bullet deck.
# Precise phrases for chat-message routing (CopilotService._match_chat_skill) —
# deliberately narrower/more specific than `trigger` above, same reasoning as
# docx-generator/SKILL.md's chat_triggers comment. This is the chat-routing
# target for deck requests — ppt-generator/SKILL.md's own chat_triggers are
# cleared to keep routing deterministic rather than accidental (first-match-
# wins over dict order otherwise).
chat_triggers:
  - "ppt"
  - "pptx"
  - "powerpoint"
  - "presentation"
  - "slide deck"
  - "create slides"
  - "make slides"
  - "generate slides"
  - "build me a deck"
  - "deck about"
license: Proprietary. LICENSE.txt has complete terms
output: pptx
questions:
  - id: topic
    prompt: "What's the presentation about?"
    type: text
    required: true
  - id: audience
    prompt: "Who is this for?"
    type: select
    options: ["Executives", "My team", "Clients", "General public", "Students"]
    allow_other: true
    required: true
  - id: tone
    prompt: "What tone should it have?"
    type: select
    options: ["Formal", "Casual", "Persuasive", "Technical", "Inspirational"]
    required: true
  - id: design
    prompt: "Any design/theme preference?"
    type: select
    options: ["Midnight Executive", "Forest & Moss", "Coral Energy", "Ocean Gradient", "Charcoal Minimal", "No preference — pick based on tone"]
    required: false
  - id: length
    prompt: "About how many slides?"
    type: text
    placeholder: "e.g. 6-8"
    required: false
  - id: sections
    prompt: "Any specific sections/points that must be included?"
    type: text
    required: false
  - id: include_charts
    prompt: "Should any slides include charts (e.g. trends, comparisons, stats)?"
    type: select
    options: ["Yes", "No", "Only if the content is naturally numeric"]
    required: false
  - id: chart_data
    prompt: "Briefly describe the data/numbers to chart (e.g. \"Q1-Q4 revenue: 10, 15, 22, 30\")"
    type: text
    required: false
    show_if: {question_id: include_charts, equals: "Yes"}
  - id: layout_style
    prompt: "Layout preference?"
    type: select
    options: ["Varied (mixed layouts)", "Mostly bullets, occasional visual", "Bold/visual-heavy"]
    required: false
---

# pptx (richer deck generator)

Builds a themed, visually varied `.pptx` file using native charts, multiple
layouts, and a topic-driven color palette — richer than a plain
title-and-bullets deck. Ask the questions above (as a pre-flight form, or
conversationally if the surrounding flow doesn't support a form) before
generating anything — don't guess at topic, audience, or tone.

## Workflow

1. **Ask** the pre-flight questions above. `topic`, `audience`, and `tone`
   are required; skip asking about the rest only if the user already stated
   them unprompted.
2. **Resolve the palette**: use `design` if given; otherwise derive one from
   `tone` (see `reference/design_themes.md`). Pick one palette per deck and
   apply it consistently across every slide.
3. **Build the outline**: turn `topic` + `sections` + `length` into a slide
   list, varying layout — don't put every section on a plain bullets slide.
   See `reference/outline_conventions.md` for slide count/pacing, and
   `reference/design_themes.md` for which layout fits which content (a
   comparison of two things → `two_column`; a headline number →
   `stat_callout`; a metric trend or category comparison with real numbers →
   `chart`; a topic pivot → `section_divider`).
4. **Write the spec** as JSON matching the shape documented at the top of
   `scripts/generate_pptx.py`:
   ```json
   {"title": "...", "subtitle": "...", "tone": "...", "theme": "palette name",
    "slides": [
      {"layout": "title|bullets|two_column|chart|stat_callout|section_divider",
       "title": "...", "bullets": ["..."], "icon": "check|target|growth|idea|warning|people|money|clock",
       "left_heading": "...", "left_bullets": ["..."], "right_heading": "...", "right_bullets": ["..."],
       "stats": [{"value": "42%", "label": "..."}],
       "chart": {"type": "bar|column|line|pie", "categories": ["..."], "series": [{"name": "...", "values": [1, 2, 3]}]}}
    ]}
   ```
   The first array entry is never the title slide — that's always built from
   the top-level `title`/`subtitle`, same as every other field this skill
   generates from.
5. **Generate**: run `scripts/generate_pptx.py --output <path>.pptx` with the
   spec piped via stdin.
6. **Hand back** the file (or its path) to the user, and briefly state the
   palette, slide count, and layouts used so they can ask for a redo if it's
   off.

## Design principles

Apply these while building the outline, not just when picking the palette —
see `reference/design_themes.md` for the full 10-palette table.

- Pick a bold, topic-specific palette — not generic blue. One color
  dominates (60-70% visual weight), 1-2 supporting tones, one sharp accent.
- Vary layout across slides — never repeat the same layout twice in a row.
- Give every content slide a visual element where the content supports one:
  a number on its own → `stat_callout`; 2+ comparable items → `two_column`;
  any numeric series the user gave real data for → `chart`. Don't force a
  chart onto qualitative content.
- Left-align body text; center only titles and section dividers.
- Icons render as a colored circle with a bold glyph (see
  `scripts/pptx_themes.py`'s `ICONS` map) — never as a color bar or accent
  stripe under a title.
- Dark backgrounds read as premium for title/section-divider slides; keep
  content slides on a lighter ground unless the user asked for dark
  throughout.

## If the user wants changes after seeing it

Don't regenerate from scratch — ask specifically what to change (a slide's
content, the tone, the palette, the length), update only the affected part
of the spec, and re-run the script. Re-running `generate_pptx.py` is cheap
and idempotent — it always builds a fresh file from the spec.

## Constraints

- Never fabricate the topic/audience/tone if the user hasn't answered — ask,
  don't assume.
- Keep bullets terse (3-5 per slide, phrases not paragraphs) — this
  generates slides, not a document.
- Use `chart` only when the content is genuinely numeric/comparative; a
  chart with fabricated numbers is worse than no chart.
- If `design` names something outside the built-in palettes, pick the
  closest match and say so explicitly rather than silently swapping in an
  unrelated one.

## Files

- `scripts/generate_pptx.py` — builds the `.pptx` from a JSON spec
  (python-pptx + `scripts/pptx_themes.py` only, no other dependency).
- `scripts/pptx_themes.py` — the ten palette definitions, tone→palette
  fallback mapping, and the icon glyph map.
- `reference/design_themes.md` — palette table and layout-selection
  guidance.
- `reference/outline_conventions.md` — turning answers into a slide-by-slide,
  layout-varied outline.

The remaining scripts in this directory (`add_slide.py`, `clean.py`,
`thumbnail.py`, `office/`) support manually editing an *existing* deck or
template and are not part of this generation pipeline — unrelated to the
Workflow above.
