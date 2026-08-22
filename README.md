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
pytest tests/          # 242 tests, no network required
```

## Configuration

Copy `.env.example` to `.env`. Everything is optional — the defaults run fully offline.

| Variable | Default | Effect |
|---|---|---|
| `AI_MODE` | `mock` | `mock` = deterministic offline responses. `configured` = use a real model. |
| `MODEL_PROVIDER` | `gemini` | `gemini` or `ollama`, used when `AI_MODE=configured`. |
| `GEMINI_API_KEY` | — | Enables Gemini chat/embedding, and LLM-based agent routing. |
| `OLLAMA_BASE_URL` | `localhost:11434` | Self-hosted models; no API key needed. |
| `TAVILY_API_KEY` | — | Enables the web-search tool. |
| `LANGFUSE_*` | — | Enables tracing (one trace per chat turn). |

Missing credentials never break a request — providers fall back down a chain, ending at an
always-available offline implementation. See `docs/runtime-settings.md`.

## Features

**Chat & orchestration**
- Streaming responses (SSE) with mid-turn cancellation, plus a non-streaming endpoint
- Agent routing: LLM-based router (`AgentRegistry.select_llm`) with keyword matching as fallback
- Agent/skill registries — `research-agent`, `coding-agent`, `data-analysis-agent`, `general`
- Conversation memory that buffers recent turns verbatim and compact-summarizes older ones,
  so long sessions degrade gracefully instead of dropping context (`app/memory.py`)
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
- HITL approval queue — risky actions require explicit approval before running
- Local Python execution with timeout and output limits (**development-only, not a secure sandbox**)

**Tools & extensibility**
- MCP tool-calling: a bundled local stdio server plus an optional remote server, both no-op
  when unavailable (`docs/tools.md`)
- Skill packages — upload, run interactively, and download generated artifacts. Bundled:
  `docx-generator`, `ppt-generator`, `brd-prd-generator`
- Optional Langfuse observability (`docs/observability.md`)

## Architecture

```
Frontend (static/)  →  FastAPI routes (app/routes.py)  →  CopilotService (app/services.py)
                                                                    │
                                                          AgentOrchestrator  ← the contract
                                                                    │
                                                          AutoGenOrchestrator  ← the only
                                                                                 AutoGen code
```

Two boundaries carry the design:

1. **Orchestrator** — application code depends on `AgentOrchestrator`, never on AutoGen types.
   A future MAF migration replaces the implementation without touching routes, services, or UI.
2. **Providers** — `AIProvider` and `EmbeddingProvider` are chains ending in an always-available
   offline implementation, so a missing key or provider outage degrades quality, not availability.

All application APIs are **POST** with Pydantic-validated bodies; no query parameters carry
application behavior.

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

Working with Claude Code on this repo: read `CLAUDE.md` first; `.claude/commands/` holds the
project's slash commands and `.claude/rules/` the standards enforced during review.

## v1 scope and limits

Deliberate, documented v1 choices — each sits behind the interface its replacement would use:

- **Vector store is an in-memory dict** with an exhaustive cosine scan — exact and fast at this
  scale, swappable for Chroma/pgvector behind `RAGStore`'s six methods. Nothing persists across
  a restart.
- **Default embedder is a hashing-trick bag-of-words**, not a semantic model — it runs offline
  with no download. It will not match "car" to "automobile". Set `AI_MODE=configured` to use
  a real embedding model (`nomic-embed-text` via Ollama, or Gemini).
- **Local code execution is a development convenience, not a security boundary.** Do not expose
  it to untrusted input.
- **Single-process only** — in-memory state does not survive multiple workers.
