# Runtime settings (Settings page → Models panel)

Lets you choose, from the running app, which provider and model each of the four
model-consuming subsystems uses — no `.env` edit or restart required. In-memory only,
per this project's v1 architecture (`.claude/rules/architecture.md`): a process restart
reverts every override back to whatever `.env` says.

## What's editable

| Slot | Setting | Consumed by |
|---|---|---|
| Provider | `model_provider` (`gemini`/`ollama`) | `build_provider()` — the default chat-completion path |
| Chat model | `gemini_model` / `ollama_model` | Same, plus `build_streaming_model_client` (direct-chat streaming, agent-mode tool-calling) |
| RAG embedding | `gemini_embedding_model` / `ollama_embedding_model` | `build_embedding_provider()` — ingestion and query embedding |
| Agent router | `agent_router_model` | `AgentRegistry.select_llm` — always Gemini regardless of `model_provider` (see `docs/agent-routing.md`) |

**Never editable from this UI**: API keys (`GEMINI_API_KEY`, `OLLAMA_API_KEY`),
`OLLAMA_BASE_URL`. Those stay `.env`-only by design — this surface picks among
already-configured credentials, it doesn't manage secrets.

## API

- `POST /api/settings/models` — current effective values, read-only. Includes
  `gemini_configured`/`ollama_configured` (whether each provider even has a usable
  API key/base URL right now) so the UI can grey out a choice that would fail.
- `POST /api/settings/models/update` — body is any subset of `model_provider`,
  `gemini_model`, `ollama_model`, `gemini_embedding_model`, `ollama_embedding_model`,
  `agent_router_model` (`app/models.py:SettingsModelsUpdate`). Only the fields present
  are changed; everything else keeps its current value. Returns the same shape as the
  read endpoint above.

## How it takes effect immediately

`settings` (`app/config.py`) is a plain mutable `pydantic-settings` singleton — every
read site in this app (`build_provider`, `build_embedding_provider`,
`build_streaming_model_client`, `list_available_models`, the router prompt) already
reads `settings.X` fresh on every call, so mutating a field is enough for most of the
app. Three places cache a *built* provider instance rather than re-reading settings
each time, and need an explicit rebuild:

- `CopilotService.provider` / `.embedding_provider` — built once at `__init__`.
- `AutoGenOrchestrator.provider`, `SkillRunService.provider`, `RAGStore.embedding_provider`
  — each holds a reference to one of the two above.
- `app/agents.py`'s module-level router-provider cache (`_build_router_provider`).

`POST /api/settings/models/update` calls `CopilotService.reload_providers()` (only
when at least one field actually changed), which rebuilds `self.provider`/
`.embedding_provider` and reassigns them onto every object holding a reference —
**never** reconstructs `RAGStore` itself, since that would wipe every indexed
document; only its `embedding_provider` attribute is swapped in place — then calls
`app.agents.reset_router_provider_cache()`. The very next chat turn, retrieval, or
agent-mode routing decision uses the new configuration.

## What doesn't change

Switching `model_provider` to a provider with no configured credentials doesn't
error — it behaves exactly like an unconfigured `.env` value always has:
`build_provider()` falls back to `MockProvider` (wrapped in `FallbackProvider`, same
as today), `build_embedding_provider()` falls back further down its chain to the
offline hash embedder, and the agent router falls back to keyword matching. Nothing
about this feature relaxes any of the graceful-degrade contracts documented
elsewhere (`docs/tools.md`, `docs/agent-routing.md`, `docs/rag.md`).
