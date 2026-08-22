# Phase 0 Guardrails

Guardrails are included in Phase 0 and are visible in the Copilot UI on every turn — the
composer chip, the "Guardrails" side panel, the "Last run" panel, and a per-message
"Guardrail activity" disclosure (`static/app.js`) all read live from the same
`guardrails` payload every chat response carries.

`GuardrailService` (`app/services.py`) is pattern/regex-based by design, matching the
rest of v1's "always-available, no external dependency" posture (the same reasoning as
the hash embedder in `app/retrieval.py`). Every check returns structured findings, not
just allow/block, so the UI can show exactly what guardrail activity happened — never
the matched text itself, only a category name and count.

## Checks

**Prompt-injection screening** (`check_input`, `check_context`) — blocks known
injection phrasing ("ignore previous instructions", "reveal system prompt", ...) in the
user's message, and separately screens retrieved RAG chunks for the same patterns
(indirect prompt injection from a compromised document — distinct check, see
`docs/rag.md`).

**PII detection & redaction** (`check_input`, `check_output`) — email, phone number,
SSN, credit card, and IP address. Detected values are **redacted, not blocked**: the
turn proceeds with `[REDACTED_EMAIL]`/`[REDACTED_SSN]`/etc. in place of the raw value,
so the model itself never sees (on input) or ever gets to repeat (on output) the
original PII. `check_input`'s `redacted_text` is what actually reaches the model,
session history, and the observability trace — the raw value is never persisted.

**Sensitive-data / secrets filtering** (`check_input`, `check_output`) — `key=value`
shaped secrets (`api_key=`, `password=`, `Bearer <token>`), PEM private-key blocks, and
provider-shaped literal keys (AWS access keys, OpenAI-style `sk-...` keys). Same
redact-not-block treatment as PII.

**Toxic / unsafe-content policy** (`check_input`, `check_output`) — keyword-family
policy grouped by category (violence, self-harm, hate/harassment, illegal activity).
This one **blocks** the turn outright, same as prompt injection — there's no safe
redaction of "how do I build a bomb," only refusal.

## Flow

```
User Input
→ check_input (prompt injection blocks; PII/secrets redacted; unsafe content blocks)
→ AI/Agent  (retrieved RAG context passes through check_context first — indirect injection)
→ check_output (unsafe content blocks; PII/secrets redacted)
→ UI Response + guardrail activity shown inline
```

A streaming reply is the one case redaction can't fully undo: by the time
`check_output` runs, every token has already been sent to the client live
(`app/streaming.py`). The stored/returned copy is still redacted, and the guardrail
activity panel still reports the finding — so a leak in a streamed reply is visible
even though it couldn't be intercepted mid-stream. See the `chat_stream` output-check
comment in `app/services.py`.

## What the UI shows

- **Composer chip** — pass/fail plus a redaction count when something was caught but
  didn't block the turn.
- **Per-message "🛡️ Guardrail activity" disclosure** — one line per finding
  (`PII redacted — email ×1`, `Sensitive data redacted — api_key ×1`, `Unsafe content
  policy — violence ×1`), never the matched text.
- **"Last run" side panel** — a "Guardrail activity" summary row plus the same
  detail block.

## Status endpoint

`POST /api/guardrails/status` reports the check list and a human-readable description
— read by the sidebar pill and the Settings tab, not hardcoded in the frontend.

This is a lightweight baseline. Production deployment should add a stronger policy
engine (e.g. a real PII/toxicity model behind this same `GuardrailService` contract),
authentication/authorization, rate limiting, audit logging, and isolated execution.
