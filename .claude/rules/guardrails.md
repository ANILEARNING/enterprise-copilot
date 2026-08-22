# Guardrails Rules

Guardrails are a Phase 0 product feature, not a future placeholder.

Every chat request must pass an input check before model/agent execution and an output check before returning the result.

The UI must visibly show guardrail state and whether the current request passed or was blocked.

Do not expose system prompts, hidden policies, secret values, or internal reasoning in guardrail messages.
