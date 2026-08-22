# Phase 0 Guardrails

Guardrails are included in Phase 0 and are visible in the Copilot UI.

Current checks:
- prompt-injection indicators
- requests for hidden/system instructions
- obvious secret requests
- secret-like output

Flow:

User Input
→ Input Guardrail
→ AI/Agent
→ Output Guardrail
→ UI Response

This is a lightweight baseline. Production deployment should add a stronger policy engine, authentication/authorization, rate limiting, audit logging and isolated execution.
