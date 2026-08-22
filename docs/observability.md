# Observability

Optional Langfuse tracing (`app/observability.py`) — a no-op tracer unless
both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set. `LANGFUSE_BASE_URL`
defaults to Langfuse Cloud's EU region (`https://cloud.langfuse.com`) — set it
to `https://us.cloud.langfuse.com` for a US-region account, or a self-hosted
instance's URL. (`LANGFUSE_HOST` also works — it's the SDK's own deprecated
alias for the same setting.) Never required for the app to run.

One trace per chat turn (`CopilotService.chat` / the real-streaming branch
of `chat_stream`), covering every path: input blocked, skill Q&A/generation,
agent-mode (including MCP tool calls), and plain direct chat. Nested inside
each trace:

- a **span/event** per real progress step already emitted for the UI's
  thinking-row (see `app/agents.py`, `app/skills.py`, `app/mcp_tools.py`) —
  this is the same `on_event` stream, not separately maintained tracing
  code, so anything visible in the chat UI's live progress is also visible
  in Langfuse
- a **generation** observation per completed model call (provider, model,
  prompt/response preview, whether it fell back to mock)

A tracing failure (client not reachable, bad credentials) is always logged
and swallowed — it degrades to untraced, never breaks the chat turn it was
describing.

`POST /api/observability/status` reports whether tracing is enabled (never
the secret key) — shown in the Settings tab.
