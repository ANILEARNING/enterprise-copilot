# Runtime settings (Settings page → Models panel)

Lets you choose, from the running app, which provider and model each of the four
model-consuming subsystems uses — no `.env` edit or restart required. In-memory only,
per this project's v1 architecture (`.claude/rules/architecture.md`): a process restart
reverts every override back to whatever `.env` says.

## What's editable

| Slot | Setting | Consumed by |
|---|---|---|
| Provider | `model_provider` (`gemini`/`ollama`/`azure`) | `build_provider()` — the default chat-completion path |
| Chat model | `gemini_model` / `ollama_model` | Same, plus `build_streaming_model_client` (direct-chat streaming, agent-mode tool-calling) |
| RAG embedding | `gemini_embedding_model` / `ollama_embedding_model` | `build_embedding_provider()` — ingestion and query embedding |
| Agent router | `agent_router_model` | `AgentRegistry.select_llm` — always Gemini regardless of `model_provider` (see `docs/agent-routing.md`) |

**Azure AI Foundry's Models-panel slot is read-only, but its DEPLOYMENT is
still pickable elsewhere**: `AZURE_AI_DEPLOYMENT` (the default) is fixed by
`.env`, shown read-only rather than as a dropdown here, since this Settings
page's `SettingsModelsUpdate` has no Azure field to write to (unlike
`gemini_model`/`ollama_model`, which ARE live-editable here). A resource with
more than one reasoning deployment (`AZURE_AI_DEPLOYMENTS`, comma-separated,
see `.env.example`) still gets every deployment offered to the **Copilot
chat's own model picker** (`POST /api/chat` with `model: "azure/<deployment>"`
— the same `"provider/model"` mechanism Gemini/Ollama already use, see
`CopilotService._parse_model_choice`) — that picker is a per-turn choice, not
a Settings-page default, which is why it works without a Models-panel
dropdown. Enumerating deployments at all is only possible from `.env`
settings in the first place: only Azure's management/ARM API (a different
auth model than the resource's own `api-key`, which is all this app holds)
could discover them live. See `app/providers.py:AzureAIFoundryProvider` and
`_azure_deployment_names`.

**Never editable from this UI**: API keys (`GEMINI_API_KEY`, `OLLAMA_API_KEY`,
`AZURE_AI_API_KEY`), `OLLAMA_BASE_URL`, `AZURE_AI_ENDPOINT`, `AZURE_AI_DEPLOYMENT`,
`AZURE_AI_DEPLOYMENTS`, `AZURE_AI_EMBEDDING_DEPLOYMENT`. Those stay `.env`-only
by design — this surface picks among already-configured credentials, it
doesn't manage secrets.

## API

- `POST /api/settings/models` — current effective values, read-only. Includes
  `gemini_configured`/`ollama_configured`/`azure_configured` (whether each provider
  even has usable credentials right now) so the UI can grey out a choice that would
  fail, plus `azure_deployment` (the default, read-only), `azure_deployments` (every
  deployment this resource has — default plus `AZURE_AI_DEPLOYMENTS` extras, same list
  `POST /api/models/list` offers the chat picker), and `azure_embedding_deployment`
  (also read-only — see "Never editable" above).
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
as today), `build_embedding_provider()` falls back further down its chain (Azure ->
Gemini as a cross-provider safety net, same shape Ollama already has -> the offline
hash embedder), and the agent router falls back to keyword matching. Nothing about
this feature relaxes any of the graceful-degrade contracts documented elsewhere
(`docs/tools.md`, `docs/agent-routing.md`, `docs/rag.md`).

**Switching embedding providers can break existing retrieval, though — this is
a real gap, not something this feature smooths over.** A Qdrant collection is
sized to whatever embedding dimension its first point had (see `docs/rag.md`);
switching `model_provider` (Gemini/Ollama/Azure all use different-dimension
embedders) changes which embedder every *later* query and re-index uses, but
does nothing to a collection that already exists at the OLD dimension —
Qdrant itself then rejects every query with a dimension-mismatch error until
the collection is dropped and every document re-indexed against the new
embedder (this happened live switching to Azure this session — see the fix
in `docs/rag.md`'s caveat). There is no automatic re-index-on-switch here.
