# Elicitation & modeling conventions

How to turn each section's loose answer(s) into the structured JSON
`scripts/generate_brd.py` expects under `spec.sections.<key>`. Only include
a key for a section the user actually selected — see `brd_prd_structure.md`.

The full spec shape (see the docstring at the top of `generate_brd.py` for
the authoritative version):

```json
{
  "meta": {
    "project_name": "...", "doc_type": "BRD (Business Requirements Document)",
    "objective": "...", "stakeholders": "..."
  },
  "elicitation_summary": "One paragraph synthesizing objective + stakeholders.",
  "output_formats": ["docx", "pdf"],
  "sections": { "...": "...(see below, one key per selected section)..." }
}
```

## `sections.user_stories` (from `user_story_personas`)

```json
{"stories": [
  {"persona": "Customer", "goal": "...", "benefit": "...",
   "acceptance_criteria": [{"given": "...", "when": "...", "then": "..."}]}
]}
```
One story per persona/goal the user described. Never fewer than one
Given/When/Then triple per story — a story with no acceptance criteria is
incomplete, not acceptable output.

## `sections.workflow` (from `current_state_process` + `future_state_process`)

```json
{"current_state": ["Step 1: ...", "Step 2: ..."],
 "future_state": ["Step 1: ...", "Step 2: ..."],
 "summary": "One sentence on what changes and why."}
```
Numbered, step-by-step prose — this is a document generator, not a
diagramming tool, so render the workflow as a clear textual sequence
(optionally noting the responsible actor/swimlane per step, e.g. "Step 2
(Support team): ..."), never claim an actual BPMN diagram was produced.

## `sections.swot` (from `swot_context`)

```json
{"strengths": ["..."], "weaknesses": ["..."], "opportunities": ["..."],
 "threats": ["..."], "gap_analysis": "Current state vs. desired state, and what closes the gap."}
```
Every quadrant should have at least one real item derived from
`swot_context` — don't leave a quadrant empty; if the answer doesn't
clearly support all four, make a reasonable, clearly-grounded inference
rather than fabricating something unrelated.

## `sections.use_cases` (from `use_cases`)

```json
{"use_cases": [
  {"name": "...", "actor": "...", "preconditions": "...",
   "main_flow": ["1. ...", "2. ..."], "alternate_flow": "...",
   "postconditions": "..."}
]}
```
One entry per use case the user listed. `alternate_flow` may be a short
"None specified" if the user's answer doesn't suggest one — don't invent an
elaborate alternate path from nothing.

## `sections.data_integration` (from `systems_and_apis`)

```json
{"systems": ["System/API name — role in the flow", "..."],
 "data_flow": "Plain-language description of how data moves between them."}
```

## `sections.roi` (from `roi_inputs`)

```json
{"costs": [{"item": "...", "amount": "..."}],
 "benefits": [{"item": "...", "amount": "..."}],
 "payback_note": "One sentence, only if inferable from the user's numbers — never fabricated."}
```
Every cost/benefit `amount` must come directly from what the user typed in
`roi_inputs` — this section is the one place fabrication is most tempting
and least acceptable; if the user gave no usable numbers, keep the
list short/honest rather than inventing figures.

## `sections.traceability` (from `traceability_scope`)

```json
{"rows": [
  {"req_id": "REQ-001", "description": "...", "source": "...",
   "test_case_id": "TC-001", "status": "Not Started"}
]}
```
Synthesize sequential `req_id`/`test_case_id` values (REQ-001, REQ-002,
TC-001, TC-002, ...) from the requirements/scope described in
`traceability_scope` — the IDs themselves are a reasonable, expected
synthesis (a matrix with no IDs is useless), but the requirement
`description`s must trace back to what the user actually described.
`status` defaults to `"Not Started"` unless the user's answer implies
otherwise.
