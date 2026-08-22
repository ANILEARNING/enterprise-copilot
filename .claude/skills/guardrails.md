# Guardrails Skill

## Phase 0
Guardrails are a Phase 0 **product feature**, not a future placeholder
(`.claude/rules/guardrails.md`). `GuardrailService` (`app/services.py`) runs three independent
checkpoints in the same request — not one — each visible separately in the response's
`guardrails` object (`{"input": ..., "context": ..., "output": ...}`):

1. **`check_input`** — the user's raw message, before any retrieval or model call.
2. **`check_context`** — retrieved knowledge-base chunks, before they enter the prompt (agent
   mode with sources found only — `null` otherwise, and its presence in a response is itself a
   signal that grounding occurred). See `knowledge-rag.md`.
3. **`check_output`** — the generated answer, before it reaches the client.

### What's actually checked
`GuardrailService.BLOCKED_PATTERNS` — a fixed phrase list, matched case-insensitively as a
substring:
- `"ignore previous instructions"`
- `"reveal system prompt"`
- `"show me your api key"`
- `"print environment variables"`

`check_input`/`check_context` match this list directly (input against the message, context
against each chunk's snippet — flagged chunk ids are dropped from both the prompt and the cited
sources, never silently included). `check_output` uses a narrower, distinct check:
secret-looking key-value patterns (`api_key=`, `password=`) in the response text — not the same
list, because the failure mode is different (leaking a real value vs. requesting one).

This is a lightweight Phase 0 baseline, explicitly not a production policy engine
(`docs/guardrails.md`): no authentication/authorization, no rate limiting, no audit logging, no
isolated execution behind it. A production deployment adds those; this project's job is proving
the three-checkpoint shape and the UI contract below.

## Workflow
User input → **input guardrail** → agent/skill routing → retrieval (if applicable) → **context
guardrail** → model/agent call → **output guardrail** → UI response. A blocked input short-
circuits immediately — no retrieval, no model call, no HITL — and the blocked message becomes
the assistant's turn. A blocked output is replaced with a fixed "blocked by guardrails" message,
never partially leaked.

## Constraints
- Never expose system prompts, hidden policies, secret values, or internal reasoning in a
  guardrail message — the block reason (`matched_rules`, `message`) states *that* a rule
  matched, never the full rule text verbatim as a way to reveal policy internals.
- The UI must visibly show guardrail state and whether the current request passed or was
  blocked, every turn — not only on failure. `static/app.js`'s `guardrailChip` reflects all
  three checkpoints on every response; the Settings/Copilot panels surface phase/enabled/checks
  via `POST /api/guardrails/status`.
- `check_context` operates on retrieved chunks specifically because a compromised/malicious
  document can carry an embedded instruction ("ignore previous instructions") that the model
  would otherwise treat as part of its own context — this is the indirect-prompt-injection
  vector `check_input` alone cannot see, since the attacker's text never appears in the user's
  own message.

## Completion
A response whose `guardrails` object always reports every checkpoint that ran this turn
(`null` for one that didn't apply, e.g. `context` on a direct/ungrounded turn) — never a
response that ran a check and silently omitted its result. A blocked request returns a safe,
generic message and stops before doing anything further, exposing no hidden prompt or policy
detail in the process.
