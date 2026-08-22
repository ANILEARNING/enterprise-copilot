# Agent routing

Which agent handles an agent-mode turn is decided by `AgentRegistry.select_llm`
(`app/agents.py`) — an LLM classification call, not the older pure-keyword match
(`AgentRegistry.select`, still present and used as the fallback below).

## Why LLM-based

Keyword matching only catches a message that happens to contain one of a fixed
list of trigger words (`code`, `bug`, `debug`, ... for `coding-agent`; `csv`,
`dataset`, `analyze`, ... for `data-analysis-agent`). A genuinely ambiguous
message with none of those words — "walk me through what's going wrong here" —
always fell to `general` under keyword matching, even when a person would
immediately recognize it as a debugging request. The LLM router judges intent
instead of scanning for specific words, at the cost of an extra model call
per turn.

## How it works

1. `AutoGenOrchestrator.run` builds a router provider once per process
   (`_build_router_provider`, cached — never rebuilt per turn) via a raw
   `GeminiProvider` targeting `settings.agent_router_model`. This is
   independent of `MODEL_PROVIDER`/the main chat provider: routing stays
   cheap and fast even when the configured chat provider is Ollama or
   something slower. **Requires `GEMINI_API_KEY` regardless of
   `MODEL_PROVIDER`.**
2. `AgentRegistry.select_llm(task, router_provider)` sends a short
   classification prompt (every registered agent's name + `purpose`, plus
   the user's message) with `json_mode=True`, expecting back exactly
   `{"agent": "<name>"}`.
3. The response is parsed and mapped to a registered `AgentDefinition`.

## Fallback (never breaks chat)

`select_llm` falls back to `select()`'s keyword matching, and reports
`routed_by_llm: False` on the `agent_selected` progress event, whenever:
- `router_provider` is `None` — no `GEMINI_API_KEY` configured, or the
  turn's provider is a bare `MockProvider` (deterministic, no real model
  call — the router is never even built for that case, same contract as
  `get_mcp_tools()`'s `MockProvider` skip).
- The router call raises — rate limit, timeout, network error.
- The response isn't valid JSON, or names an agent that isn't registered.

This is the same graceful-degrade posture as every other provider/tool in
this app (`FallbackProvider`, the embedding chain, MCP tools): a router
failure degrades exactly to today's keyword-matching behavior, never to an
error or a broken turn.

## Model choice: Gemma 4 via the Gemini API

Default `agent_router_model` is `gemma-4-26b-a4b-it` — Google's smallest/
fastest Gemma 4 hosted variant, served through the same
`generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`
endpoint `GeminiProvider` already calls for real Gemini models, just a
different model id.

**Known cost — read before assuming this is a cheap/instant call.** Gemma 4
is a "thinking" model whose hidden reasoning cannot be disabled the way
Gemini's own reasoning models can (`thinkingConfig: {"thinkingBudget": 0}`
returns `400 INVALID_ARGUMENT "Thinking budget is not supported for this
model."` for any `gemma-*` model — verified live). Even a trivial
classification prompt burns real hidden-reasoning tokens before the visible
answer appears — `GeminiProvider.complete()` floors the token budget to
`_GEMMA_THINKING_TOKEN_FLOOR` (512) for any `gemma-*` model id specifically
to survive this, and strips `{"thought": true}` parts from the response
before returning the real answer text. In practice this measured 7-15+
seconds per router call in testing — real, per-agent-mode-turn latency, not
a one-time cost. `AGENT_ROUTER_MODEL=gemini-3.1-flash-lite` (non-reasoning-
forced, documented free-tier limits, sub-second typical latency) is a
one-line `.env` swap if this proves too slow in practice; see
`.env.example` for the exact alternatives.

Real-world free-tier rate limits for Gemma models are not published by
Google as a fixed number — check `https://aistudio.google.com/rate-limit`
for your own key's live limits rather than trusting a hardcoded figure
anywhere in this codebase.
