# Enterprise Copilot

A minimal, runnable Enterprise Copilot: FastAPI + Pydantic backend, Bootstrap SPA frontend,
and an agent layer built on AutoGen behind a swappable orchestrator boundary.

Runs with **zero configuration** — no API key, no model download, no external services.
Every external integration (Gemini, Ollama, MCP, Tavily, Langfuse) is optional and degrades
gracefully to an offline path when unconfigured.

## Quick start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. On git bash / Linux / macOS, `./run.sh` does all of the above
(creates the venv, installs deps, seeds `.env` from `.env.example`, starts the server).

```bash
pytest tests/          # 372 tests, no network required
```

## Configuration

Copy `.env.example` to `.env`. Everything is optional — the defaults run fully offline.

| Variable | Default | Effect |
|---|---|---|
| `AI_MODE` | `mock` | `mock` = deterministic offline responses. `configured` = use a real model. |
| `MODEL_PROVIDER` | `gemini` | `gemini`, `ollama`, or `azure`, used when `AI_MODE=configured`. |
| `GEMINI_API_KEY` | — | Enables Gemini chat/embedding, and LLM-based agent routing (always used for routing regardless of `MODEL_PROVIDER`). |
| `OLLAMA_BASE_URL` | `localhost:11434` | Self-hosted models; no API key needed. |
| `AZURE_AI_ENDPOINT` / `AZURE_AI_API_KEY` / `AZURE_AI_DEPLOYMENT` | — | Azure AI Foundry (classic Azure OpenAI resource shape) — see `.env.example` for the full set, including `AZURE_AI_DEPLOYMENTS` (extra reasoning deployments selectable from the model picker) and `AZURE_AI_EMBEDDING_DEPLOYMENT`. |
| `TAVILY_API_KEY` | — | Enables the web-search tool. |
| `QDRANT_URL` / `QDRANT_API_KEY` | — | Enables Qdrant Cloud as the vector store; falls back to an in-memory cosine scan when unset. |
| `LANGFUSE_*` | — | Enables tracing (one trace per chat turn). |

Missing credentials never break a request — providers fall back down a chain, ending at an
always-available offline implementation. See `docs/runtime-settings.md`.

## Features

**Chat & orchestration**
- Streaming responses (SSE) with mid-turn cancellation, plus a non-streaming endpoint
- Agent routing: LLM-based router (`AgentRegistry.select_llm`) with keyword matching as fallback
- Agent/skill registries — `research-agent`, `coding-agent`, `data-analysis-agent`, `general`
- **Deck Builder** — a conversational, Magentic-One-driven flow for slide decks: plans, asks
  clarifying questions, calls web search for grounding, and drafts a richer spec (native charts,
  varied layouts) than the fixed-question skill flow. See `app/agents.py:DeckBuilderOrchestrator`.
- Conversation memory that buffers recent turns verbatim and compact-summarizes older ones,
  so long sessions degrade gracefully instead of dropping context (`app/memory.py`)
- **Checkpointer** — automatic step-boundary progress markers plus user-named, restorable session
  checkpoints, so a session survives a process restart or an interrupted turn (`app/storage.py`)
- Multimodal input — images attached to a chat turn (`docs/multimodal.md`)

**Knowledge / RAG** (`docs/rag.md`)
- Full document lifecycle in the UI: add, list, view/edit, update, delete, with immediate re-index
- Ingestion: extract → clean → chunk → metadata → embed → index
  (paragraph-aware chunking, 600 chars, 80-char overlap)
- Retrieval: hybrid dense + BM25, fused by reciprocal rank fusion, then reranked
- PDF text extraction; encrypted and image-only PDFs are rejected explicitly rather than
  silently indexing nothing
- Answers cite their sources as `[filename#chunk_index]`

**Safety**
- Three-stage guardrails, visible in the UI: `check_input` (user message),
  `check_context` (retrieved chunks — indirect prompt injection), `check_output` (model reply)
- PII detection & redaction (email, phone, SSN, credit card, IP) and sensitive-data
  filtering (API keys, passwords, tokens) — redacted in place, turn still proceeds
- Toxic/unsafe-content policy — blocks the turn, same as prompt injection
- Per-message "Guardrail activity" panel showing exactly what was caught this turn
  (category + count only, never the matched value) — see `docs/guardrails.md`
- HITL approval queue — risky actions require explicit approval before running. Two kinds today:
  `code_execution` (the coding agent's own generated code) and `deck_generation` (Deck Builder,
  when its "Auto-generate" toggle is off — the default). Requests persist to disk
  (`data/hitl-requests/`) so a pending approval survives a restart.
- Local Python execution with timeout and output limits (**development-only, not a secure sandbox**)

**Tools & extensibility**
- MCP tool-calling: a bundled local stdio server plus an optional remote server, both no-op
  when unavailable (`docs/tools.md`)
- Skill packages — upload, run interactively, and download generated artifacts. Bundled:
  `docx-generator`, `ppt-generator`, `brd-prd-generator`, `pptx` (the richer generator behind
  Deck Builder — native charts, 10 palettes, 6 layouts)
- Optional Langfuse observability (`docs/observability.md`)

## Architecture

```
Frontend (static/)  →  FastAPI routes (app/routes.py)  →  CopilotService (app/services.py)
                                                                    │
                                                          AgentOrchestrator  ← the contract
                                                                    │
                                                    AutoGenOrchestrator + DeckBuilderOrchestrator
                                                          ← every AutoGen import lives here
```

Two boundaries carry the design:

1. **Orchestrator** — application code depends on `AgentOrchestrator`, never on AutoGen types.
   A future MAF migration replaces the implementation without touching routes, services, or UI.
   `autogen_agentchat` is imported in exactly three files in the whole repo: `app/agents.py`,
   `app/hitl_agents.py`, `app/streaming.py`.
2. **Providers** — `AIProvider` and `EmbeddingProvider` are chains ending in an always-available
   offline implementation, so a missing key or provider outage degrades quality, not availability.

All application APIs are **POST** with Pydantic-validated bodies; no query parameters carry
application behavior.

A full interactive deep-dive — every subsystem, click-to-expand, with real code excerpts and a
request-flow diagram — lives at [`docs/architecture-explorer.html`](docs/architecture-explorer.html)
(open it directly in a browser).

### Agent architecture

An in-progress exchange always continues first — a half-finished skill Q&A or deck
clarification is a conversation already underway, not a new request. Once nothing is pending,
`plan_turn()` (`app/agents.py`) decides what the message should actually do, from the message
itself rather than from whichever substring happens to appear in it:

```
check_input()  →  CopilotService.chat()  →  ┬─ pending_skill_run?     → SkillRunService
 (guardrails)                                ├─ pending_deck_builder?  → DeckBuilderOrchestrator
                                             └─ plan_turn()  ─┬─ "deck"   → DeckBuilderOrchestrator
                                              (LLM router,    ├─ "skill"  → SkillRunService
                                               trigger-match  ├─ "agent"  → AutoGenOrchestrator
                                               fallback)      └─ "direct" → AIProvider.complete()
                                                                              │
                                                                     check_output()  →  response
```

The three composer toggles are **permissions**, not modes — they bound what `plan_turn` may
choose and what the chosen route may do, and they compose on a single turn:

| Toggle | Means |
| --- | --- |
| **Agent mode** | Non-generation turns go through `AutoGenOrchestrator` (RAG grounding, tools) instead of a direct answer. |
| **Web Search** | This turn may call the live web — on agent answers *and* deck research alike. |
| **Auto-generate** | A finished deck spec may generate without a human approving it first. |

See [`docs/agent-routing.md`](docs/agent-routing.md) for the router prompt, its fallback and
its model choice.

- **`AutoGenOrchestrator`** (`app/agents.py`) — agent-mode requests. LLM-based agent selection
  (`AgentRegistry.select_llm`, keyword-match fallback), RAG grounding with guardrail screening,
  real MCP tool-calling via a live `AssistantAgent.on_messages_stream`, and the coding agent's
  HITL gate (a fenced code block in the model's own answer is extracted and queued for approval).
- **`DeckBuilderOrchestrator`** (`app/agents.py`) — a single-participant `MagenticOneGroupChat`
  that plans, asks clarifying questions, and calls `web_search` before drafting a deck spec as
  its final answer (JSON only, no tool-based generation — the model drafts, `generate_pptx.py`
  deterministically executes). Reused pattern: every generator in this app splits "model drafts
  JSON" from "app code runs a fixed script," never letting the model run code itself.
- Registered agents (`AgentRegistry`) are keyword/LLM-routed specializations within agent mode —
  `coding-agent`, `research-agent`, `data-analysis-agent`, `general` — distinct from the two
  orchestrator *classes* above.

See `docs/agent-architecture.md`, `docs/agent-routing.md`, and §4–§6 of the architecture explorer.

### RAG architecture

```
Ingestion:  extract → clean → chunk (structure-aware, parent-child) → embed → index (Qdrant + BM25)
Retrieval:  understand → hybrid retrieve (vector + BM25) → RRF fusion → rerank → dedupe → compress
```

- **Vector store**: Qdrant Cloud when `QDRANT_URL`/`QDRANT_API_KEY` are set and
  `AI_MODE=configured`; an in-memory exhaustive cosine scan otherwise (`app/vector_store.py`).
  Every point carries a `tenant_id` payload field, indexed and filtered on every query — the
  multi-tenancy seam exists at the vector-store layer even though v1 has one tenant.
- **Hybrid retrieval**: a dense vector leg and a `BM25Index` lexical leg, fused by reciprocal
  rank fusion (rank position, not raw score magnitude, since the two legs are scaled
  differently), then reranked by lexical coverage over the fused candidate pool.
- **Relevance gate**: auto-grounding a turn requires genuine BM25 lexical overlap *and* a
  reranker score above threshold — vector similarity alone is never trusted to decide relevance,
  because the default embedder (a hashing-trick bag-of-words, always available offline) is
  admittedly noisy on its own.
- **Guardrails on retrieved context**: indirect prompt injection is screened out of chunks
  before they reach the prompt (`check_context`); surviving chunks get PII redacted in place
  (`redact_context_pii`) — retrieval-time only, the source documents themselves stay unredacted.

See `docs/rag.md` and §3 of the architecture explorer.

### Layout

```
app/          backend — routes, services, agents, providers, retrieval, memory, sandbox
static/       Bootstrap SPA (no build step)
skills/       bundled skill packages
mcp_servers/  local MCP tool servers
docs/         design and implementation documentation
tests/        pytest suite
.claude/      Claude Code agents, rules, commands, skills
```

## Documentation

Start with `docs/agent-overview.md` for the narrative walkthrough of how a message becomes an
answer. Then, by topic:

| Area | Doc |
|---|---|
| Agent layer, orchestrator boundary | `docs/agent-architecture.md`, `docs/agent-routing.md` |
| RAG pipeline | `docs/rag.md` |
| Guardrails, HITL, security | `docs/guardrails.md`, `docs/hitl.md`, `docs/security.md` |
| Memory, multimodal, tools | `docs/memory.md`, `docs/multimodal.md`, `docs/tools.md` |
| API reference | `docs/api.md` |
| Skills | `docs/skill-architecture.md` |
| Setup, testing, config | `docs/development.md`, `docs/testing.md`, `docs/runtime-settings.md` |
| Interactive deep-dive, every subsystem | `docs/architecture-explorer.html` |

Working with Claude Code on this repo: read `CLAUDE.md` first; `.claude/commands/` holds the
project's slash commands and `.claude/rules/` the standards enforced during review.

## Future enhancements (v3)

Planned infra work, each replacing a component that's explicitly a v1 placeholder today.
None of these are implemented yet:

- **Postgres (Neon)** — replaces the five separate file-backed JSON stores (sessions, documents,
  HITL requests, artifacts, skill runs) with one normalized, multi-tenant schema. `org_id` on
  every owned table + Row-Level Security from day one, even before real auth exists, so turning
  on multi-tenancy later is additive rather than a retrofit across every table.
- **Redis (Upstash)** — cross-process state for the two things that are currently plain
  in-process Python objects and can't survive a second worker: the live HITL decision wait
  (`HitlService._decision_futures`) and the in-flight-stream registry (`_active_streams` in
  `app/routes.py`).
- **Filebase (S3-compatible storage)** — moves document text, artifact bytes, and generated
  skill output off local disk and into object storage, behind the existing `ArtifactStore`/
  `DocumentStore` interfaces.
- **E2B sandbox** — replaces `LocalSubprocessSandbox` (explicitly development-only today) behind
  the existing `CodeSandbox` interface, for a real isolated execution environment in production.

## v1 scope and limits

Deliberate, documented v1 choices — each sits behind the interface its replacement would use:

- **Vector store defaults to an in-memory dict** with an exhaustive cosine scan when Qdrant
  isn't configured — exact and fast at this scale, and nothing persists across a restart in
  that mode. Set `QDRANT_URL`/`QDRANT_API_KEY` with `AI_MODE=configured` for a real, persistent
  vector store (`app/vector_store.py:QdrantVectorStore`).
- **Default embedder is a hashing-trick bag-of-words**, not a semantic model — it runs offline
  with no download. It will not match "car" to "automobile". Set `AI_MODE=configured` to use
  a real embedding model (`nomic-embed-text` via Ollama, or Gemini).
- **Local code execution is a development convenience, not a security boundary.** Do not expose
  it to untrusted input — see the E2B item above for the planned replacement.
- **Single-process only** — sessions, documents, and HITL requests are file-backed and survive a
  restart, but a few things are still plain in-process state that won't survive multiple workers:
  generated artifacts (`ArtifactStore`), the live HITL-decision wait, and the in-flight SSE
  stream registry — see the Postgres/Redis items above.
