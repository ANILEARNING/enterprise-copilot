"""Agent/skill/tool layer and the AgentOrchestrator boundary.

Application code depends only on `AgentOrchestrator`. AutoGen-specific
imports and calls live in `AutoGenOrchestrator` and nowhere else, so a
future MAF implementation can replace this class without touching routes,
services, or the UI (see .claude/rules/autogen-maf.md).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import Response, TaskResult
from autogen_agentchat.messages import TextMessage, ToolCallExecutionEvent, ToolCallRequestEvent
from autogen_agentchat.teams import MagenticOneGroupChat
from autogen_core import CancellationToken

from .config import settings
from .hitl_agents import await_human_decision
from .mcp_tools import get_mcp_tools
from .memory import (
    BUFFER_SIZE, CompactingChatCompletionContext, CompactMemoryState, compact_history,
    history_to_llm_messages, llm_messages_to_plain, render_memory_preview,
)
from .providers import AIProvider, GeminiProvider, MockProvider, describe_embedding_config
from .skills import _extract_json
from .streaming import build_streaming_model_client, build_user_message

logger = logging.getLogger(__name__)

# Progress callback threaded through AgentOrchestrator.run() for live-streaming
# UIs (see CopilotService.chat_stream) — each event is a small dict describing
# one real step the orchestrator just took (see AutoGenOrchestrator.run() for
# the concrete stages). None (the default) means "no one's listening" — every
# call site is a plain no-op await, so this costs nothing for the existing
# non-streaming /api/chat path.
EventSink = Callable[[dict], Awaitable[None]]


async def _emit(on_event: EventSink | None, event: dict) -> None:
    if on_event is not None:
        await on_event(event)


# --- LLM agent routing (AgentRegistry.select_llm) ----------------------------
#
# The router always calls Gemini directly (GeminiProvider, settings.
# agent_router_model — default a Gemma 4 model served via the Gemini API,
# see app/config.py's comment for the exact endpoint/rationale), independent
# of MODEL_PROVIDER/the main chat provider: routing needs to stay cheap and
# fast even when the configured chat provider is Ollama or something slower.
# Built once per process and cached — same rationale as get_mcp_tools()
# (app/mcp_tools.py): constructing a provider is cheap, but re-deciding
# "is GEMINI_API_KEY set" on every single turn is pointless work. None means
# "not attempted yet" until _router_provider_built flips True — after that,
# a None value correctly means "attempted, not configured, don't retry"
# without re-checking settings.gemini_api_key every call.
_router_provider_built = False
_router_provider: AIProvider | None = None


def _build_router_provider() -> AIProvider | None:
    global _router_provider_built, _router_provider
    if _router_provider_built:
        return _router_provider
    _router_provider_built = True
    if not settings.gemini_api_key:
        return None
    # No FallbackProvider wrapping — a router failure is handled explicitly
    # by AgentRegistry.select_llm's try/except, which degrades to keyword
    # matching for that turn rather than silently landing on mock (mock
    # routing would always pick "general", defeating the point of routing).
    _router_provider = GeminiProvider(
        settings.gemini_api_key, max_output_tokens=64, model=settings.agent_router_model,
    )
    return _router_provider


def reset_router_provider_cache() -> None:
    """Clears the cached router provider so the next call to
    _build_router_provider() rebuilds it from current settings — called by
    app/routes.py's settings-update route after agent_router_model or
    gemini_api_key changes at runtime (see docs/runtime-settings.md), so a
    Settings-page change takes effect on the very next agent-mode turn
    instead of only after a process restart."""
    global _router_provider_built, _router_provider, _turn_router_built, _turn_router
    _router_provider_built = False
    _router_provider = None
    _turn_router_built = False
    _turn_router = None


# --- autonomous turn routing (plan_turn) -------------------------------------
#
# WHAT THIS REPLACES: CopilotService.chat() used to route a turn with a fixed
# if/elif chain over SkillPackageStore.select_for_chat's substring match,
# evaluated BEFORE the agent_mode toggle was ever consulted. That made the
# three composer toggles (Agent mode / Web search / Auto-generate) mutually
# exclusive in effect even though the UI presents them as independent, and no
# single turn could ever honour more than one of them:
#
#   "...presentation..."     -> Deck Builder    (agent_mode + web_search dropped)
#   "...create a report..."  -> fixed Q&A form  (all three dropped)
#   anything else, agent on  -> agent           (auto_generate dropped)
#
# So a user who ticked all three and asked for a researched, auto-generated
# deck got a deck built with two of their three choices silently discarded.
#
# WHAT IT DOES INSTEAD: one small, fast LLM call reads the message and picks
# the route itself, using the same cheap router model AgentRegistry.select_llm
# already uses (see _build_router_provider). The toggles stop being mode
# switches and become permissions — they bound what the router is ALLOWED to
# pick, and within those bounds the decision comes from the query rather than
# from whichever substring happened to appear in it.
#
# The degrade path is deliberately exact: with no router provider (mock mode,
# no GEMINI_API_KEY, a call that fails or answers nonsense) this falls back to
# _deterministic_plan, which reproduces the previous if/elif chain move for
# move — so the "every flow works end-to-end with zero credentials" guarantee
# and all existing routing behaviour survive unchanged.

# Enough for this answer shape (four short fields) with real headroom — a
# truncated answer is worse than a slightly costlier one, since it reads as
# malformed JSON and degrades the whole turn to trigger matching.
_ROUTER_MAX_TOKENS = 512

# plan_turn's router provider — deliberately NOT settings.agent_router_model,
# which select_llm uses and keeps.
#
# Measured live against both. agent_router_model's default (gemma-4-26b-a4b-it)
# answers select_llm's one-line "which agent?" prompt fine, but on this larger
# prompt it spends 10-15s on hidden reasoning it cannot be told to skip (Gemma
# rejects thinkingConfig outright — see app/providers.py's
# _GEMMA_THINKING_TOKEN_FLOOR) and routinely blew GeminiProvider's own 15s HTTP
# timeout. Routing then degraded to trigger matching on roughly half of all
# turns: the feature was silently off, which is indistinguishable from it not
# existing. The configured Gemini chat model answers the same prompt in 1-3s,
# because thinkingConfig CAN disable reasoning for real Gemini models.
#
# This call sits in front of every single turn, so its latency is the product.
# It follows GEMINI_MODEL (already exposed on the Settings page) rather than
# introducing another env var. Cached for the life of the process for the same
# reason _build_router_provider is, and cleared by the same reset function.
_turn_router_built = False
_turn_router: AIProvider | None = None


def _build_turn_router_provider() -> AIProvider | None:
    global _turn_router_built, _turn_router
    if _turn_router_built:
        return _turn_router
    _turn_router_built = True
    if not settings.gemini_api_key:
        return None
    # No FallbackProvider wrapping, same as _build_router_provider: a failure
    # here is handled explicitly by plan_turn's try/except, which degrades to
    # trigger matching for that turn rather than landing on mock.
    _turn_router = GeminiProvider(
        settings.gemini_api_key, max_output_tokens=_ROUTER_MAX_TOKENS,
        model=settings.gemini_model or GeminiProvider.DEFAULT_MODEL,
    )
    return _turn_router


_ROUTE_DESCRIPTIONS = {
    "deck": "The user wants a PowerPoint/slide deck built as a real downloadable file.",
    "skill": "The user wants one of the file generators listed below to produce a document file.",
    "agent": ("The request needs multi-step work — tools, live web research, code, or this "
              "organisation's own indexed documents — rather than a single direct answer."),
    "direct": "A simple, single-step request the model can answer straight out.",
}


@dataclass(frozen=True)
class TurnPlan:
    """One turn's routing decision.

    `needs_web` is ADVISORY only — reported to the UI, never used to gate
    anything. The Web search toggle is a hard ceiling on capability (a user who
    switched it off must not get web calls because a router decided the query
    "needed" them), and inside that ceiling the model already decides per-call
    whether an offered tool is worth invoking. Its job is to explain the
    decision, and to let the UI point out when a turn would have benefited from
    a capability the user left switched off.

    There is deliberately no `needs_knowledge` counterpart: knowledge-base
    grounding is already autonomous and relevance-gated on every agent turn
    (see AutoGenOrchestrator.run), so a router hint would change nothing and
    only costs the router tokens it can't spare — see the prompt below.
    """
    route: str
    skill_id: str | None = None
    needs_web: bool = False
    routed_by_llm: bool = False
    reason: str = ""

    def public(self) -> dict:
        return {
            "route": self.route, "skill_id": self.skill_id, "needs_web": self.needs_web,
            "routed_by_llm": self.routed_by_llm, "reason": self.reason,
        }


def _loads_router_json(raw: str) -> dict:
    """Gemini's JSON mode, given no response schema, is not consistent about the
    shape it wraps an answer in: verified live against gemma-4-26b-a4b-it, the
    same prompt returns a bare object on one call and `["{\\"route\\": ...}"]` —
    the object re-encoded as a string inside a list — on the next. A parser that
    only accepts the bare object silently degrades routing to trigger matching
    on roughly every other turn, which looks exactly like the feature not
    working. Peel up to a few layers of list/string before giving up, and fall
    back to _extract_json (app/skills.py — the same tolerant extractor skill
    spec-drafting already relies on) for the fenced//prose-wrapped answers
    strict json.loads rejects outright."""
    value: object = raw
    for _ in range(4):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                extracted = _extract_json(value)
                if extracted is None:
                    raise
                return extracted
        elif isinstance(value, list):
            if not value:
                raise ValueError("router returned an empty list")
            value = value[0]
        else:
            break
    if not isinstance(value, dict):
        raise ValueError(f"router returned {type(value).__name__}, expected an object")
    return value


def _as_bool(value: object) -> bool:
    """Router models answer JSON booleans most of the time and the strings
    "true"/"yes" the rest of the time; both must mean the same thing."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "1")


def _deterministic_plan(keyword_match: dict | None, allow_agent: bool, allow_web: bool) -> TurnPlan:
    """Exactly the routing CopilotService.chat() did before plan_turn existed —
    the fallback whenever no router model is available. `keyword_match` is
    SkillPackageStore.select_for_chat's result (as .public()), i.e. the same
    substring match that used to be the whole routing decision."""
    if keyword_match is not None:
        skill_id = keyword_match["skill_id"]
        return TurnPlan(
            route="deck" if skill_id == "pptx" else "skill", skill_id=skill_id,
            # The Deck Builder used to hardcode web_search=True regardless of the
            # toggle; recording that as "this route wants the web" keeps the same
            # intent while letting the toggle stay the thing that actually decides.
            needs_web=(skill_id == "pptx"),
            reason="matched this generator's chat triggers",
        )
    if allow_agent:
        return TurnPlan(route="agent", needs_web=allow_web, reason="agent mode is on")
    return TurnPlan(route="direct", reason="no generator matched and agent mode is off")


async def plan_turn(
    message: str, *, skills: list[dict], keyword_match: dict | None,
    allow_agent: bool, allow_web: bool, provider: AIProvider,
) -> TurnPlan:
    """Decides what this turn should DO, from the message itself.

    `skills`: SkillPackageStore.list() — the router is told the real installed
    generators (id/name/output/description) rather than a hardcoded list, so an
    uploaded skill package becomes routable the moment it's installed.

    `allow_agent`/`allow_web`: the Agent mode and Web search toggles, as
    permissions. allow_agent picks which non-generation route is on the menu
    ("agent" when on, "direct" when off) — the router chooses between
    generating a file and answering, never between those two. allow_web never
    restricts routing at all (it gates tool offering at the point of use) and
    is passed only so the deterministic fallback can reproduce the previous
    behaviour exactly.

    `provider`: the caller's chat provider, used only for the same
    bare-MockProvider check AutoGenOrchestrator.run makes — explicitly mock
    means "deterministic, no real model call", so the router is never built
    for it and routing stays fully reproducible in tests.
    """
    fallback = _deterministic_plan(keyword_match, allow_agent, allow_web)
    router_provider = _build_turn_router_provider() if not isinstance(provider, MockProvider) else None
    if router_provider is None:
        return fallback

    by_id = {s["skill_id"]: s for s in skills}
    # "agent" and "direct" are the two ends of the same non-generation route,
    # picked by the toggle rather than by the router: Agent mode on means every
    # non-generation turn gets the agent's RAG grounding and tools, off means
    # every one is a plain answer. That's unchanged, and deliberately so —
    # letting the router downgrade an agent-mode turn to "direct" would quietly
    # cost it the relevance-gated knowledge-base grounding AutoGenOrchestrator.
    # run does on every turn. What the router decides is the thing that was
    # actually broken: whether this message wants a FILE generated at all.
    allowed = ["agent"] if allow_agent else ["direct"]
    if "pptx" in by_id:
        allowed.append("deck")
    if any(skill_id != "pptx" for skill_id in by_id):
        allowed.append("skill")

    # Kept deliberately terse. The default router model is Gemma, which spends
    # hidden reasoning tokens in proportion to how much it is given to weigh and
    # cannot be capped (see _ROUTER_MAX_TOKENS) — a longer prompt or a wordier
    # answer shape pushes the visible JSON past the budget and the whole call
    # degrades to trigger matching. Verified live: full 300-char skill
    # descriptions plus a five-field answer failed on most deck requests even at
    # 2048 tokens; this shape answers reliably at 1536.
    routes_block = "\n".join(f'- "{route}": {_ROUTE_DESCRIPTIONS[route]}' for route in allowed)
    skills_block = "\n".join(
        f'- "{s["skill_id"]}" -> .{s["output"]}: {s["description"][:110]}' for s in skills
    ) or "(none installed)"
    prompt = (
        "Route one message in an enterprise copilot. Answer with ONLY this JSON:\n"
        '{"route": "...", "skill_id": "..." or null, "needs_web": true|false, "reason": "..."}\n\n'
        f"Routes:\n{routes_block}\n\n"
        f'Generators (use as "skill_id" for the file routes):\n{skills_block}\n\n'
        'Pick a file route ONLY if the user wants a FILE made. Asking about, reviewing or '
        "discussing a document or deck is not a request to generate one.\n"
        '"needs_web": true only if answering needs current external facts.\n'
        '"reason": under 10 words, shown to the user.\n\n'
        f"Message:\n{message}"
    )

    try:
        result = await router_provider.complete(prompt, [], max_tokens=_ROUTER_MAX_TOKENS, json_mode=True)
        parsed = _loads_router_json(result.text)
        route = str(parsed.get("route", "")).strip()
    except Exception as exc:  # noqa: BLE001 - a router failure must degrade to trigger matching, never break the turn
        logger.warning("Turn routing failed, falling back to trigger matching: %s", exc)
        return fallback

    if route not in allowed:
        logger.warning("Turn router chose unavailable route %r, falling back to trigger matching.", route)
        return fallback

    skill_id = str(parsed.get("skill_id") or "").strip() or None
    if route in ("deck", "skill"):
        if skill_id not in by_id:
            # Router picked a generation route but couldn't name a real
            # generator — take the one the substring matcher found, if any.
            skill_id = "pptx" if route == "deck" and "pptx" in by_id else (keyword_match or {}).get("skill_id")
        if skill_id not in by_id:
            # Still nothing real to run. Answering the message beats running
            # the wrong generator against it.
            return TurnPlan(
                route="agent" if allow_agent else "direct", routed_by_llm=True,
                reason="no matching generator is installed",
            )
        # Keep route and skill_id consistent however the router paired them:
        # "pptx" is the Deck Builder's conversational flow, every other skill is
        # SkillRunService's fixed-question form. Neither reassignment can name a
        # route that wasn't in `allowed` — skill_id == "pptx" implies "deck" was
        # offered, and anything else implies "skill" was.
        route = "deck" if skill_id == "pptx" else "skill"
    else:
        skill_id = None

    return TurnPlan(
        route=route, skill_id=skill_id, needs_web=_as_bool(parsed.get("needs_web")),
        routed_by_llm=True, reason=str(parsed.get("reason", "")).strip()[:120],
    )


# --- agent-mode system prompt ------------------------------------------------
#
# _run_with_tools' previous system_message was one fixed sentence for every
# agent and every tool combination ("use tools when they genuinely help...").
# That left two real failure modes with nothing to catch them:
#   - the model answering a "who currently holds office X" / "what's the
#     latest Y" question from its own (dated) training data instead of
#     calling web_search, with no signal to the user that it skipped the
#     tool — see mcp_servers/tavily_search_server.py's FastMCP `instructions`
#     for the matching enforcement on the tool-description side.
#   - the coding agent answering with runnable code that isn't wrapped in a
#     ``` fence, which _queue_generated_code's regex can't see at all, so
#     the HITL gate silently never engages for that turn.
# Both are now explicit, agent-specific instructions built here instead of
# left to the model's judgment alone (see _looks_like_unfenced_code below
# for the belt-and-suspenders fallback on the coding side).
_BASE_SYSTEM_MESSAGE = (
    "You are Enterprise Copilot's agent-mode assistant. Be concise and "
    "accurate. If you are not confident in a fact, say so rather than "
    "guessing."
)

_CODING_SYSTEM_ADDENDUM = (
    " You are acting as the coding agent. Any runnable code in your answer "
    "MUST be inside a fenced code block using triple backticks (```language "
    "... ```) — this is not a style preference: code outside a fence is "
    "invisible to this app's human-approval step and will never be offered "
    "for execution. Even if you find no bug and the code the user gave you "
    "is already correct, you MUST still restate the full, complete, working "
    "code in a fenced block in your answer — never describe it in prose "
    "only and never reply with just an explanation. There must always be a "
    "concrete, runnable block for the human reviewer to approve, otherwise "
    "nothing can ever run. Never claim you ran code yourself; you can only "
    "write it — a human must approve it before it actually runs.\n\n"
    "If the user asks for a dashboard, report, chart, or any visual/"
    "document output: write a SINGLE self-contained Python script that "
    "writes one file named exactly output.html to the current working "
    "directory (open('output.html', 'w', encoding='utf-8').write(...) or "
    "equivalent) containing complete, valid, self-contained HTML — inline "
    "<style> and inline data, no external stylesheets/scripts/images/fonts, "
    "since the sandbox has no network access and nothing else will be "
    "packaged alongside it. Populate it with the REAL data already given to "
    "you in this conversation (e.g. from a prior web_search result) — never "
    "invent placeholder numbers. Do not print the HTML to stdout; the file "
    "is what becomes downloadable once approved, print() output is not."
)

_WEB_SEARCH_SYSTEM_ADDENDUM = (
    " The web_search tool is available. For ANY question about current "
    "events, who currently holds a position/office, prices, versions, "
    "schedules, or any fact that could have changed since your training "
    "cutoff — you MUST call web_search first and base your answer on its "
    "results, citing the source URL for each fact you use. Do not answer "
    "such questions from memory alone, even if you believe you already "
    "know the answer: your training data has a fixed cutoff and may be "
    "outdated. If web_search returns no useful result, say so explicitly "
    "instead of falling back to a guess."
)


# --- Deck Builder system prompt ----------------------------------------------
#
# Distinct from _build_agent_mode_system_message above: this drives a
# MagenticOneGroupChat's single participant (DeckBuilderOrchestrator, near
# the bottom of this file), not the plain agent-mode AssistantAgent. Embeds
# the pptx skill's own SKILL.md body (skill.instructions) directly, since
# Deck Builder bypasses SkillRunService/draft_spec entirely (app/skills.py)
# — that skill's Workflow/design-principles content has to reach the model
# through this call site instead.
_DECK_BUILDER_BASE_INSTRUCTIONS = (
    "You are Enterprise Copilot's Deck Builder — a conversational assistant "
    "that plans, researches, and drafts PowerPoint presentations. You have a "
    "web_search tool: use it whenever the deck needs real, current, or "
    "specific factual data (numbers, dates, named facts) rather than "
    "guessing or inventing plausible-sounding figures.\n\n"
    "If the user's request is genuinely underspecified — you don't know the "
    "topic, audience, or tone — ask ONE short clarifying question and stop; "
    "do not ask more than a couple of questions total across the "
    "conversation, and never ask about something the user already told you.\n\n"
    "Once you have enough to build the deck (topic, audience, and tone, at "
    "minimum), respond with ONLY a single JSON object matching the spec "
    "shape documented in the skill instructions below — no prose, no "
    "markdown code fence, no explanation before or after it. If you are "
    "still gathering information or asking a question, respond with plain "
    "prose instead (never partial or placeholder JSON)."
)


def _build_deck_builder_system_message(skill_instructions: str) -> str:
    return f"{_DECK_BUILDER_BASE_INSTRUCTIONS}\n\n---\n\n{skill_instructions}"


def _build_agent_mode_system_message(agent_name: str, tool_names: set[str]) -> str:
    """Base instruction plus whichever addenda actually apply to this turn —
    "coding" only for the coding agent, "web search" only when the
    web_search tool was actually offered (see get_mcp_tools(web_search=...))."""
    message = _BASE_SYSTEM_MESSAGE
    if agent_name == "coding-agent":
        message += _CODING_SYSTEM_ADDENDUM
    if "web_search" in tool_names:
        message += _WEB_SEARCH_SYSTEM_ADDENDUM
    return message


# Fallback code detection for _queue_generated_code: catches an unfenced
# answer that is still unmistakably source code (the model ignored the fence
# instruction above) so the HITL gate still engages instead of silently
# doing nothing. Deliberately conservative — a line only counts as "code"
# when it opens a definition/import/control-flow construct or is indented
# under one; a couple of stray punctuation marks in prose can't trigger it.
_CODE_OPENER = re.compile(
    r"^\s*("
    r"def\s+\w|class\s+\w|import\s+\w|from\s+\w.*\bimport\b|"          # Python
    r"(async\s+)?function\s+\w|(const|let|var)\s+\w+\s*=|"             # JS/TS
    r"(public|private|static|protected)\s+[\w<>\[\]]+\s+\w+\s*\(|"      # Java/C#/C++
    r"if\s+.*:\s*$|for\s+.*:\s*$|while\s+.*:\s*$"                       # Python control flow
    r")",
    re.MULTILINE,
)


def _parse_web_search_result(raw: str) -> list[dict]:
    """web_search's own tool result (mcp_servers/tavily_search_server.py)
    returns a JSON string: {"query", "results": [{"title","url","content"},
    ...]} on success, {"error": "..."} on failure — but by the time it
    reaches here, AutoGen's MCP tool bridge (autogen_ext.tools.mcp) has
    wrapped that string in the standard MCP tool-result content-block
    envelope: `[{"type": "text", "text": "<the original JSON string>"}]`.
    Verified live (2026-08-22): ToolCallExecutionEvent.content[i].content
    was genuinely `'[{"type": "text", "text": "{\\"query\\": ...}"}]'`, not
    the bare dict this function originally assumed — every real web_search
    call parsed as [] until this was accounted for, even though the tool
    itself and Tavily both succeeded (is_error=False, HTTP 200). Handles
    both shapes so this also still works if a future AutoGen version (or a
    different MCP client) delivers the bare JSON string directly. Returns
    [] for a failure/genuinely malformed result — never raises, since a
    parse failure here must not break the turn (see _run_with_tools' call
    site)."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return []
    # MCP content-block envelope: unwrap to the actual JSON payload string,
    # then parse that.
    if isinstance(payload, list):
        text_parts = [
            block.get("text", "") for block in payload
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not text_parts:
            return []
        try:
            payload = json.loads("".join(text_parts))
        except (ValueError, TypeError):
            return []
    if not isinstance(payload, dict) or "error" in payload:
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    return [
        {"title": r.get("title") or "Untitled", "url": r.get("url") or "", "content": r.get("content") or ""}
        for r in results if isinstance(r, dict) and r.get("url")
    ]


def _looks_like_unfenced_code(text: str) -> str | None:
    """None if nothing in this text reads as an unfenced code block;
    otherwise the contiguous run of lines that does. Anchors on a line that
    opens a definition/import/control-flow construct (_CODE_OPENER), then
    extends through whatever immediately follows that's indented under it,
    blank, or itself another opener — i.e. the same "keep going while it
    still looks like the same block" a person would use to eyeball where
    code starts and ends in an otherwise-prose answer. Stops at the first
    line that's back at column 0 and isn't itself an opener (prose resuming)."""
    lines = text.splitlines()
    opener_idx = next((i for i, ln in enumerate(lines) if _CODE_OPENER.match(ln)), None)
    if opener_idx is None:
        return None

    block = [lines[opener_idx]]
    for ln in lines[opener_idx + 1:]:
        if not ln.strip():
            block.append(ln)  # blank line inside the block — keep going
            continue
        if ln.startswith((" ", "\t")) or _CODE_OPENER.match(ln):
            block.append(ln)
            continue
        break  # back to column-0 prose — block is over
    # Trim trailing blank lines collected above.
    while block and not block[-1].strip():
        block.pop()
    if len(block) < 2:
        return None  # a single opener line alone isn't worth queuing
    return "\n".join(block)


# --- registration -----------------------------------------------------------

@dataclass(frozen=True)
class AgentDefinition:
    name: str
    purpose: str
    trigger_keywords: tuple[str, ...]
    skills: tuple[str, ...]


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    trigger: str
    workflow: str


class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, AgentDefinition] = {}

    def register(self, agent: AgentDefinition) -> None:
        self._agents[agent.name] = agent

    def get(self, name: str) -> AgentDefinition | None:
        return self._agents.get(name)

    def list(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    def select(self, task: str) -> AgentDefinition:
        """Keyword-based selection. Falls back to the general-purpose agent,
        matching the direct-call-for-simple-tasks guidance in
        docs/agent-architecture.md.

        Kept as the fallback path for select_llm() below (a router-call
        failure — no GEMINI_API_KEY, rate limit, malformed response, mock
        mode — must never break routing entirely), and as the synchronous
        primitive existing tests/tooling can still call directly."""
        lowered = task.lower()
        for agent in self._agents.values():
            if agent.name == "general":
                continue
            if any(keyword in lowered for keyword in agent.trigger_keywords):
                return agent
        return self._agents["general"]

    async def select_llm(self, task: str, router_provider: "AIProvider | None") -> tuple[AgentDefinition, bool]:
        """LLM-based agent selection: asks a small, fast classifier model
        (see settings.agent_router_model, app/config.py) which registered
        agent should handle `task`, instead of the fixed substring match in
        select() above. Returns (chosen agent, used_llm) — used_llm is False
        whenever this fell back to select() (router_provider is None, e.g.
        no GEMINI_API_KEY configured; the call raised — rate limit, network,
        malformed JSON; or the model named an agent that isn't registered),
        so callers/telemetry can tell a real LLM routing decision from a
        keyword-matched fallback one.

        router_provider is a raw provider (no FallbackProvider wrapping —
        see build_provider_for), built once by the caller and passed in
        rather than constructed here, so a missing/broken router
        configuration is resolved once per orchestrator, not on every turn.
        """
        if router_provider is None:
            return self.select(task), False

        candidates = [a for a in self._agents.values() if a.name != "general"]
        options = "\n".join(f'- "{a.name}": {a.purpose}' for a in candidates)
        prompt = (
            "Classify which agent should handle this user message. Respond with ONLY a JSON "
            'object of the exact shape {"agent": "<name>"} — no other text.\n\n'
            f"Available agents:\n{options}\n"
            '- "general": anything else — a simple, single-step request that doesn\'t need a '
            "specialist agent.\n\n"
            f"User message:\n{task}"
        )
        try:
            # 64 is enough for the actual JSON answer; GeminiProvider raises
            # this internally to _GEMMA_THINKING_TOKEN_FLOOR when the router
            # model is Gemma (a "thinking" model whose hidden reasoning can't
            # be disabled the way real Gemini models' can — see
            # app/providers.py:GeminiProvider.complete for why).
            result = await router_provider.complete(prompt, [], max_tokens=64, json_mode=True)
            parsed = json.loads(result.text)
            chosen_name = str(parsed.get("agent", "")).strip()
        except Exception as exc:  # noqa: BLE001 - a router failure must degrade to keyword matching, never break the turn
            logger.warning("LLM agent routing failed, falling back to keyword matching: %s", exc)
            return self.select(task), False

        agent = self._agents.get(chosen_name)
        if agent is None:
            if chosen_name != "general":
                logger.warning("LLM router named unknown agent %r, falling back to keyword matching.", chosen_name)
                return self.select(task), False
            agent = self._agents["general"]
        return agent, True


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, SkillDefinition] = {}

    def register(self, skill: SkillDefinition) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> SkillDefinition | None:
        return self._skills.get(name)

    def list(self) -> list[SkillDefinition]:
        return list(self._skills.values())


def default_agent_registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(AgentDefinition(
        name="general", purpose="Direct response for simple, single-step requests.",
        trigger_keywords=(), skills=(),
    ))
    registry.register(AgentDefinition(
        name="coding-agent",
        purpose=(
            "Implement, test and debug software tasks. Also the only agent that can produce a "
            "downloadable file (HTML dashboard, PDF report, chart) — it does so by writing and "
            "running Python code, so any request for a dashboard, report, or visual/document "
            "output belongs here even if the user never says the word 'code'."
        ),
        # "dashboard"/"report"/"html file"/"download": a user asking for a
        # generated dashboard/report is asking for code (the only way this
        # app produces one — see _CODING_SYSTEM_ADDENDUM) even though they
        # never say "code"/"script" themselves. Verified live (2026-08-22):
        # "Create a downloadable html dashboard by finding who is the KING
        # of..." matched none of the original keywords, routed to `general`
        # (no code path, no HITL at all) — and the plain-completion fallback
        # then also failed from token starvation (see max_output_tokens_code
        # below), producing a silently empty/mock response with no
        # explanation. Both fixed together: correct routing here, and a
        # generous fallback budget for token-hungry turns regardless of
        # which skill selected them (see max_tokens below).
        trigger_keywords=(
            "code", "bug", "function", "script", "debug", "implement", "```",
            "dashboard", "report", "html file", "download",
        ),
        skills=("coding",),
    ))
    registry.register(AgentDefinition(
        name="research-agent", purpose="Answer questions using indexed project knowledge.",
        trigger_keywords=("document", "knowledge base", "cite", "source", "according to"),
        skills=("knowledge-rag",),
    ))
    registry.register(AgentDefinition(
        name="data-analysis-agent", purpose="Analyze data and produce validated insights.",
        trigger_keywords=("csv", "dataset", "analyze", "statistic", "chart"),
        skills=("data-analysis",),
    ))
    return registry


def default_skill_registry() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(SkillDefinition("coding", "Software creation, modification, testing or debugging.",
                                       "Inspect -> implement -> test -> fix -> validate."))
    registry.register(SkillDefinition("knowledge-rag", "Questions requiring uploaded/project knowledge.",
                                       "Retrieve relevant chunks -> build context -> answer -> identify sources."))
    registry.register(SkillDefinition("data-analysis", "Dataset analysis, statistics or charts.",
                                       "Load -> validate -> profile -> analyze -> visualize -> verify."))
    return registry


# --- orchestration result -----------------------------------------------------

@dataclass
class OrchestrationResult:
    text: str
    agent: str
    skills: list[str] = field(default_factory=list)
    provider: str = "mock"
    # The actual model behind `provider` — see ProviderResult.model
    # (app/providers.py). None when unknown (mock, or a provider that
    # doesn't expose a model name).
    model: str | None = None
    used_fallback: bool = False
    hitl_pending: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    context_guardrail: dict | None = None
    # Names of MCP tools actually invoked this turn (see get_mcp_tools()) —
    # empty unless the tool-calling path below ran and the model chose to
    # call something.
    tool_calls: list[str] = field(default_factory=list)
    # Real web_search results the model actually saw this turn — {"title",
    # "url", "content"} each, parsed straight out of its own tool result
    # (see _parse_web_search_result) so the UI can render genuine clickable
    # citations. Empty unless web_search was both offered and called.
    web_sources: list[dict] = field(default_factory=list)
    # A generated .html/.pdf file the coding agent's approved code produced
    # and left in its workspace — {"artifact_id", "filename", "mime_type",
    # "size_bytes", "view_url"} each (StoredArtifact.public(), app/
    # artifacts.py). Only ever non-empty on the live-SSE-wait path (agent-
    # mode POST /api/chat/stream) — a request that only queues (the non-live
    # /api/chat coding path) can't know the artifact yet, since nothing has
    # run at the point this turn returns.
    downloadable_artifacts: list[dict] = field(default_factory=list)
    # Updated compact-memory state (see app/memory.py) for CopilotService to
    # persist via SessionStore.set_field — None only if this turn never
    # touched context management at all (shouldn't happen in practice; every
    # branch below sets it).
    memory_state: dict | None = None


_CODE_FENCE = re.compile(r"```(?:\w+\n)?(.*?)```", re.DOTALL)


def extract_code_block(task: str) -> str | None:
    match = _CODE_FENCE.search(task)
    return match.group(1).strip() if match else None


class AgentOrchestrator:
    """Application contract. Framework-independent by design."""

    async def run(self, task: str, context: dict, on_event: EventSink | None = None) -> OrchestrationResult:
        raise NotImplementedError


class AutoGenOrchestrator(AgentOrchestrator):
    """v1 implementation. Every AutoGen-specific type/call stays in this class."""

    # Relevant-context management is now compact_history()/CompactingChatCompletionContext
    # (app/memory.py, buffer size app.memory.BUFFER_SIZE) — a hard recent-N-turns
    # window superseded by "buffer the last few turns verbatim, compact-summarize
    # anything older" so a long session degrades gracefully instead of silently
    # losing turns.

    # Relevance gate for auto-grounding (see run()): a hit must have genuine lexical
    # overlap (bm25_score > 0 — the hash-embedding vector leg alone is too noisy to
    # trust on its own, see docs/rag.md's caveat) AND a reranker score clearing this
    # bar. bm25_score > 0 is the real noise filter (a truly irrelevant query shares
    # no terms at all, scoring exactly 0); this threshold just needs to be low enough
    # to survive natural phrasing that doesn't happen to maximize lexical coverage —
    # calibrated against real queries scoring ~0.18-0.33 for genuinely relevant hits.
    RELEVANCE_MIN_RERANK = 0.15

    def __init__(self, provider: AIProvider, agent_registry: AgentRegistry,
                 skill_registry: SkillRegistry, rag_store, hitl_service, guardrails=None):
        self.provider = provider
        self.agent_registry = agent_registry
        self.skill_registry = skill_registry
        self.rag_store = rag_store
        self.hitl_service = hitl_service
        self.guardrails = guardrails

    def _is_relevant(self, source: dict) -> bool:
        return source["bm25_score"] > 0 and source["rerank_score"] >= self.RELEVANCE_MIN_RERANK

    async def run(self, task: str, context: dict, on_event: EventSink | None = None) -> OrchestrationResult:
        await _emit(on_event, {"stage": "selecting_agent", "label": "Selecting agent…"})
        # LLM-based routing (AgentRegistry.select_llm) replaces pure keyword
        # matching as the primary router — see settings.agent_router_model
        # (app/config.py). Explicitly bare MockProvider means "deterministic,
        # no real model call" (same contract as the MCP tool-calling gate
        # below), so the router provider is never even built for that case —
        # select_llm(router_provider=None) falls straight to select()'s
        # keyword match, same as any other router failure.
        router_provider = (
            _build_router_provider() if not isinstance(self.provider, MockProvider) else None
        )
        agent, routed_by_llm = await self.agent_registry.select_llm(task, router_provider)
        agent_name = agent.name
        await _emit(on_event, {
            "stage": "agent_selected", "label": f"Selected {agent_name} ({'LLM router' if routed_by_llm else 'keyword match'}).",
            "agent": agent_name, "routed_by_llm": routed_by_llm,
        })
        skills_used = list(agent.skills)
        sources: list[dict] = []
        hitl_pending: list[str] = []
        context_guardrail: dict | None = None
        prompt_parts = [task]

        # skill invocation: knowledge-rag retrieves (hybrid vector+BM25, reranked) chunks,
        # screens them for indirect prompt injection, then grounds the prompt with what's left.
        #
        # Keyword-selected research-agent always checks. Every other task ALSO gets a
        # relevance-gated check — an enterprise copilot shouldn't need the user to say
        # "document"/"cite"/"source" before it looks at its own knowledge base; it should
        # ground on what it actually has whenever that's genuinely relevant, and otherwise
        # stay out of the way. See docs/rag.md.
        embedding_desc = describe_embedding_config()
        await _emit(on_event, {
            "stage": "retrieving_knowledge", "label": "Searching knowledge base…",
            "embedding_provider": embedding_desc["provider"], "embedding_model": embedding_desc["model"],
        })
        if "knowledge-rag" in skills_used:
            sources = await self.rag_store.search(task, limit=3)
        else:
            candidates = await self.rag_store.search(task, limit=3)
            sources = [s for s in candidates if self._is_relevant(s)]
            if sources:
                skills_used.append("knowledge-rag")
                # Auto-grounding only *upgrades* the generic fallback agent —
                # it must never overwrite an already-specific selection (e.g.
                # "use the coding agent" correctly picking coding-agent must
                # not get silently relabeled research-agent just because some
                # tangentially related document also matched).
                if agent_name == "general":
                    agent_name = "research-agent"
        await _emit(on_event, {
            "stage": "sources_found", "label": f"Found {len(sources)} relevant source(s)." if sources
                     else "No relevant sources found — answering from general knowledge.",
            "count": len(sources),
        })

        if sources and self.guardrails is not None:
            context_guardrail = self.guardrails.check_context(sources)
            if not context_guardrail["allowed"]:
                blocked = set(context_guardrail["flagged_chunk_ids"])
                sources = [s for s in sources if s["chunk_id"] not in blocked]
                await _emit(on_event, {
                    "stage": "context_guardrail_blocked",
                    "label": f"Guardrails filtered {len(blocked)} retrieved chunk(s) before use.",
                })
            # PII redaction — a distinct concern from the injection screen
            # above (which drops a chunk outright); this one masks and
            # KEEPS every remaining chunk, so it runs after that screen has
            # already settled which chunks survive. Covers both what goes
            # into the prompt (`joined`, below) and what's returned as
            # citations (OrchestrationResult.sources -> ChatResponse.sources
            # -> the UI's "Sources" panel) — retrieval-time only, per
            # docs/rag.md: the documents themselves stay stored unredacted.
            sources, pii_findings = self.guardrails.redact_context_pii(sources)
            if pii_findings["redacted_count"]:
                await _emit(on_event, {
                    "stage": "context_pii_redacted",
                    "label": f"Guardrails redacted PII in {pii_findings['redacted_count']} retrieved chunk(s).",
                })
            context_guardrail = {**context_guardrail, **pii_findings}
        if sources:
            # The retrieval gate is a cheap lexical/vector heuristic (see docs/rag.md's
            # caveat) — on a small corpus it will sometimes surface a weak/irrelevant
            # match. Rather than force the model to only answer from this context (which
            # produces "the context doesn't cover that" refusals on real questions), the
            # model gets the retrieved context and decides for itself whether it actually
            # answers the question — the same judgment call a person would make skimming
            # a search result. This is autonomy applied to relevance, not just retrieval.
            joined = "\n".join(f"- [{s['filename']}#{s['chunk_index']}]: {s['snippet']}" for s in sources)
            prompt_parts.append(
                "The following was retrieved from the knowledge base and may or may not be "
                "relevant to the question below. If it genuinely helps answer the question, "
                "use it and cite sources as [filename#chunk_index]. If it does not address "
                "the question, ignore it completely and answer from your own knowledge — "
                "never claim you lack information just because this excerpt doesn't cover it:\n"
                f"{joined}"
            )

        # Full session history — NOT pre-windowed. compact_history()/
        # CompactingChatCompletionContext (app/memory.py) below own the
        # buffering (last app.memory.BUFFER_SIZE turns verbatim) and
        # summarization of anything older; truncating here first would just
        # throw that older context away before either ever saw it.
        history = context.get("history", [])
        images = context.get("images")
        memory_state = CompactMemoryState.from_dict(context.get("memory_state"))

        prompt = "\n\n".join(prompt_parts)
        # Nontrivial code routinely exceeds the default token budget (see
        # app/config.py) — the coding-agent gets a generous override so the
        # model isn't cut off mid-function. (Only applies to the plain
        # completion path below — see _run_with_tools' docstring for why the
        # tool-calling path doesn't have an equivalent override yet.)
        max_tokens = settings.max_output_tokens_code if "coding" in skills_used else None

        # Tool invocation: real MCP tool-calling (app/mcp_tools.py) — distinct
        # from the code-queue step above, which only ever queues; this
        # genuinely runs (whatever tools get_mcp_tools() found), gated on
        # both tools being available AND a real AutoGen model client existing
        # for whatever's configured. Mock mode / missing credentials can't
        # tool-call, same as every other "not configured" case in this app —
        # falls straight through to the plain completion below, as does any
        # failure partway through (a broken MCP server, a model-client
        # hiccup) rather than failing the whole turn.
        text: str | None = None
        provider_name: str | None = None
        model_name: str | None = None
        used_fallback = False
        tool_names: list[str] = []
        web_sources: list[dict] = []
        degraded_from_tools = False  # set True only if the tool-calling attempt below ran and failed/emptied out
        # Explicitly bare MockProvider (as opposed to a FallbackProvider that
        # might itself land on mock) means the caller wants deterministic,
        # no-real-model-call behavior — tests construct AutoGenOrchestrator
        # this way expecting exactly that. build_streaming_model_client()
        # below resolves straight from global settings, independent of
        # self.provider, so this check is what actually honors that contract
        # (settings.ai_mode alone isn't enough: it can be "configured" with
        # real credentials in .env while a test still injects MockProvider()
        # directly, and that must stay mock, not silently call a real model).
        mcp_tools = (
            await get_mcp_tools(web_search=bool(context.get("web_search")))
            if not isinstance(self.provider, MockProvider) else []
        )
        if mcp_tools:
            model_client, resolved_provider, resolved_model = build_streaming_model_client(
                function_calling=True, vision=bool(images),
            )
            if model_client is not None:
                await _emit(on_event, {
                    "stage": "tools_available",
                    "label": f"{len(mcp_tools)} tool(s) available: {', '.join(t.name for t in mcp_tools)}.",
                    "tools": [t.name for t in mcp_tools],
                })
                try:
                    text, tool_names, web_sources, memory_state, memory_preview = await self._run_with_tools(
                        model_client, mcp_tools, prompt, history, agent_name, on_event, images, memory_state,
                    )
                    # A successful-but-empty result is just as unusable as an
                    # exception: verified live (2026-08-22) — a broad query
                    # can exhaust max_tool_iterations (repeated web_search
                    # reformulation) without AutoGen ever emitting a final
                    # Response event, leaving _run_with_tools' final_text at
                    # its initial "" with no exception raised at all. The old
                    # `if text is None` fallback check below only catches the
                    # exception path (text = None), never this one ("" is
                    # not None) — so the turn silently returned an empty
                    # chat message, with no HITL queued (there was no code to
                    # extract from empty text either) and no explanation.
                    if not (text or "").strip():
                        raise RuntimeError(
                            f"Tool-calling turn produced no final answer after {len(tool_names)} tool call(s) "
                            "(likely exhausted max_tool_iterations without the model ever concluding)."
                        )
                    provider_name, model_name = resolved_provider, resolved_model
                    await _emit(on_event, {
                        "stage": "model_call",
                        "label": f"Answered ({provider_name}/{model_name})"
                                 + (f" using {len(tool_names)} tool call(s)." if tool_names else "."),
                        "provider": provider_name, "model": model_name, "used_fallback": False,
                        # The real system_message this turn's AssistantAgent was
                        # built with (_build_agent_mode_system_message,
                        # recomputed identically here — deterministic given the
                        # same (agent_name, tool_names)) alongside the user-facing
                        # prompt, so "View context sent to model" (static/app.js)
                        # shows the complete picture, not just half of it.
                        "system_preview": _build_agent_mode_system_message(agent_name, {t.name for t in mcp_tools}),
                        "prompt_preview": prompt[:2000], "response_preview": (text or "")[:2000],
                        "tool_calls": tool_names,
                        # The buffered/summarized prior-turn context this
                        # turn's AssistantAgent actually saw via its
                        # CompactingChatCompletionContext — see
                        # _run_with_tools' return and render_memory_preview.
                        "memory_preview": memory_preview,
                    })
                    degraded_from_tools = False
                except Exception as exc:  # noqa: BLE001 - degrade to the plain completion below, never fail the turn
                    logger.warning("Tool-calling agent turn failed, falling back to a plain completion: %s", exc)
                    text = None
                    tool_names = []
                    web_sources = []  # a failed attempt's partial sources must not attach to the fallback answer
                    degraded_from_tools = True
                finally:
                    await model_client.close()

        if text is None:
            await _emit(on_event, {"stage": "thinking", "label": f"Thinking ({agent_name})…"})
            compacted, memory_state = await compact_history(self.provider, history, memory_state, BUFFER_SIZE)
            # A turn degrading here straight from a failed/empty tool-calling
            # attempt gets the same generous budget as "coding" regardless of
            # skill — verified live (2026-08-22): a tool-heavy prompt (5 web_
            # search results' worth of context) hit the plain 256-token
            # default on this path, Ollama returned done_reason=length with
            # zero visible text (the whole budget spent on hidden "thinking"
            # — the same starvation issue max_output_tokens_code exists for
            # on the "coding" skill), and FallbackProvider then degraded a
            # SECOND time to a generic mock reply with no explanation.
            fallback_max_tokens = (
                settings.max_output_tokens_code
                if "coding" in skills_used or degraded_from_tools
                else max_tokens
            )
            result = await self.provider.complete(prompt, compacted, max_tokens=fallback_max_tokens, images=images)
            text, provider_name, model_name, used_fallback = (
                result.text, result.provider, result.model, result.used_fallback
            )
            await _emit(on_event, {
                "stage": "model_call",
                "label": f"Answered ({provider_name}{f'/{model_name}' if model_name else ''}).",
                "provider": provider_name, "model": model_name, "used_fallback": used_fallback,
                # No separate system_preview here: this is the plain-completion
                # path (AIProvider.complete — mock/Gemini/Ollama's own simple
                # HTTP call), which has no distinct system-prompt concept, only
                # a single combined prompt string. Genuinely nothing to show,
                # not an oversight — see _build_agent_mode_system_message for
                # the tool-calling path, which does have one.
                "prompt_preview": prompt[:2000], "response_preview": text[:2000],
                # `compacted` (above) is exactly what compact_history() built
                # and handed to self.provider.complete() as prior-turn
                # context — the buffered recent turns plus, once the session
                # is long enough, a synthetic summary turn. Rendered here so
                # "View context sent to model" (static/app.js) can show it
                # alongside the prompt, instead of the memory the model
                # actually used being invisible. See render_memory_preview.
                "memory_preview": render_memory_preview(compacted),
            })

        # Tool invocation: coding agent queues code execution behind HITL
        # approval (UserProxyAgent/CodeExecutorAgent, app/hitl_agents.py) — it
        # never runs inline from the orchestrator. Detected from the model's
        # OWN answer, not the user's original task: "write me a script that
        # ..." has no code in `task` at all (there's nothing to queue until
        # the model has actually written something), and "debug this
        # ```code```" gets whatever corrected version the model's answer
        # presents, not blindly the user's original draft — either way, this
        # is "the code the assistant just told you to run," which is what
        # approval should gate.
        downloadable_artifacts: list[dict] = []
        if "coding" in skills_used:
            text, coding_hitl_ids, downloadable_artifacts = await self._queue_generated_code(text, context, on_event)
            hitl_pending.extend(coding_hitl_ids)

        return OrchestrationResult(
            text=text,
            agent=agent_name,
            skills=skills_used,
            provider=provider_name,
            model=model_name,
            used_fallback=used_fallback,
            web_sources=web_sources,
            hitl_pending=hitl_pending,
            sources=sources,
            context_guardrail=context_guardrail,
            tool_calls=tool_names,
            downloadable_artifacts=downloadable_artifacts,
            memory_state=memory_state.to_dict(),
        )

    async def _queue_generated_code(
        self, text: str, context: dict, on_event: EventSink | None,
    ) -> tuple[str, list[str], list[dict]]:
        """Extracts a fenced code block from the model's own answer (if any)
        and queues it for HITL approval — see run()'s call site for why this
        checks the *answer*, not the original task. Returns (possibly
        updated text, hitl request ids, downloadable artifacts) — text gains
        a short appended note once a live decision is known (see
        context["allow_live_hitl_wait"], only set by CopilotService.
        chat_stream's SSE delegate branch); the non-live path leaves text
        untouched (the UI already shows "awaiting approval" via the Pending
        approvals panel/chip) and always returns an empty artifact list
        (nothing has run yet to have produced one).

        Falls back to _looks_like_unfenced_code when there's no ``` fence at
        all — the coding-agent system prompt (_CODING_SYSTEM_ADDENDUM) now
        tells the model to always fence runnable code, but a model that
        ignores that instruction must still not silently skip HITL; this is
        the belt-and-suspenders catch for that case, flagged as
        heuristically detected (not extracted from an explicit fence) so a
        reviewer knows why the code looks like it does."""
        code = extract_code_block(text)
        heuristic = False
        if not code:
            code = _looks_like_unfenced_code(text)
            heuristic = code is not None
        if not code:
            return text, [], []

        record = self.hitl_service.submit_code_execution(code, context.get("session_id"))
        request_id = record["request_id"]
        # Deliberately NOT "code execution" — per guardrails.md this code
        # sits behind an explicit human approval step and never runs
        # automatically from here, so the event says exactly that instead
        # of implying it already ran.
        await _emit(on_event, {
            "stage": "code_queued",
            "label": ("Unfenced code detected (heuristic) — queued for your approval before it can run."
                      if heuristic else "Code detected — queued for your approval before it can run."),
            "request_id": request_id, "heuristic": heuristic,
        })
        if not context.get("allow_live_hitl_wait"):
            return text, [request_id], []

        # Agent-mode SSE only — a long-lived connection can genuinely wait
        # for a live decision instead of only ever reporting "queued".
        # Represented by a real UserProxyAgent (app/hitl_agents.py:
        # await_human_decision); cancelling this turn (the chat's own Stop
        # button) cleanly abandons the wait — the request stays
        # WAITING_FOR_APPROVAL, still decidable later from Agents & Tools.
        await _emit(on_event, {
            "stage": "awaiting_approval", "label": "Waiting for your approval in Agents & Tools…",
            "request_id": request_id,
        })
        decided = None
        try:
            decided = await await_human_decision(self.hitl_service, request_id)
        except Exception as exc:  # noqa: BLE001 - a wait failure degrades to "still queued", never fails the turn
            logger.warning("Live HITL wait failed for %s: %s", request_id, exc)
        if decided is None:
            return text, [request_id], []
        if decided["status"] == "REJECTED":
            text += "\n\n---\n**Not run — rejected by reviewer.**"
            await _emit(on_event, {
                "stage": "code_decided", "label": "Rejected by reviewer.",
                "request_id": request_id, "approved": False,
            })
            return text, [request_id], []

        # COMPLETED
        result = decided.get("result") or {}
        stdout, stderr = (result.get("stdout") or "")[:1000], (result.get("stderr") or "")[:1000]
        text += (
            "\n\n---\n**Execution result** (ok: {ok}, exit code: {rc}):\n```\n{out}\n```".format(
                ok=result.get("ok"), rc=result.get("returncode"),
                out=(stdout + (f"\nstderr:\n{stderr}" if stderr else "")) or "(no output)",
            )
        )
        # A generated .html/.pdf the approved code left behind — persisted by
        # HitlService.decide() (app/artifacts.py) — surfaced both inline
        # (a real, working link the model can't fabricate, since it's built
        # from the actual stored artifact_id) and structurally in the third
        # return value for the UI to render as a proper "View / Download"
        # chip rather than the user having to click a raw markdown link.
        downloadable = decided.get("downloadable_artifacts") or []
        for artifact in downloadable:
            text += f"\n\n📄 **{artifact['filename']}** — [View]({artifact['view_url']})"
        await _emit(on_event, {
            "stage": "code_decided", "label": "Approved — code ran.",
            "request_id": request_id, "approved": True, "downloadable_artifacts": downloadable,
        })
        return text, [request_id], downloadable

    async def _run_with_tools(
        self, model_client, tools: list, prompt: str, history: list[dict],
        agent_name: str, on_event: EventSink | None, images: list[dict] | None,
        memory_state: CompactMemoryState,
    ) -> tuple[str, list[str], CompactMemoryState]:
        """One real AutoGen tool-calling turn: a single AssistantAgent wired
        to this turn's resolved model client and available MCP tools (see
        get_mcp_tools()), streamed through on_messages_stream so every tool
        call/result becomes a real progress event as it actually happens —
        not a fabricated "tool used" claim, a genuine one (the tool really
        runs — see mcp_servers/general_tools_server.py and docs/tools.md).

        `history` seeds the agent's own CompactingChatCompletionContext via
        initial_messages (the full transcript — the context does its own
        buffering/summarizing, see app/memory.py) rather than being replayed
        as extra task messages, so a long session doesn't flood this turn's
        events with every prior turn. Only the new grounded `prompt` (plus
        `images`, if any — sent as a MultiModalMessage) is passed as the
        task.

        Known limitation: unlike the plain-completion path above, there's no
        per-call token-budget override here yet (AutoGen's ChatCompletionClient
        doesn't take one per-request the way AIProvider.complete() does) — a
        future pass could set one at client-construction time if the same
        reasoning-model starvation issue (see app/config.py) shows up here.

        Returns (final_text, names_of_tools_actually_called, web_sources,
        updated_memory_state as a CompactMemoryState object, matching
        compact_history()'s return shape, memory_preview — a human-readable
        rendering of the buffered/summarized prior-turn context this turn's
        agent actually received, see render_memory_preview) — tool_calls/
        web_sources are empty if the model answered directly without needing
        any tool.

        web_sources is parsed straight out of web_search's own
        ToolCallExecutionEvent result (mcp_servers/tavily_search_server.py
        returns {"query", "results": [{"title","url","content"}, ...]} as a
        JSON string) — the exact sources the model itself saw, not a
        separately re-run search, so the UI can render real clickable
        citations (see OrchestrationResult.web_sources, app/models.py
        ChatResponse.web_sources).
        """
        model_context = CompactingChatCompletionContext(
            BUFFER_SIZE, self.provider, initial_messages=history_to_llm_messages(history),
            initial_state=memory_state,
        )
        tool_names = {t.name for t in tools}
        agent = AssistantAgent(
            # AutoGen agent names must be valid Python identifiers — this
            # app's own agent names ("coding-agent", "research-agent", ...)
            # aren't, so they're sanitized here only; OrchestrationResult.agent
            # (returned to the API/UI) keeps the original, unsanitized name.
            agent_name.replace("-", "_"), model_client=model_client, tools=tools, model_context=model_context,
            system_message=_build_agent_mode_system_message(agent_name, tool_names),
            reflect_on_tool_use=True, max_tool_iterations=4,
        )
        task_message = build_user_message(prompt, images, source="user")
        token = CancellationToken()
        final_text = ""
        called: list[str] = []
        web_sources: list[dict] = []
        async for event in agent.on_messages_stream([task_message], token):
            if isinstance(event, ToolCallRequestEvent):
                for call in event.content:
                    called.append(call.name)
                    await _emit(on_event, {
                        "stage": "tool_call", "label": f"Calling tool {call.name}…",
                        "tool": call.name, "arguments": call.arguments,
                    })
            elif isinstance(event, ToolCallExecutionEvent):
                for result in event.content:
                    await _emit(on_event, {
                        "stage": "tool_result",
                        "label": f"{result.name} failed." if result.is_error else f"{result.name} → done.",
                        "tool": result.name, "is_error": result.is_error,
                    })
                    if result.name == "web_search" and not result.is_error:
                        parsed = _parse_web_search_result(result.content)
                        if parsed:
                            web_sources.extend(parsed)
                            await _emit(on_event, {
                                "stage": "web_sources", "label": f"{len(parsed)} web source(s) found.",
                                "sources": parsed,
                            })
            elif isinstance(event, Response):
                content = event.chat_message.content
                final_text = content if isinstance(content, str) else str(content)
        # A turn can call web_search more than once (the model reformulating
        # its query — routinely 2-3x in practice, verified live) with
        # overlapping results; dedupe by URL, first-seen order, so the UI
        # doesn't show the same citation repeated.
        seen_urls: set[str] = set()
        deduped_sources = []
        for source in web_sources:
            if source["url"] not in seen_urls:
                seen_urls.add(source["url"])
                deduped_sources.append(source)
        # Same buffered turns + summary the agent itself just used —
        # get_messages() re-runs compact_history() internally, but by now
        # model_context.state already accounts for all overflow (the stream
        # above already triggered that), so this re-call finds no NEW
        # overflow and makes no extra provider call; it's just reading back
        # what was actually sent. Rendered for "View context sent to model"
        # (static/app.js) — see render_memory_preview.
        sent_messages = await model_context.get_messages()
        memory_preview = render_memory_preview(llm_messages_to_plain(sent_messages))
        return final_text, called, deduped_sources, model_context.state, memory_preview


# --- Deck Builder: a Magentic-One-driven conversational deck-building flow ---
#
# Structurally distinct from AutoGenOrchestrator above (open-ended
# conversational loop, JSON-final-answer, no agent/skill/RAG routing), so
# this is a separate class, not a method on it. Exposes one narrow method —
# run_turn() — matching AgentOrchestrator's own narrow-contract discipline:
# app/services.py only ever calls this, never touches raw autogen_agentchat
# types directly (see .claude/rules/autogen-maf.md).

_DECK_BUILDER_MAX_TURNS = 8  # each chat turn launches a FRESH, bounded run — not one long-lived 20-turn conversation


def _fallback_deck_spec(task_brief: str) -> dict:
    """Deterministic, LLM-free spec builder — engaged whenever no real model
    client is available (mirrors app/skills.py:_fallback_spec's "every skill
    works end-to-end with zero credentials" guarantee, and
    _run_with_tools' model_client is None skip). Never raises; always
    produces a usable, if generic, single-slide deck from whatever brief
    text accumulated so far."""
    title = (task_brief.strip().splitlines()[0] if task_brief.strip() else "Untitled Presentation")[:80]
    return {
        "title": title, "subtitle": "", "theme": "midnight_executive",
        "slides": [{"layout": "bullets", "title": "Overview", "bullets": [task_brief.strip()[:300] or title]}],
    }


@dataclass
class DeckBuilderResult:
    # Exactly one of these is meaningful, depending on what the Magentic-One
    # run's final answer turned out to be this turn.
    clarifying_text: str | None = None   # a question/status to show the user; spec is None, phase stays "clarifying"
    spec: dict | None = None              # a parsed, ready-to-generate deck spec; clarifying_text is None
    used_fallback: bool = False           # True whenever _fallback_deck_spec ran (no model client, or unparseable JSON)
    provider: str | None = None
    model: str | None = None
    # Real web_search results this run actually read — same {"title", "url",
    # "content"} shape as OrchestrationResult.web_sources, so the UI renders
    # deck research with the identical citation panel it uses for agent turns.
    # These were previously emitted as progress events and then dropped on the
    # floor: the turn's meta hardcoded an empty list, so a deck built entirely
    # from live sources showed no sources at all.
    web_sources: list[dict] = field(default_factory=list)


class DeckBuilderOrchestrator:
    """Runs one Magentic-One turn for the Deck Builder flow. See
    app/services.py:DeckBuilderService for the session-state machinery
    (accumulating task_brief across turns, HITL hand-off) that calls this."""

    def __init__(self, skill):
        # `skill`: the pptx SkillPackage (app/skills.py) — its `instructions`
        # (SKILL.md body) is embedded in the system message so this flow's
        # Workflow/design guidance reaches the model even though it bypasses
        # SkillRunService/draft_spec entirely.
        self.skill = skill

    async def run_turn(self, task: str, context: dict, on_event: EventSink | None = None) -> DeckBuilderResult:
        """`task`: the accumulated task_brief (original request + prior
        clarifying Q&A) plus this turn's new user message, already merged by
        the caller.

        `context["web_search"]`: this turn's Web search toggle. Previously the
        Tavily tool was offered unconditionally here (`web_search=True`), which
        meant a deck request made live web calls whether or not the user had
        asked for that — the toggle was simply not reachable from this route.
        It now behaves the same way it does on an agent turn: offered when the
        user permits it, and the model still decides per-call whether to use
        it. Defaults True so a caller that doesn't pass one (tests, any future
        internal caller) keeps the previous behaviour."""
        model_client, provider_name, model_name = build_streaming_model_client(function_calling=True)
        if model_client is None:
            await _emit(on_event, {"stage": "drafting_spec", "label": "No model configured — using a basic deck."})
            return DeckBuilderResult(spec=_fallback_deck_spec(task), used_fallback=True)

        # Owned here, not inside _run_magentic_one, so research already done
        # survives a failure part-way through the run. That is not theoretical:
        # AutoGen raises RuntimeError("Reflect on tool use produced no valid
        # text response.") when the model returns empty content while
        # summarising tool results, which happens on exactly the turns that
        # searched the most. Losing the citations as well as the deck made a
        # turn that really did three web searches look like it did nothing.
        web_sources: list[dict] = []
        try:
            tools = await get_mcp_tools(web_search=context.get("web_search", True))
            agent = AssistantAgent(
                "deck_builder", model_client=model_client, tools=tools,
                system_message=_build_deck_builder_system_message(self.skill.instructions),
                reflect_on_tool_use=True,
            )
            team = MagenticOneGroupChat([agent], model_client=model_client, max_turns=_DECK_BUILDER_MAX_TURNS)
            final_text = await self._run_magentic_one(team, task, on_event, web_sources)
        except Exception as exc:  # noqa: BLE001 - a Magentic-One failure must degrade, never break the turn
            logger.warning("Deck Builder Magentic-One run failed, falling back to a basic deck: %s", exc)
            return DeckBuilderResult(
                spec=_fallback_deck_spec(task), used_fallback=True, web_sources=web_sources,
            )
        finally:
            await model_client.close()

        if not (final_text or "").strip():
            return DeckBuilderResult(spec=_fallback_deck_spec(task), used_fallback=True, web_sources=web_sources)

        spec = _extract_json(final_text)
        if spec is None or not isinstance(spec, dict) or not spec.get("title"):
            # Doesn't parse as a spec-shaped JSON object -> treat as a
            # clarifying question/status, not a failure. The caller keeps
            # the session in "clarifying" phase and shows this text as the
            # chat response.
            return DeckBuilderResult(
                clarifying_text=final_text.strip(), provider=provider_name, model=model_name,
                web_sources=web_sources,
            )

        return DeckBuilderResult(
            spec=spec, provider=provider_name, model=model_name, web_sources=web_sources,
        )

    async def _run_magentic_one(
        self, team: MagenticOneGroupChat, task: str, on_event: EventSink | None,
        web_sources: list[dict],
    ) -> str:
        """Consumes MagenticOneGroupChat.run_stream()'s event shape — DIFFERENT
        from AutoGenOrchestrator._run_with_tools' on_messages_stream/Response
        (MagenticOneGroupChat has no on_messages_stream; it's a BaseGroupChat,
        not a ChatAgent). The final event is a TaskResult, not a Response.
        Reuses the exact same ToolCallRequestEvent/ToolCallExecutionEvent
        classes _run_with_tools already consumes for the participant agent's
        own tool calls (these fold into the group's event stream unchanged),
        plus a new "planning" stage for the orchestrator's own plan/ledger
        TextMessages — there is no dedicated plan/progress-ledger event type
        in this AutoGen version, confirmed by inspecting the installed
        package: the orchestrator only ever emits plain TextMessage.

        Returns the final answer text, and appends every web source this run
        read to the caller's `web_sources` list — appended as they arrive, not
        returned at the end, so a run that raises part-way through still leaves
        the caller holding the research it already did (see run_turn).
        Deduped by URL, first-seen order, the same treatment _run_with_tools
        gives its own sources and for the same reason: a Magentic-One run
        routinely reformulates its query and re-searches, so raw results
        overlap heavily."""
        token = CancellationToken()
        final_text = ""
        seen_urls = {s["url"] for s in web_sources}
        async for event in team.run_stream(task=task, cancellation_token=token):
            if isinstance(event, ToolCallRequestEvent):
                for call in event.content:
                    await _emit(on_event, {
                        "stage": "researching", "label": f"Calling {call.name}…",
                        "tool": call.name, "arguments": call.arguments,
                    })
            elif isinstance(event, ToolCallExecutionEvent):
                for result in event.content:
                    await _emit(on_event, {
                        "stage": "researching",
                        "label": f"{result.name} failed." if result.is_error else f"{result.name} → done.",
                        "tool": result.name, "is_error": result.is_error,
                    })
                    if result.name == "web_search" and not result.is_error:
                        parsed = _parse_web_search_result(result.content)
                        if parsed:
                            await _emit(on_event, {
                                "stage": "web_sources", "label": f"{len(parsed)} web source(s) found.",
                                "sources": parsed,
                            })
                            for source in parsed:
                                if source["url"] not in seen_urls:
                                    seen_urls.add(source["url"])
                                    web_sources.append(source)
            elif isinstance(event, TextMessage) and event.source == "MagenticOneOrchestrator":
                await _emit(on_event, {"stage": "planning", "label": str(event.content)[:200]})
            elif isinstance(event, TaskResult):
                final_text = event.messages[-1].content if event.messages else ""
                if not isinstance(final_text, str):
                    final_text = str(final_text)
        return final_text
