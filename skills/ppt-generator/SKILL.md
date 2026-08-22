---
name: ppt-generator
description: >
  Creates a formatted PowerPoint (.pptx) presentation from a topic, asking the
  user about content, tone, and design before generating it.
trigger: >
  The user asks to create/build/generate a presentation, slide deck, or PPT/PowerPoint.
# Precise phrases for chat-message routing (CopilotService._match_chat_skill) —
# see docx-generator/SKILL.md's chat_triggers comment for why this is narrower
# than `trigger` above.
chat_triggers:
  - "ppt"
  - "pptx"
  - "powerpoint"
  - "presentation"
  - "slide deck"
  - "create slides"
  - "make slides"
  - "generate slides"
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
    options: ["Minimal", "Corporate", "Vibrant", "Dark", "Playful", "No preference — pick based on tone"]
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
---

# ppt-generator

Builds a themed `.pptx` file. Ask the questions above (as a pre-flight form, or
conversationally if the surrounding flow doesn't support a form) before generating anything —
don't guess at topic, audience, or tone.

## Workflow

1. **Ask** the pre-flight questions above. `topic`, `audience`, and `tone` are required; skip
   asking about the rest only if the user already stated them unprompted.
2. **Resolve the theme**: use `design` if given; otherwise derive it from `tone` (see
   `reference/design_themes.md`).
3. **Build the outline**: turn `topic` + `sections` + `length` into a slide list — a title
   slide, an agenda slide (usually), one slide per section/topic, and a closing slide. Follow
   `reference/outline_conventions.md` for slide count, bullets-per-slide, and how tone should
   shape the actual bullet wording, not just the color scheme.
4. **Write the spec** as JSON matching the shape documented at the top of
   `scripts/generate_ppt.py`:
   ```json
   {"title": "...", "subtitle": "...", "tone": "...", "theme": "...",
    "slides": [{"title": "...", "bullets": ["...", "..."]}]}
   ```
5. **Generate**: run `scripts/generate_ppt.py --input <spec.json> --output <path>.pptx`.
6. **Hand back** the file (or its path) to the user, and briefly state the theme/slide count
   chosen so they can ask for a redo if it's off.

## If the user wants changes after seeing it

Don't regenerate from scratch — ask specifically what to change (content of a slide, tone,
theme, length), update only the affected part of the spec, and re-run the script. Re-running
`generate_ppt.py` is cheap and idempotent (it always builds a fresh file from the spec), so
prefer "adjust the spec, regenerate" over trying to patch a `.pptx` file directly.

## Constraints

- Never fabricate the topic/audience/tone if the user hasn't answered — ask, don't assume.
- Keep bullets terse (see `reference/outline_conventions.md`) — this generates slides, not a
  document; dense paragraph-length bullets are a failure of this skill, not a style choice.
- If `design` names something outside the five built-in themes, pick the closest match and say
  so explicitly rather than silently swapping in an unrelated theme.

## Files

- `scripts/generate_ppt.py` — builds the `.pptx` from a JSON spec (standalone, only depends on
  `python-pptx` and `scripts/theme_presets.py`).
- `scripts/theme_presets.py` — the five theme definitions + tone→theme fallback mapping.
- `reference/design_themes.md` — theme details and the tone→theme table.
- `reference/outline_conventions.md` — how to turn answers into a slide-by-slide outline.
