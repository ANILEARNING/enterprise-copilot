"""Real-time streaming chat: an actual AutoGen AssistantAgent, in a minimal
one-agent team, with model_client_stream=True, emit_team_events=True, a
termination condition capped at a couple of turns, and a CancellationToken
that genuinely aborts the in-flight model call (verified: it interrupts the
underlying HTTPS connection mid-read, not just a polite stop-asking-for-more-
tokens).

This is the one place that resolves a real AutoGen model client from
settings (build_streaming_model_client, reused by AutoGenOrchestrator's
MCP tool-calling path — see app/agents.py) and builds the plain single-agent
team used for token-by-token direct chat. AutoGenOrchestrator (app/agents.py)
is where an actual AssistantAgent/tool-calling loop runs; both stay behind
the AgentOrchestrator/AutoGenOrchestrator boundary (.claude/rules/autogen-maf.md)
— nothing outside app/agents.py and app/streaming.py imports autogen_agentchat.
This module in particular is optional runtime infrastructure behind one
streaming endpoint — if it's unavailable or fails, CopilotService.chat_stream()
falls back to the existing, fully-tested non-streaming chat() path (see
app/services.py). Nothing elsewhere in the app depends on this module succeeding.

Scope: powers only the router's "direct" route (see app/agents.py:plan_turn
— no augmentation needed, no skill match, no RAG grounding) — the one case
that's genuinely "send a prompt, stream the answer." "agent"/skill/deck
turns keep using the existing orchestrator and are delivered over the same
SSE envelope as a single non-streamed chunk, so the frontend (and the stop
button) behave uniformly regardless of which path served a given turn.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import ModelClientStreamingChunkEvent, MultiModalMessage, TextMessage
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core import CancellationToken, Image

from .config import settings
from .memory import (
    BUFFER_SIZE, CompactingChatCompletionContext, CompactMemoryState,
    history_to_llm_messages, llm_messages_to_plain, render_memory_preview,
)
from .providers import _azure_deployment_names

logger = logging.getLogger(__name__)

# "one or two loops for minimal token consumption" — counts the task message
# itself plus replies, so 2 permits exactly one agent turn before stopping.
MAX_TEAM_MESSAGES = 2

SYSTEM_MESSAGE = (
    "You are Enterprise Copilot, a helpful, concise assistant. Answer directly; "
    "don't pad responses with unnecessary preamble."
)


def build_user_message(text: str, images: list[dict] | None, source: str = "user"):
    """TextMessage for a plain turn, MultiModalMessage when images are
    attached (see app/models.py:ImageAttachment) — the one place both this
    module and AutoGenOrchestrator (app/agents.py, which imports this) build
    the final task message, so multimodal construction isn't duplicated."""
    if not images:
        return TextMessage(content=text, source=source)
    content: list = [text]
    for image in images:
        content.append(Image.from_base64(image["data"]))
    return MultiModalMessage(content=content, source=source)


def _model_info(function_calling: bool, vision: bool = False) -> dict:
    # Neither Gemini nor Ollama is a model autogen-ext recognizes by name, so
    # this must be supplied explicitly rather than looked up. function_calling
    # must be True for AssistantAgent to attach tools at all (see
    # AutoGenOrchestrator._run_with_tools, app/agents.py) — a model that's
    # actually incapable of it then just fails the request instead, caught
    # there and degraded to a plain completion, same as any other
    # tool-calling failure. vision must be True to send a MultiModalMessage
    # at all (build_user_message above) — same fail-and-degrade posture if
    # the actual configured model can't see images.
    return {
        "vision": vision, "function_calling": function_calling, "json_output": False,
        "family": "unknown", "structured_output": False,
    }


class ModelUnavailableError(ValueError):
    """An explicitly requested provider/model can't be used at all in this
    environment (not configured) — distinct from that model being reachable
    but failing the request (a real API error, surfaced via describe_model_error
    in app/providers.py instead)."""


def build_streaming_model_client(
    provider: str | None = None, model: str | None = None, *, strict: bool = False,
    function_calling: bool = False, vision: bool = False,
):
    """Returns (client, resolved_provider, resolved_model) pointed at whichever
    provider is configured (Gemini/Ollama both expose an OpenAI-compatible
    endpoint) — or at `provider`/`model` explicitly, when the user picked a
    specific model in the Copilot UI (see CopilotService.chat_stream).
    `client` is None (with resolved_provider/model also None) exactly when
    streaming can't proceed — see strict below for how that's signaled.

    `function_calling`: pass True when the caller intends to attach tools
    (AutoGenOrchestrator's MCP tool-calling path) — AssistantAgent refuses to
    attach any tool to a client whose model_info claims it can't call
    functions. Plain direct-chat streaming (no tools) leaves this False.

    `vision`: pass True when the caller intends to send a MultiModalMessage
    (an image attached to this turn — see build_user_message above).

    strict=False (the automatic path, no explicit user choice): returns
    (None, None, None) when streaming isn't available (mock mode, no
    credentials) so the caller falls back to the non-streaming path silently.
    strict=True (an explicit user choice): raises ModelUnavailableError with a
    human-readable reason instead of returning None — an explicit choice that
    can't even be attempted should say why, not silently substitute something else.
    """
    provider = provider or settings.model_provider
    if not strict and settings.ai_mode != "configured":
        return None, None, None
    try:
        from autogen_ext.models.openai import OpenAIChatCompletionClient
    except ImportError:
        if strict:
            raise ModelUnavailableError("Streaming isn't available in this environment (autogen-ext[openai] not installed).")
        logger.warning("autogen-ext[openai] not installed; real streaming chat is unavailable.")
        return None, None, None

    if provider == "gemini":
        if not settings.gemini_api_key:
            if strict:
                raise ModelUnavailableError("Gemini isn't configured in this environment (no GEMINI_API_KEY).")
            return None, None, None
        resolved_model = model or "gemini-flash-latest"
        client = OpenAIChatCompletionClient(
            model=resolved_model,
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            model_info=_model_info(function_calling, vision),
        )
        return client, "gemini", resolved_model
    if provider == "ollama":
        resolved_model = model or settings.ollama_model
        if not resolved_model:
            if strict:
                raise ModelUnavailableError("No Ollama model specified.")
            return None, None, None
        client = OpenAIChatCompletionClient(
            model=resolved_model,
            # The OpenAI client requires a non-empty key even against a local,
            # unauthenticated daemon; Ollama ignores it in that case.
            api_key=settings.ollama_api_key or "ollama-local",
            base_url=f"{settings.ollama_base_url.rstrip('/')}/v1",
            model_info=_model_info(function_calling, vision),
        )
        return client, "ollama", resolved_model
    if provider == "azure":
        if not (settings.azure_ai_endpoint and settings.azure_ai_api_key and settings.azure_ai_deployment):
            if strict:
                raise ModelUnavailableError(
                    "Azure AI Foundry isn't configured in this environment "
                    "(need AZURE_AI_ENDPOINT, AZURE_AI_API_KEY and AZURE_AI_DEPLOYMENT)."
                )
            return None, None, None
        try:
            from autogen_ext.models.openai import AzureOpenAIChatCompletionClient
        except ImportError:
            if strict:
                raise ModelUnavailableError("Streaming isn't available in this environment (autogen-ext[openai] not installed).")
            logger.warning("autogen-ext[openai] not installed; real streaming chat is unavailable.")
            return None, None, None
        # `model` is the picker's chosen deployment name — honored if it's
        # one of this resource's known deployments (see
        # _azure_deployment_names, app/providers.py), otherwise the
        # configured default, same reasoning as build_provider_for's Azure
        # branch there.
        resolved_model = model if model in _azure_deployment_names() else settings.azure_ai_deployment
        client = AzureOpenAIChatCompletionClient(
            model=resolved_model, azure_deployment=resolved_model,
            azure_endpoint=settings.azure_ai_endpoint, api_version=settings.azure_ai_api_version,
            api_key=settings.azure_ai_api_key,
            model_info=_model_info(function_calling, vision),
        )
        return client, "azure", resolved_model
    if strict:
        raise ModelUnavailableError(f"{provider!r} doesn't support streaming chat.")
    return None, None, None


async def stream_chat(
    message: str, history: list[dict], cancellation_token: CancellationToken,
    *, provider: str | None = None, model: str | None = None, strict: bool = False,
    images: list[dict] | None = None, memory_provider=None, memory_state: dict | None = None,
) -> AsyncIterator[dict]:
    """Yields SSE-payload dicts: {"type": "model_info", "provider": ..., "model": ...}
    (once, as soon as the real provider/model is resolved — this is the actual
    identity of what's about to run, not a guess), {"type": "status", "stage":
    "thinking", ...} (once, before the first token — the model may take a
    moment to respond), {"type": "delta", "text": ...},
    {"type": "team_event", "event_type": ..., "source": ...}, {"type":
    "memory_state", "state": {...}} (once, right before the turn ends — see
    below), {"type": "cancelled"}, or {"type": "error", "message": ...}.
    Never raises — a model-client/network failure becomes an "error" event so
    the caller can degrade gracefully instead of the request crashing.

    provider/model/strict: an explicit user model choice (see
    CopilotService.chat_stream) — strict=True means a missing/unreachable
    model raises/reports instead of silently falling back.

    images: optional multimodal attachments for this turn (see
    app/models.py:ImageAttachment) — sent as a MultiModalMessage instead of
    plain text (build_user_message).

    memory_provider/memory_state: context management (buffered chat
    completion context, buffer size app.memory.BUFFER_SIZE, plus a compact
    summary of anything older — see app/memory.py). `history` seeds the
    agent's own CompactingChatCompletionContext via initial_messages (the
    full transcript, not pre-windowed — the context does its own
    buffering/summarizing) rather than being replayed as extra task
    messages, so a long session doesn't flood this turn's team_events with
    every prior turn. memory_provider is the app's own AIProvider (used only
    for the context's internal summarization calls, never for the actual
    answer — that's still the real streaming model_client below); None
    skips compaction (the context still buffers to BUFFER_SIZE, it just
    never summarizes the overflow). The updated state is emitted as one
    "memory_state" event for the caller (CopilotService.chat_stream) to
    persist via SessionStore — see CompactMemoryState.
    """
    from .providers import describe_model_error  # local import: providers.py doesn't import streaming.py

    try:
        model_client, resolved_provider, resolved_model = build_streaming_model_client(
            provider, model, strict=strict, vision=bool(images),
        )
    except ModelUnavailableError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    if model_client is None:
        yield {"type": "error", "message": "Streaming chat is not configured in this environment."}
        return

    # The real, resolved identity of what's about to answer — replaces the
    # generic "autogen-stream" placeholder the caller previously had to guess
    # a label from.
    yield {"type": "model_info", "provider": resolved_provider, "model": resolved_model}

    context = CompactingChatCompletionContext(
        BUFFER_SIZE, memory_provider, initial_messages=history_to_llm_messages(history),
        initial_state=CompactMemoryState.from_dict(memory_state),
    )
    # Built early (before the agent/team below) specifically so this turn's
    # buffered-recent-turns + summary can be previewed up front, alongside
    # the system/prompt preview — get_messages() runs compact_history()
    # itself (one provider call only if new turns just overflowed the
    # buffer); the agent's own internal get_messages() call later in this
    # same turn finds nothing new to summarize and makes no second call.
    memory_preview = render_memory_preview(llm_messages_to_plain(await context.get_messages()))

    # The exact system + user message about to be sent — known up front here
    # (unlike AutoGenOrchestrator's tool-calling/plain-completion paths,
    # which only know prompt_preview once a response comes back), so this
    # fires before the first token rather than after. Powers the UI's "View
    # context sent to model" disclosure (see static/app.js
    # contextDisclosureHtml) — without this, direct chat (the router's
    # "direct" route) never had a system_preview/prompt_preview at all.
    yield {
        "type": "status", "stage": "model_call_started",
        "system_preview": SYSTEM_MESSAGE, "prompt_preview": message[:2000],
        "memory_preview": memory_preview,
    }
    yield {"type": "status", "stage": "thinking", "label": "Thinking…"}
    try:
        agent = AssistantAgent(
            "copilot", model_client=model_client, model_client_stream=True,
            system_message=SYSTEM_MESSAGE, model_context=context,
        )
        team = RoundRobinGroupChat(
            [agent],
            termination_condition=MaxMessageTermination(MAX_TEAM_MESSAGES),
            emit_team_events=True,
        )

        task_message = build_user_message(message, images, source="user")

        async for event in team.run_stream(task=[task_message], cancellation_token=cancellation_token):
            if isinstance(event, ModelClientStreamingChunkEvent):
                yield {"type": "delta", "text": event.content}
            elif isinstance(event, TaskResult):
                continue  # terminal event; the caller already has the full text from the deltas
            else:
                # team-level events (emit_team_events=True) and the non-streamed
                # echo of the final TextMessage — surfaced for transparency, not
                # rendered as chat content.
                yield {"type": "team_event", "event_type": type(event).__name__,
                       "source": getattr(event, "source", None)}
        yield {"type": "memory_state", "state": context.state.to_dict()}
    except asyncio.CancelledError:
        # AutoGen's own cancellation checking raises this (verified: it aborts the
        # in-flight HTTPS call, not just a "stop asking for more tokens" flag) —
        # caught here, not re-raised, so the caller (CopilotService.chat_stream)
        # gets a normal "cancelled" event and can still do its own bounded
        # cleanup (persist the partial turn, send a proper "done") instead of
        # cancellation continuing to unwind as an exception through the stack.
        yield {"type": "cancelled"}
    except Exception as exc:  # noqa: BLE001 - degrade to an error event, never crash the request
        logger.warning("Streaming chat failed: %s", exc)
        yield {"type": "error", "message": describe_model_error(exc)}
    finally:
        try:
            await model_client.close()
        except Exception:  # noqa: BLE001 - closing the client must never mask the real outcome above
            pass
