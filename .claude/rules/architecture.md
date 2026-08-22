# Architecture Rules

- Keep backend and frontend minimal.
- Use service boundaries without unnecessary modules.
- Application code depends on `AgentOrchestrator`, not AutoGen directly.
- Prefer reusable skills/tools over duplicated agent logic.
- Keep infrastructure in-memory/local for v1 unless the specification requires otherwise.
