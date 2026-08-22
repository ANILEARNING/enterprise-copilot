---
name: brd-prd-generator
description: >
  Creates a Business Requirements Document (BRD) or Product Requirements
  Document (PRD) from stakeholder answers — elicitation summary, user
  stories with acceptance criteria, BPMN-style workflow narrative,
  SWOT/gap analysis, use case specs, data & integration mapping, ROI
  cost-benefit modeling, and a requirements traceability matrix — as a
  real Word (.docx) and/or PDF file, chosen at generation time.
trigger: >
  The user (typically a Business Analyst) asks to create/write/generate a
  BRD, PRD, requirements document, or a specific BA artifact such as a
  traceability matrix, user stories with acceptance criteria, a use case
  spec, or an ROI/cost-benefit analysis.
# Precise phrases for chat-message routing (CopilotService._match_chat_skill)
# — deliberately narrower/more specific than `trigger` above, same reasoning
# as docx-generator/SKILL.md's chat_triggers comment: a bare word like
# "requirements" alone is too common in ordinary chat to route on safely.
chat_triggers:
  - "brd"
  - "prd"
  - "business requirements document"
  - "product requirements document"
  - "requirements document"
  - "create a brd"
  - "write a brd"
  - "generate a brd"
  - "create a prd"
  - "write a prd"
  - "generate a prd"
  - "traceability matrix"
  - "user stories with acceptance criteria"
  - "requirements elicitation"
# The multi-format sentinel (app/skills.py:MULTI_FORMAT_OUTPUT) — this
# skill's own script decides which real file(s) to write per the answered
# output_format question below, not a single fixed extension.
output: docx+pdf
questions:
  - id: project_name
    prompt: "What's the project or initiative called?"
    type: text
    required: true
  - id: objective
    prompt: "What's the business objective — what problem does this solve, in one or two sentences?"
    type: text
    required: true
  - id: stakeholders
    prompt: "Who are the key stakeholders / sponsors / affected teams?"
    type: text
    required: true
  - id: doc_type
    prompt: "Is this a BRD (business-level) or a full PRD (product/technical-level)?"
    type: select
    options: ["BRD (Business Requirements Document)", "PRD (Product Requirements Document)"]
    required: true
  - id: output_format
    prompt: "Which file format do you want?"
    type: select
    options: ["Word (.docx)", "PDF", "Both"]
    required: true
  - id: reference_material
    prompt: "Have any existing requirements doc, meeting notes, or process doc to ground this in? (optional)"
    type: file
    accept: ["docx", "pdf", "txt", "md"]
    required: false

  - id: sections
    prompt: "Which sections should this document include?"
    type: multiselect
    required: true
    options:
      - "User Stories & Acceptance Criteria"
      - "Process/Workflow Mapping (BPMN-style)"
      - "SWOT & Gap Analysis"
      - "Use Case Specifications"
      - "Data & Integration Mapping"
      - "ROI / Cost-Benefit Analysis"
      - "Requirements Traceability Matrix"

  # --- User Stories & Acceptance Criteria ---
  - id: user_story_personas
    prompt: "Who are the user personas / roles, and what are their main goals? (one per line is fine)"
    type: text
    required: true
    show_if: {question_id: sections, includes: "User Stories & Acceptance Criteria"}

  # --- Process/Workflow Mapping ---
  - id: current_state_process
    prompt: "Describe the CURRENT-state process/workflow (as-is, step by step)."
    type: text
    required: true
    show_if: {question_id: sections, includes: "Process/Workflow Mapping (BPMN-style)"}
  - id: future_state_process
    prompt: "Describe the desired FUTURE-state process/workflow (to-be)."
    type: text
    required: true
    show_if: {question_id: sections, includes: "Process/Workflow Mapping (BPMN-style)"}

  # --- SWOT & Gap Analysis ---
  - id: swot_context
    prompt: "Briefly describe the current situation this SWOT/gap analysis should cover."
    type: text
    required: true
    show_if: {question_id: sections, includes: "SWOT & Gap Analysis"}

  # --- Use Case Specifications ---
  - id: use_cases
    prompt: "List the main system use cases / interactions to specify (one per line, e.g. 'Actor: Customer — Submit an order')."
    type: text
    required: true
    show_if: {question_id: sections, includes: "Use Case Specifications"}

  # --- Data & Integration Mapping ---
  - id: systems_and_apis
    prompt: "Which systems, databases, or APIs are involved, and roughly how should data flow between them?"
    type: text
    required: true
    show_if: {question_id: sections, includes: "Data & Integration Mapping"}

  # --- ROI / Cost-Benefit Analysis ---
  - id: roi_inputs
    prompt: "Rough cost items and expected benefits/savings (e.g. 'Dev cost: $50k; Savings: $20k/yr in manual effort')."
    type: text
    required: true
    show_if: {question_id: sections, includes: "ROI / Cost-Benefit Analysis"}

  # --- Requirements Traceability Matrix ---
  - id: traceability_scope
    prompt: "Which requirements should the traceability matrix cover? (paste a list, or describe the scope)"
    type: text
    required: true
    show_if: {question_id: sections, includes: "Requirements Traceability Matrix"}
---

# brd-prd-generator

Builds a structured BRD or PRD as a real `.docx` and/or `.pdf`, from stakeholder answers
covering elicitation, process modeling, and BA-specific artifacts (user stories, use cases,
traceability, ROI). Ask the questions above (as a pre-flight form, or conversationally if the
surrounding flow doesn't support a form) before generating anything — this is a formal business
document; don't invent scope, stakeholders, or requirements the user hasn't stated.

## Workflow

1. **Ask** the core questions (`project_name`, `objective`, `stakeholders`, `doc_type`,
   `output_format` — all required) plus `sections` (which BA artifacts to include). Only ask a
   section's follow-up question if that section was selected in `sections`.
2. **Resolve document type**: `doc_type` determines section framing — a BRD stays business-level
   (stakeholder needs, scope, business rules); a PRD goes deeper into functional/non-functional
   requirements and system behavior. See `reference/brd_prd_structure.md` for the canonical
   section ordering for each.
3. **Structure each selected section** per `reference/elicitation_and_modeling_conventions.md`:
   - User stories: `As a <persona>, I want <goal>, so that <benefit>`, each with Given/When/Then
     acceptance criteria — never a bare feature list.
   - Process/workflow: a textual current-state vs. future-state narrative (numbered steps per
     swimlane/actor) — this generates a document, not a BPMN diagram file, so keep it as
     structured prose/steps, not an attempt at BPMN XML.
   - SWOT/gap analysis: four SWOT quadrants plus an explicit gap-analysis paragraph (current vs.
     desired state, and what closes the gap).
   - Use cases: Actor / Preconditions / Main Flow / Alternate Flow / Postconditions per use case.
   - Data & integration: a systems/APIs list plus a plain-language data-flow description.
   - ROI: a cost-benefit table (cost items, benefit items, rough payback period) — real numbers
     the user gave, not fabricated figures.
   - Traceability matrix: Requirement ID / Description / Source / Test Case ID / Status rows —
     synthesize plausible IDs (REQ-001, TC-001) from the requirements the user described.
4. **Write the spec** as JSON matching the shape documented at the top of
   `scripts/generate_brd.py` — a `meta` block, an `elicitation_summary`, and a `sections` dict
   keyed by the same section names as the `sections` question's options, each holding that
   section's structured content. Set `output_formats` per the answered `output_format` (the
   generation script also derives this itself if omitted — see script docstring).
5. **Generate**: run `scripts/generate_brd.py --input <spec.json> --output <path>` — writes
   `<path>.docx` and/or `<path>.pdf` depending on `output_formats`.
6. **Hand back** the file(s) to the user, and briefly state which sections were included so they
   can ask for additions if something's missing.

## If the user wants changes after seeing it

Don't regenerate from scratch — ask specifically what to change (a section's content, which
sections to include, the output format), update only the affected part of the spec, and re-run
the script. Re-running `generate_brd.py` is cheap and idempotent, so prefer "adjust the spec,
regenerate" over trying to patch a `.docx`/`.pdf` file directly.

## Constraints

- Never fabricate stakeholders, business objectives, or requirements the user hasn't stated —
  ask, don't assume. Traceability-matrix requirement IDs and use-case names may be synthesized
  from what the user described, but the underlying requirements themselves must come from the
  user's answers.
- Only include sections the user actually selected in `sections` — an unselected section must
  not appear in the generated document, even if the model has an opinion that it "should."
- Keep the process/workflow section as structured narrative, not a claim of an actual BPMN
  diagram — this skill has no diagramming capability.
- ROI figures must come from what the user provided (`roi_inputs`) — don't invent cost/benefit
  numbers.

## Files

- `scripts/generate_brd.py` — builds `.docx` and/or `.pdf` from a JSON spec (python-docx for
  Word, reportlab for PDF; only depends on those two plus `scripts/style_presets.py`).
- `scripts/style_presets.py` — enterprise-appropriate style definitions shared by both renderers.
- `reference/brd_prd_structure.md` — canonical BRD vs. PRD section ordering/naming.
- `reference/elicitation_and_modeling_conventions.md` — how to structure each BA artifact
  (user stories, workflow narrative, SWOT, use cases, data mapping, ROI, traceability).
