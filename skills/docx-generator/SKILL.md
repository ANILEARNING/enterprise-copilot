---
name: docx-generator
description: >
  Creates a formatted Word (.docx) document from a topic, asking the user about
  document type, content, tone, and style before generating it.
trigger: >
  The user asks to create/write/generate a document, report, proposal, memo,
  whitepaper, meeting notes, or Word doc.
# Precise phrases for chat-message routing (CopilotService._match_chat_skill) —
# deliberately narrower/more specific than `trigger` above, which is prose for
# humans and the general-purpose skill-package matcher. A bare word like "word"
# or "report" alone is too common in ordinary chat to route on safely.
chat_triggers:
  - "docx"
  - "word doc"
  - "word document"
  - "create a document"
  - "write a document"
  - "generate a document"
  - "create a report"
  - "write a report"
  - "create a proposal"
  - "write a proposal"
  - "create a memo"
  - "write a memo"
  - "whitepaper"
output: docx
questions:
  - id: topic
    prompt: "What's the document about?"
    type: text
    required: true
  - id: doc_type
    prompt: "What kind of document is this?"
    type: select
    options: ["Report", "Proposal", "Memo", "Whitepaper", "Meeting notes", "Letter"]
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
  - id: style
    prompt: "Any design/style preference?"
    type: select
    options: ["Minimal", "Corporate", "Academic", "Modern", "Editorial", "No preference — pick based on tone"]
    required: false
  - id: length
    prompt: "About how long? (pages, or word count)"
    type: text
    placeholder: "e.g. 2-3 pages"
    required: false
  - id: sections
    prompt: "Any specific sections/points that must be included?"
    type: text
    required: false
---

# docx-generator

Builds a styled `.docx` file. Ask the questions above (as a pre-flight form, or
conversationally if the surrounding flow doesn't support a form) before generating anything —
don't guess at topic, document type, audience, or tone.

## Workflow

1. **Ask** the pre-flight questions above. `topic`, `doc_type`, `audience`, and `tone` are
   required; skip asking about the rest only if the user already stated them unprompted.
2. **Resolve the style**: use `style` if given; otherwise derive it from `tone` (see
   `reference/document_styles.md`).
3. **Build the outline**: turn `topic` + `doc_type` + `sections` + `length` into a section
   list. `doc_type` determines the *default* section structure (a report's sections differ
   from a memo's or a letter's) — follow `reference/outline_conventions.md` for the
   per-type shape, paragraph length, and how tone should shape the prose itself, not just
   the visual style.
4. **Write the spec** as JSON matching the shape documented at the top of
   `scripts/generate_docx.py`:
   ```json
   {"title": "...", "subtitle": "...", "tone": "...", "style": "...",
    "sections": [{"heading": "...", "paragraphs": ["...", "..."]}]}
   ```
5. **Generate**: run `scripts/generate_docx.py --input <spec.json> --output <path>.docx`.
6. **Hand back** the file (or its path) to the user, and briefly state the style/section
   count chosen so they can ask for a redo if it's off.

## If the user wants changes after seeing it

Don't regenerate from scratch — ask specifically what to change (a section's content, tone,
style, length), update only the affected part of the spec, and re-run the script. Re-running
`generate_docx.py` is cheap and idempotent (it always builds a fresh file from the spec), so
prefer "adjust the spec, regenerate" over trying to patch a `.docx` file directly.

## Constraints

- Never fabricate the topic/audience/tone/doc_type if the user hasn't answered — ask, don't
  assume.
- Match section structure to `doc_type` (see `reference/outline_conventions.md`) — a memo
  padded out with report-style sections, or a letter broken into headed sections, is a
  failure of this skill, not a style choice.
- If `style` names something outside the five built-in styles, pick the closest match and say
  so explicitly rather than silently swapping in an unrelated style.

## Files

- `scripts/generate_docx.py` — builds the `.docx` from a JSON spec (standalone, only depends
  on `python-docx` and `scripts/style_presets.py`).
- `scripts/style_presets.py` — the five style definitions + tone→style fallback mapping.
- `reference/document_styles.md` — style details and the tone→style table.
- `reference/outline_conventions.md` — per-`doc_type` section structure and prose conventions.
