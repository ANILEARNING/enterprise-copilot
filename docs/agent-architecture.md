# Agent Architecture

Use a small set of capable agents. Prefer direct model calls for simple tasks and agent orchestration only for multi-step work.

Agents should use reusable skills and centralized tools.

## v1 implementation (`app/agents.py`, `app/providers.py`)

- **AgentRegistry** — registers `AgentDefinition`s (`general`, `coding-agent`, `research-agent`,
  `data-analysis-agent`), each mirroring the corresponding `.claude/agents/*.md` purpose. Selection
  is LLM-based (`AgentRegistry.select_llm`) — a small classifier model (`settings.agent_router_model`,
  default a Gemma 4 model served via the Gemini API) picks the agent for every agent-mode turn,
  defaulting to `general` for simple, single-step requests. Falls back to the original keyword-
  based matching (`AgentRegistry.select`) whenever the router is unavailable or fails — no
  `GEMINI_API_KEY` configured, a rate limit, a network error, or an unparseable response — so
  routing never breaks a chat turn (see `docs/agent-routing.md`).
- **SkillRegistry** — registers `SkillDefinition`s mirroring `.claude/skills/*.md` (`coding`,
  `knowledge-rag`, `data-analysis`). An agent's `skills` tuple names which ones it invokes.
- **Tool invocation** — the coding agent never runs code inline: it submits a HITL request
  (`HitlService.submit_code_execution`) and the orchestrator reports the pending `request_id`;
  execution only happens after `/api/hitl/decide` approves it, via a real `CodeExecutorAgent`
  (`app/hitl_agents.py`, `docs/hitl.md`) — and, for agent-mode SSE turns specifically, the wait itself
  can be live (a real `UserProxyAgent`) instead of only ever reporting "queued". Separately, every
  agent-mode turn also gets real MCP (Model Context Protocol) tools when configured
  (`app/mcp_tools.py`, `docs/tools.md`) — a local stdio server (bundled general-purpose tools, on by
  default) and/or a remote server. When tools are available and a real AutoGen model client can be
  built for the configured provider, the orchestrator runs one `AssistantAgent` tool-calling turn
  (`AutoGenOrchestrator._run_with_tools`) instead of a plain completion, streaming each tool call/result
  out as a real progress event; mock mode or missing credentials fall straight through to the plain
  completion, same as if no tools were configured. Agent names use hyphens
  (`coding-agent`) — AutoGen requires a valid Python identifier for an agent's `name`, so this is
  sanitized (`.replace("-", "_")`) only for the AutoGen agent object; `OrchestrationResult.agent` (what
  the API/UI sees) keeps the original name.
- **Skill invocation** — the research agent's `knowledge-rag` skill calls `RAGStore.search`, which runs
  the full ingestion→retrieval pipeline (extract/clean/chunk/embed/index, then hybrid vector+BM25
  retrieval, reciprocal rank fusion, and a lexical reranker — see `docs/rag.md`). Retrieved chunks are
  screened by `GuardrailService.check_context` for indirect prompt injection before they ground the
  prompt; the model cites sources as `[filename#chunk_index]`.
- **Session/context management** — `SessionStore` holds the full per-session transcript; every provider
  call (direct, agent-mode, real streaming, tool-calling) is grounded through `compact_history()`/
  `CompactingChatCompletionContext` (`app/memory.py`, `docs/memory.md`) instead of a hard recent-N-turns
  window: the last `app.memory.BUFFER_SIZE` (5) turns verbatim, plus a running compact summary of
  everything older, persisted across restarts via `SessionStore.set_field(..., "memory_state", ...)`.
- **Multimodal** — chat turns accept optional image attachments (`docs/multimodal.md`); both configured
  providers and both real-AutoGen paths (direct streaming, tool-calling) support vision, degrading
  gracefully (same as any other provider/model failure) when the configured model actually can't see
  images.
- **AI provider abstraction** (`app/providers.py`) — `AIProvider` is provider-agnostic; `MockProvider`
  is always available, `GeminiProvider`/`OllamaProvider` call the configured backend, and
  `FallbackProvider` wraps a configured provider so missing credentials or a provider outage degrade
  to the mock provider instead of failing the request. `build_provider()` selects mock vs. fallback
  based on `AI_MODE`. Verified live against real credentials: Gemini (`gemini-flash-latest`, API key
  sent via `x-goog-api-key` header, never the URL) and Ollama Cloud (`https://ollama.com`, `Authorization:
  Bearer <key>` — the same provider also works against a local, unauthenticated Ollama daemon). Fallback
  failures are logged server-side with full detail; the client only ever sees the exception class name.
- **Output size cap** (`MAX_OUTPUT_TOKENS`, default 256) — every live call caps generated length, so
  configured-mode requests (including test runs against real credentials) stay fast and cheap. Gemini
  additionally disables its hidden "thinking" budget (`thinkingConfig.thinkingBudget: 0`) so the cap
  applies to visible text, not invisible reasoning tokens; without it, or with too small a cap on a
  reasoning model (e.g. Ollama's `gpt-oss`), the model can spend the whole budget thinking and return
  no visible text at all — both providers detect that empty-content case and raise, so `FallbackProvider`
  degrades to mock instead of returning a silently blank response.
- **AutoGen boundary** — `AgentOrchestrator` is the framework-independent contract. `AutoGenOrchestrator`
  is the only class that imports `autogen_agentchat` types (`TextMessage`, used to represent
  conversation turns). A future MAF orchestrator implements the same `AgentOrchestrator.run()` contract
  and can replace `AutoGenOrchestrator` in `CopilotService` without touching routes, models, or the UI.
