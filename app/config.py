import sys

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    ai_mode: str = "mock"
    model_provider: str = "gemini"
    gemini_api_key: str = ""
    # Default chat-completion model when MODEL_PROVIDER=gemini — empty means
    # "use GeminiProvider.DEFAULT_MODEL" (gemini-flash-latest). Settable live
    # from the Settings page (POST /api/settings/models) as well as .env; see
    # app/runtime_settings.py for how a UI change takes effect immediately.
    gemini_model: str = ""
    gemini_embedding_model: str = "gemini-embedding-001"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = ""
    ollama_embedding_model: str = "nomic-embed-text"
    ollama_api_key: str = ""
    # Azure AI Foundry (classic Azure OpenAI resource shape — see
    # app/providers.py:AzureAIFoundryProvider). `azure_ai_endpoint` is the
    # resource's base URL (e.g. "https://<resource>.openai.azure.com"), never
    # including "/openai/deployments/...". `azure_ai_deployment` is the
    # DEFAULT reasoning/chat deployment NAME (Azure's "which model" is a
    # deployment you create in the resource, not a public model id like
    # gemini-flash-latest) — used everywhere MODEL_PROVIDER=azure is the
    # active provider (chat, skill drafting, turn routing), and also what
    # AutoGen's client needs for the streaming/tool-calling path (see
    # app/streaming.py). `azure_ai_deployments` is every reasoning deployment
    # this resource has, comma-separated, INCLUDING azure_ai_deployment — the
    # full set list_available_models() offers the Copilot model picker (see
    # docs/runtime-settings.md), since a resource can have more than one
    # deployment but only ever one *default*. Empty means "just the default
    # deployment, no extra picker choices" (the common single-deployment
    # case). `azure_ai_embedding_deployment` is the (single) embedding
    # deployment name — mirrors gemini_embedding_model/ollama_embedding_model,
    # no per-call picker (see AzureEmbeddingProvider). `azure_ai_api_version`
    # follows Azure's own dated versioning; the default here is a stable GA
    # version as of this writing — override in .env if your resource
    # requires a different one.
    azure_ai_endpoint: str = ""
    azure_ai_api_key: str = ""
    azure_ai_deployment: str = ""
    azure_ai_deployments: str = ""
    azure_ai_embedding_deployment: str = ""
    azure_ai_api_version: str = "2024-10-21"
    # "local" (default): LocalSubprocessSandbox — dev-only, no real isolation
    # (see app/sandbox.py's module docstring). "e2b": every code execution
    # routes to E2BSandbox instead — a real, network-isolated VM sandbox via
    # the E2B API (https://e2b.dev), up to an hour long / 20 concurrent
    # sandboxes vs. local's 20s/single-process posture. Reserved for a
    # future "online_compiler" mode once that provider's API is reachable
    # (see app/sandbox.py's module docstring on the two-provider design this
    # is meant to grow into) — "disabled" turns code execution off entirely,
    # matching LocalSubprocessSandbox.run's existing DISABLED status.
    code_execution_mode: str = "local"
    max_code_execution_seconds: int = 20
    # E2B (https://e2b.dev) API key — required only when code_execution_mode
    # == "e2b". Env var name intentionally does NOT match e2b's own SDK
    # convention (E2B_API_KEY) — this app reads it under E2B_SANDBOX (see
    # .env) and passes it explicitly to AsyncSandbox.create(api_key=...)
    # rather than relying on the SDK's own env var auto-detection, so
    # there's exactly one place (this field) that governs whether E2B is
    # configured, consistent with every other provider key in this file.
    e2b_sandbox: str = Field(default="", validation_alias=AliasChoices("E2B_SANDBOX", "E2B_API_KEY"))
    max_upload_mb: int = 20
    # Root directory for every file-backed store CopilotService owns
    # (sessions/, skills/, skill-runs/ — see CopilotService.__init__,
    # app/services.py). Empty (the default) means "use each store's own
    # real repo-root data/ default" — production and normal local runs never
    # need to set this. Exists so tests/conftest.py can redirect the whole
    # tree to a throwaway temp directory instead of every test process
    # writing real session/skill/run files into this repo's own data/ dir.
    data_dir: str = ""
    # Caps every live provider call's generated length. Keeps configured-mode
    # requests (including test runs against real credentials) fast and cheap.
    # Reasoning models (e.g. Ollama's gpt-oss) can burn this entire budget on
    # hidden "thinking" before emitting visible text, so tasks that need a
    # substantial answer get a bigger override below rather than raising this
    # default for every call.
    max_output_tokens: int = 256
    # Skill spec-drafting (docx/pptx — see app/skills.py:draft_spec): the
    # prompt asks for a full multi-section JSON document, which the 256-token
    # default can't fit — was silently tripping the reasoning-model fallback
    # above and always landing on the generic template instead.
    max_output_tokens_skill_draft: int = 2048
    # Coding-agent tasks (AutoGenOrchestrator, "coding" in skills_used):
    # nontrivial code routinely exceeds the default budget.
    max_output_tokens_code: int = 6144

    # --- Agent routing (AgentRegistry.select_llm, app/agents.py) ---
    #
    # LLM-based agent selection replaces the old pure-keyword AgentRegistry.select()
    # as the primary router — a small, fast, cheap-to-call classifier model picks
    # which agent handles a turn instead of a fixed substring list. Always backed
    # by a raw GeminiProvider (build_provider_for("gemini", agent_router_model)),
    # independent of MODEL_PROVIDER/the main chat model — routing needs to stay
    # cheap and fast even when the main chat provider is Ollama or something
    # slower/costlier. Requires GEMINI_API_KEY regardless of MODEL_PROVIDER.
    #
    # Default is Gemma 4 26B-A4B via the Gemini API (generativelanguage.
    # googleapis.com/v1beta/models/{model}:generateContent — same endpoint
    # GeminiProvider already calls, just a different model id) — Google's
    # smallest/fastest Gemma 4 hosted variant, positioned for low-latency
    # reasoning/coding classification tasks exactly like this one. Real-world
    # free-tier rate limits for Gemma models are not published by Google as a
    # fixed number (check https://aistudio.google.com/rate-limit for your own
    # key's live limits) — if this router model gets rate-limited in practice,
    # switch to a documented-limit alternative:
    #   AGENT_ROUTER_MODEL=gemma-4-31b-it        # larger Gemma 4, stronger reasoning
    #   AGENT_ROUTER_MODEL=gemini-3.1-flash-lite # Gemini's own lightweight model,
    #                                             # published free-tier limits
    # A router call failure (no GEMINI_API_KEY, rate limit, malformed response,
    # mock mode) always falls back to AgentRegistry.select()'s keyword matching
    # for that turn — routing never breaks chat, same graceful-degrade posture
    # as every other provider/tool in this app.
    agent_router_model: str = "gemma-4-26b-a4b-it"

    # --- MCP (Model Context Protocol) tool servers — optional, see app/mcp_tools.py ---
    #
    # Agent-mode turns (AutoGenOrchestrator.run, app/agents.py) get these tools
    # available for real tool-calling, alongside its built-in knowledge-base/
    # code-queue steps. Both servers are independent; either/both unset just
    # means no extra tools for that server, never a startup/request error —
    # same graceful-degrade posture as every AIProvider above.
    #
    # Local stdio server: defaults to this repo's own bundled general-purpose
    # tool server (mcp_servers/general_tools_server.py — current time, basic
    # arithmetic, text stats, UUIDs; no filesystem/network access of its own),
    # launched with the exact interpreter running this app (sys.executable),
    # not a bare "python" that might resolve to a different install on PATH.
    mcp_stdio_enabled: bool = True
    mcp_stdio_command: str = sys.executable
    mcp_stdio_args: str = "mcp_servers/general_tools_server.py"
    # Remote MCP server (Streamable HTTP or SSE transport) — disabled unless a
    # URL is configured. mcp_remote_headers is one "Key: Value" pair per line
    # (e.g. an Authorization bearer token) — never logged.
    mcp_remote_url: str = ""
    mcp_remote_transport: str = "streamable-http"  # or "sse"
    mcp_remote_headers: str = ""

    # Web search tool (Tavily, https://tavily.com) — see mcp_servers/tavily_search_server.py.
    # A third, independent MCP server from the two above: only ever offered to
    # the model when this key is set — no per-request toggle any more (see
    # get_mcp_tools's call sites in app/agents.py) — and never logged.
    tavily_api_key: str = ""

    # --- RAG v2 vector store (Qdrant Cloud) — optional, see app/vector_store.py ---
    #
    # Unset (either empty) means RAGStore uses InMemoryVectorStore — today's exhaustive
    # in-memory cosine scan, extracted verbatim behind the same VectorStore interface —
    # so mock-mode/offline dev and the test suite need zero Qdrant credentials. Both
    # must be set for QdrantVectorStore to be selected; the collection is created
    # on first use if it doesn't exist yet (idempotent, no manual provisioning step).
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection: str = "enterprise_copilot_chunks"
    # Single fixed tenant carried through every chunk's payload/metadata from day
    # one (RAGStore, DocumentStore) — not real multi-tenant isolation yet, just the
    # schema shape a second tenant later slots into without a migration/backfill.
    rag_tenant_id: str = "default"
    # Character-based proxy for a token budget (consistent with this app's existing
    # prompt[:2000]-style char-budgeting elsewhere — no tokenizer dependency added).
    # Caps how much retrieved context compress_context() packs into one prompt.
    rag_context_token_budget: int = 4000

    # --- Observability — optional, see app/observability.py ---
    #
    # No-op tracer unless both keys are set; never required for the app to run.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    # LANGFUSE_BASE_URL is the SDK's current name (LANGFUSE_HOST is its own
    # deprecated alias — accepted here too for whichever one ends up in .env).
    langfuse_base_url: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("LANGFUSE_BASE_URL", "LANGFUSE_HOST"),
    )

    # --- Database (PostgreSQL — Azure PG Flexible Server primary for the
    # initial 1-week Azure sandbox window, Neon fallback afterward; same
    # Postgres dialect, swappable via this one URL, no code change either
    # way) — see docs/database.md ---
    #
    # Required for the app to start (Postgres is now the only persistence
    # layer for tenant-scoped data — no file-backed fallback). A standard
    # postgresql+asyncpg:// URL works unmodified against both Azure
    # PostgreSQL Flexible Server and Neon.
    database_url: str = ""

    # --- Auth (email/password login; Google/SSO schema reserved for later —
    # see app/auth.py, app/tenancy.py) ---
    #
    # Required for the app to start once auth ships — signs/verifies every
    # JWT access token. No default: a blank or guessable secret would let
    # anyone forge a token for any user/tenant, so this fails startup loudly
    # instead of silently running insecure, same posture as database_url.
    jwt_secret_key: str = ""
    # Access-token lifetime in minutes — short, now that a refresh-token flow
    # exists (app/db/models.py:RefreshTokenRow, app/db/tenancy_repository.py)
    # to renew it: a stolen access token is only ever useful for this long,
    # unlike the old week-long default written before refresh tokens existed.
    jwt_expire_minutes: int = 15
    # Refresh-token lifetime in days — how long a login session lasts before
    # the user must sign in again from scratch (as opposed to a silent
    # access-token renewal via POST /api/auth/refresh). A week is a
    # reasonable default for a small SaaS; RefreshTokenRepository.revoke/
    # revoke_all_for_user end a session earlier (logout, password change).
    refresh_token_expire_days: int = 7

    # --- Session store (Upstash Redis, over its HTTPS REST API — see
    # app/session_store.py) ---
    #
    # Required for chat to work once app/session_store.py is wired into
    # CopilotService — replaces the old file-backed SessionStore entirely
    # (see app/db/models.py's "Sessions / messages — Redis, not Postgres"
    # note). Both required together: the REST API needs a URL AND a bearer
    # token, unlike a plain redis:// connection string that carries both in
    # one value. No default, same "fail loudly, not silently
    # insecure/broken" posture as database_url/jwt_secret_key above.
    upstash_redis_rest_url: str = ""
    upstash_redis_rest_token: str = ""

    # --- Blob storage (Backblaze B2, S3-compatible — see app/blob_store.py) ---
    #
    # Holds document raw content and skill-run output files; Postgres keeps
    # only metadata + the b2_key/object-key pointer (see
    # app/db/models.py:DocumentRow.b2_key). All four required together once
    # blob_store.py is wired in — boto3's S3 client needs endpoint + both
    # credential halves + a bucket to target.
    b2_endpoint: str = ""
    b2_bucket_name: str = ""
    b2_key_id: str = ""
    b2_application_key: str = ""

    # Ignore unrecognized env vars instead of failing startup on them.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
