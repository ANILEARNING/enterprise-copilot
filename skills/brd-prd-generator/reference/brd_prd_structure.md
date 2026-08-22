# BRD vs. PRD structure

Both share a common backbone; a PRD goes one level deeper into system/
functional behavior. `doc_type` selects the framing — this doesn't change
*which* sections appear (that's `sections`), only how each section is
written.

## Common backbone (every generated document)

1. **Title page** — project name, document type (BRD/PRD), objective,
   stakeholders, date.
2. **Executive Summary / Elicitation Summary** — a short paragraph
   synthesizing `objective` + `stakeholders` into "why this document exists
   and who it's for."
3. **Scope** — one paragraph, derived from `objective`: what's in scope,
   and (if inferable) what's explicitly out of scope.
4. Then, in order, only the sections the user selected in `sections`
   (see `elicitation_and_modeling_conventions.md` for how to write each):
   - Process/Workflow Mapping (current-state / future-state)
   - SWOT & Gap Analysis
   - User Stories & Acceptance Criteria
   - Use Case Specifications
   - Data & Integration Mapping
   - ROI / Cost-Benefit Analysis
   - Requirements Traceability Matrix

## BRD framing (`doc_type` = "BRD (Business Requirements Document)")

Stay at the business level throughout: business rules, stakeholder needs,
business outcomes. Avoid system implementation detail — a BRD tells
engineering *what the business needs*, not *how to build it*. User stories
still get acceptance criteria, but keep the "so that <benefit>" clause
tied to a business outcome, not a technical one.

## PRD framing (`doc_type` = "PRD (Product Requirements Document)")

Same backbone, but go one level deeper wherever the section allows it:

- **User stories**: add a functional-requirements flavor — what the system
  must actually do to satisfy the story, not just the business benefit.
- **Use cases**: fill out Main Flow / Alternate Flow with real
  system-interaction steps, not just a one-line description.
- **Data & Integration Mapping**: include a rough non-functional note
  (performance, data volume, or security consideration) if the user's
  answer gives enough to infer one — never invent specifics they didn't
  mention.

## What NOT to do

- Don't add a section the user didn't select in `sections`, even if it
  would "round out" the document.
- Don't invent a formal "Non-Functional Requirements" section unless the
  user's answers actually contain non-functional content — folding a
  performance/security note into an existing section (see PRD framing
  above) is fine; inventing a whole new section from nothing is not.
- Don't pad with generic BA boilerplate ("Assumptions", "Glossary",
  "Sign-off") the user never asked for — every section in the generated
  document should trace back to something the user actually answered.
