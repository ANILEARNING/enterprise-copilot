"""Context management: a bounded "buffered chat completion context" (the
last `BUFFER_SIZE` raw turns, verbatim) plus a running *compact* summary of
everything older, so a long session degrades gracefully — turn 6 and
earlier don't just silently vanish from the model's view with zero trace,
they get folded into a short summary instead. Two forms sharing one
summarization helper:

- `compact_history()` — plain dict-in/dict-out, used by every call site that
  talks to `AIProvider.complete()` directly (app/services.py's direct-chat
  branch, AutoGenOrchestrator's plain-completion fallback in app/agents.py).
  No AutoGen import at all.
- `CompactingChatCompletionContext` — a real
  `autogen_core.model_context.BufferedChatCompletionContext` subclass, used
  everywhere this app builds an actual `AssistantAgent` (app/streaming.py's
  direct-chat path, AutoGenOrchestrator's MCP tool-calling path). Delegates
  its own compaction decision to `compact_history()` so the summarization
  prompt/logic exists in exactly one place.

State — the running summary text and how many of the oldest turns have
already been folded into it — persists across process restarts via
`SessionStore.set_field(session_id, "memory_state", ...)` (app/storage.py is
already file-backed; see CompactMemoryState.to_dict/from_dict and
`ChatCompletionContext.save_state()/load_state()`, whose exact naming this
mirrors on purpose — "session memory that survives beyond the current
execution, via the state management").

`autogen_core.model_context`/`autogen_core.models` are framework-agnostic
building blocks already used directly outside app/agents.py's AutoGen
boundary (see app/routes.py's CancellationToken import) — not the
`autogen_agentchat` orchestration types `.claude/rules/autogen-maf.md`
actually restricts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from autogen_core.model_context import BufferedChatCompletionContext
from autogen_core.models import AssistantMessage, LLMMessage, SystemMessage, UserMessage

logger = logging.getLogger(__name__)

BUFFER_SIZE = 5
# A summarization prompt built from unlimited prior turns would itself blow
# past any provider's token budget eventually — bounded the same way every
# other prompt-building call site in this app already is (see app/skills.py,
# app/observability.py's _trim).
MAX_EXCERPT_CHARS = 6000


@dataclass
class CompactMemoryState:
    """summary: the running compact summary of every turn older than the
    buffer. summarized_count: how many of the session's oldest turns are
    already folded into it — so a later call only summarizes the NEW
    overflow, not the whole history again."""
    summary: str = ""
    summarized_count: int = 0

    def to_dict(self) -> dict:
        return {"summary": self.summary, "summarized_count": self.summarized_count}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "CompactMemoryState":
        if not data:
            return cls()
        return cls(summary=str(data.get("summary", "")), summarized_count=int(data.get("summarized_count", 0)))


async def _extend_summary(provider, state: CompactMemoryState, new_overflow: list[dict]) -> CompactMemoryState:
    excerpt = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in new_overflow)
    excerpt = excerpt[:MAX_EXCERPT_CHARS]
    prompt = (
        "Summarize the following older conversation excerpt in 2-4 concise sentences. "
        "Preserve concrete facts, names, numbers and decisions; drop small talk and filler.\n\n"
        + (f"Fold it into this existing summary rather than starting over from scratch:\n{state.summary}\n\n"
           if state.summary else "")
        + f"Conversation excerpt:\n{excerpt}"
    )
    try:
        result = await provider.complete(prompt, [])
    except Exception as exc:  # noqa: BLE001 - a summarization failure must degrade, never break the turn it's for
        logger.warning("Compact-memory summarization failed, keeping the previous summary: %s", exc)
        return state
    summary = result.text.strip()
    if not summary:
        return state
    return CompactMemoryState(summary=summary, summarized_count=state.summarized_count + len(new_overflow))


def render_memory_preview(context_for_the_prompt: list[dict]) -> str | None:
    """Renders `compact_history()`'s `context_for_the_prompt` (the synthetic
    summary turn, if any, plus the buffered recent turns) as a flat,
    human-readable transcript — the same shape "View context sent to model"
    (static/app.js contextDisclosureHtml) already shows for the system
    prompt and grounded prompt, just for the *memory* piece: what prior-turn
    context the model actually received alongside this turn's prompt.

    None when there's nothing to show (a session's very first turn, or one
    with fewer than BUFFER_SIZE prior turns and no summary yet) — the UI
    already treats an absent preview as "nothing to disclose" for the other
    context-preview fields, same convention here."""
    if not context_for_the_prompt:
        return None
    lines = [f"{turn.get('role', 'user')}: {turn.get('content', '')}" for turn in context_for_the_prompt]
    return "\n\n".join(lines)


async def compact_history(
    provider, history: list[dict], state: CompactMemoryState, buffer_size: int = BUFFER_SIZE,
) -> tuple[list[dict], CompactMemoryState]:
    """`history` is the FULL session transcript, oldest first (no
    pre-truncation needed — that's what this function is for). Returns
    (context_for_the_prompt, updated_state):

    - context_for_the_prompt: at most `buffer_size` raw recent turns, with
      one synthetic leading `{"role": "system", ...}` turn carrying the
      running summary prepended whenever one exists.
    - updated_state: unchanged if nothing new fell outside the buffer this
      call, otherwise the summary extended (one provider.complete() call) to
      cover the newly-overflowed turns.

    Never raises — a summarization failure just keeps the previous summary
    and still returns the raw buffered window; context management degrades
    gracefully, same posture as every AIProvider call in this app.
    """
    overflow = history[: max(0, len(history) - buffer_size)]
    new_overflow = overflow[state.summarized_count:]
    if new_overflow:
        state = await _extend_summary(provider, state, new_overflow)
    recent = history[-buffer_size:] if buffer_size > 0 else []
    context = list(recent)
    if state.summary:
        context = [{"role": "system", "content": f"Summary of earlier conversation: {state.summary}"}] + context
    return context, state


# --- the AutoGen-native flavor: a real BufferedChatCompletionContext ---------

def history_to_llm_messages(history: list[dict]) -> list[LLMMessage]:
    """Plain {"role", "content"} dicts (SessionStore's on-disk shape) ->
    autogen_core.models types, for seeding CompactingChatCompletionContext's
    initial_messages."""
    messages: list[LLMMessage] = []
    for h in history:
        content = h.get("content", "")
        if h.get("role") == "user":
            messages.append(UserMessage(content=content, source="user"))
        else:
            messages.append(AssistantMessage(content=content, source="assistant"))
    return messages


def llm_messages_to_plain(messages: list[LLMMessage]) -> list[dict]:
    plain: list[dict] = []
    for m in messages:
        if isinstance(m, UserMessage):
            role = "user"
        elif isinstance(m, AssistantMessage):
            role = "assistant"
        elif isinstance(m, SystemMessage):
            role = "system"
        else:
            continue  # function-call/tool-result messages aren't meaningful to summarize
        content = m.content if isinstance(m.content, str) else str(m.content)
        plain.append({"role": role, "content": content})
    return plain


class CompactingChatCompletionContext(BufferedChatCompletionContext):
    """A real `BufferedChatCompletionContext(buffer_size=5)` — every message
    ever added stays in `self._messages` (inherited), so this is a genuine
    AutoGen buffered context, not a lookalike — plus a running compact
    summary of whatever falls outside that window, computed via the exact
    same `compact_history()` used by the non-AutoGen call sites.

    Used as `AssistantAgent(model_context=...)`: seed with this session's
    prior turns as `initial_messages` (and prior state via `load_state()`),
    let the agent's own `on_messages_stream`/team loop add the new turn(s),
    then persist `await context.save_state()` back to the session (see
    CopilotService) so a restart resumes the same compact memory, not a
    blank one.
    """

    def __init__(
        self, buffer_size: int, provider, initial_messages: list[LLMMessage] | None = None,
        initial_state: CompactMemoryState | None = None,
    ):
        super().__init__(buffer_size, initial_messages)
        self._provider = provider
        self._state = initial_state or CompactMemoryState()

    @property
    def state(self) -> CompactMemoryState:
        return self._state

    async def get_messages(self) -> list[LLMMessage]:
        plain_history = llm_messages_to_plain(self._messages)
        _, self._state = await compact_history(self._provider, plain_history, self._state, self._buffer_size)
        recent = await super().get_messages()  # last buffer_size raw messages, real AutoGen types, untouched
        if not self._state.summary:
            return recent
        return [SystemMessage(content=f"Summary of earlier conversation: {self._state.summary}"), *recent]

    async def save_state(self) -> Mapping[str, Any]:
        base = dict(await super().save_state())
        base["compact_memory"] = self._state.to_dict()
        return base

    async def load_state(self, state: Mapping[str, Any]) -> None:
        await super().load_state(state)
        self._state = CompactMemoryState.from_dict(state.get("compact_memory"))
