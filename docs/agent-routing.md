# Agent routing

Two routing decisions run per turn, in this order:

1. **Turn routing** — `plan_turn` (`app/agents.py`) decides *what this message
   should do*: generate a deck, generate a document, run the agent, or answer
   directly. See [Turn routing](#turn-routing) below.
2. **Agent routing** — only on turns that took the agent route,
   `AgentRegistry.select_llm` decides *which agent* handles it. That's the rest
   of this document.

---

# Turn routing

## The problem it fixes

Turn routing used to be a fixed `if/elif` chain over `SkillPackageStore.
select_for_chat`'s substring match, evaluated **before** an `agent_mode`
toggle was ever read, with two more toggles (Web search, Auto-generate)
gating capability further downstream. Because each branch was terminal, the
three composer toggles were mutually exclusive in effect even though the UI
presented them as independent — no single turn could honour more than one:

| Message | Old route | Toggles silently dropped |
| --- | --- | --- |
| "...presentation..." | Deck Builder | Agent mode, Web search |
| "...create a report..." | fixed Q&A form | all three |
| anything else, agent on | agent | Auto-generate |

So ticking all three and asking for a researched, auto-generated deck built one
with two of the three choices discarded — and asking *about* a presentation
("what did last quarter's presentation say about churn?") was hijacked into the
Deck Builder, because `presentation` is one of `skills/pptx/SKILL.md`'s
`chat_triggers`.

**Agent mode and Web search no longer exist as toggles at all.** Every turn
now gets full agent capability unconditionally — tools, RAG grounding, live
web research whenever Tavily is configured. Auto-generate remains the one
deliberate, explicit control (a safety/HITL gate, not a capability switch).

## How it works

`plan_turn(message, skills=..., keyword_match=..., provider=...)` makes one
small LLM call and returns a `TurnPlan`: `route`
(`"deck" | "skill" | "agent" | "direct"`), `skill_id`, `needs_web`,
`routed_by_llm`, `reason`. The plan is surfaced to the UI as a `routing`
progress event and on `ChatResponse.routing`, so the decision and its reason
are visible rather than implicit.

The router is told the **real installed generators** (`SkillPackageStore.list()`
— id, output extension, description), so an uploaded skill package becomes
routable the moment it's installed; nothing is hardcoded.

## What the router still decides

With no toggles left to consult, `"agent"` and `"direct"` are both always on
the menu — the router picks purely from the query:

- Generate a **file** (`"deck"`/`"skill"`) vs. **answer** (`"agent"`/`"direct"`).
- Within "answer," whether the message needs augmentation at all —
  **`"agent"`** for anything that might benefit from tools/RAG/web research
  (the safe default — `_deterministic_plan`'s fallback never picks anything
  else), **`"direct"`** only for what's unambiguously answerable from the
  model alone. `"direct"` exists specifically so
  `CopilotService.chat_stream` can real-stream tokens for a genuinely simple
  question instead of paying for the full orchestrator pipeline on every
  turn — a turn routed `"agent"` always goes through
  `AutoGenOrchestrator.run` (RAG grounding, tools) instead, even with no tool
  ultimately invoked.

Web search is no longer gated by anything routing decides either: it's
offered on both tool-using routes (agent turns and Deck Builder research)
whenever `TAVILY_API_KEY` is configured, full stop — the model itself still
decides per-call whether the offered tool is worth invoking.

`TurnPlan.needs_web` is **advisory only** — reported to the UI to explain
whether this turn's route benefits from live web results, never used to gate
anything. (There is deliberately no `needs_knowledge` counterpart: RAG
grounding is already autonomous and relevance-gated on every agent turn, so
the hint would change nothing and only costs router tokens.)

## Delivery: file vs. inline content

On a `"deck"`/`"skill"` route, the router also judges `TurnPlan.delivery` —
`"file"` (default) generates and hands back a real downloadable file exactly
as before this field existed; `"inline"` means the phrasing clearly asked for
the content shown in chat instead ("just tell me," "summarize it here," "no
need for a file"). Ambiguous or unstated phrasing defaults to `"file"`.

Unlike `needs_web`, `delivery` is a real gate, but not an unconditional one:
`app/skill_render.py`'s `render_spec_as_chat_text(skill_id, spec)` is what
actually turns a drafted spec into chat text, and it only covers
`docx-generator` and `pptx` today — `ppt-generator` (chat-unreachable by
design) and `brd-prd-generator` (a more complex, conditional-section shape,
deferred) return `None`. Every caller (`DeckBuilderService._run_and_handle`,
`SkillRunService.submit_answers`) treats `None` as "no renderer for this
skill yet" and falls back to generating the file regardless of what
`delivery` said — "inline" is a request the render step is allowed to grant,
not a guarantee it can.

There's no separate mechanism for "the user changed their mind" (e.g. "actually
just give me the file" after an inline reply) — it's just the next chat
message, which re-enters `plan_turn` and can pick `delivery="file"` on its
own, the same way Deck Builder's existing "enhance this" follow-up already
reuses `last_deck_spec`.

## Fallback (no router available)

`_deterministic_plan` — a trigger match routes to deck/skill, otherwise
unconditionally to `"agent"` (never `"direct"`: telling a genuinely simple
question apart from one that needs augmentation is exactly the judgment call
that needs a real router; without one, the safe default is the fully-capable
route). It runs whenever the router is unavailable: a bare `MockProvider`
(deterministic, no real model call — same contract `select_llm` has), no
`GEMINI_API_KEY`, a call that raises, or an answer that isn't usable. So the
"every flow works end-to-end with zero credentials" guarantee holds, and a
router outage degrades to full agent capability rather than to an error.

`plan_turn` also tolerates two answer shapes that strict parsing rejects, both
verified live against the real API: the object re-encoded as a string inside a
list (`["{\"route\": ...}"]`, which Gemini's JSON mode returns on some calls and
not others), and fenced/prose-wrapped JSON (via `_extract_json`). Before that,
routing degraded to trigger matching on roughly every other turn — which looks
exactly like the feature not working.

## Model choice: not `AGENT_ROUTER_MODEL`

`plan_turn` deliberately uses the configured **Gemini chat model**
(`GEMINI_MODEL`, default `gemini-flash-latest`) via
`_build_turn_router_provider`, *not* `settings.agent_router_model`.

Measured live against both. The `agent_router_model` default
(`gemma-4-26b-a4b-it`) answers `select_llm`'s one-line "which agent?" prompt
fine, but on this larger prompt it spent **10–15s** on hidden reasoning it
cannot be told to skip and routinely blew `GeminiProvider`'s own 15s HTTP
timeout — routing then degraded to trigger matching on about half of all turns.
The Gemini chat model answers the same prompt in **1–3s**, because
`thinkingConfig` *can* disable reasoning for real Gemini models. This call sits
in front of every single turn, so its latency is the product.

Also note this is a second model call per turn, on top of `select_llm`'s on
agent turns. Both degrade gracefully under rate limiting (a 429 falls back to
trigger matching), but on a constrained free-tier key that fallback will fire.

---

# Agent routing

Which agent handles a turn on the `"agent"` route is decided by `AgentRegistry.select_llm`
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
