# Tools (Phase 0)

## Local Python code execution

- Runs in a temporary workspace directory, deleted after the run.
- Bounded by `MAX_CODE_EXECUTION_SECONDS` (default 20s).
- Output truncated to a fixed character limit.
- Development-only: not an isolated/production sandbox (see `docs/security.md`).
- Never has access to the app's `.env` or host secrets.
- Gated by HITL: submitting code only queues it; it runs only after explicit approval (see `docs/hitl.md`).

## MCP (Model Context Protocol) tools

Optional, real tool-calling for agent-mode chat turns (`AutoGenOrchestrator.run`,
`app/agents.py`) — distinct from the code-execution tool above, which only
ever queues: an MCP tool genuinely runs when the model decides to call it.
See `app/mcp_tools.py` for the loader and `app/config.py` for every setting
below.

- **Local stdio server** (`MCP_STDIO_ENABLED`, default on) — spawns
  `mcp_servers/general_tools_server.py` with the app's own interpreter: a
  small bundled set of safe utility tools (current UTC time, basic
  arithmetic, text stats, UUID generation) with no filesystem, network, or
  subprocess access of their own. `MCP_STDIO_COMMAND`/`MCP_STDIO_ARGS`
  point it at a different stdio MCP server instead.
- **Remote server** (`MCP_REMOTE_URL`, disabled unless set) — Streamable
  HTTP or SSE transport (`MCP_REMOTE_TRANSPORT`), with an optional
  `MCP_REMOTE_HEADERS` for auth (e.g. a bearer token — one `Key: Value`
  pair per line, never logged).
- **Web search** (`TAVILY_API_KEY`, disabled unless set) — a third, separate
  stdio server (`mcp_servers/tavily_search_server.py`) exposing one
  `web_search` tool backed by the [Tavily Search
  API](https://docs.tavily.com/documentation/api-reference/endpoint/search).
  Unlike the two servers above, it's only ever offered to the model on a
  given turn when the UI's **Web Search** toggle (next to Agent mode) is
  also on for that turn — configuring the key alone doesn't make every
  agent-mode turn search the web. The key reaches the child process only
  through its own environment (never the app's own env, never logged, never
  echoed in a tool result). See `app/mcp_tools.py:get_mcp_tools(web_search=...)`
  and `app/models.py:ChatRequest.web_search`.
- Tools from all servers are loaded once per process and cached (loading
  spawns a subprocess / makes a network call — too slow to repeat every
  turn). A server that fails to load just contributes no tools; it never
  breaks the chat request (`POST /api/tools/mcp/status` shows why, including
  a `web_search` section).
- Gated on a real AutoGen model client existing for whatever's configured
  (same requirement `app/streaming.py`'s direct-chat streaming has) — mock
  mode / missing credentials can't tool-call, so agent-mode falls back to
  the existing plain completion, same as if no tools were configured at all.
