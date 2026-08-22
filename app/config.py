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
    code_execution_mode: str = "local"
    max_code_execution_seconds: int = 20
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
    # the model when both this key is set AND the turn's "Web Search" UI
    # toggle is on (app/models.py ChatRequest.web_search) — never on by
    # default, and never logged (see get_mcp_tools(web_search=...)).
    tavily_api_key: str = ""

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

    # Ignore unrecognized env vars instead of failing startup on them.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
