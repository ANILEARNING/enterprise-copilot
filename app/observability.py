"""Optional Langfuse observability: one trace per chat turn, with a nested
span/event per genuinely-happening sub-step and a nested "generation" per
completed model call — reusing the exact same progress-event dicts already
threaded through CopilotService.chat()'s on_event callback (see
app/agents.py, app/skills.py, app/mcp_tools.py) as the single source of
truth for "what actually happened this turn." This is one integration
point, not Langfuse-specific code scattered across every layer.

No-op unless both LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are configured
(see app/config.py) — never required for the app to run, same
graceful-degrade posture as every AIProvider/EmbeddingProvider in
app/providers.py. This is also the only file that imports `langfuse`.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from .config import settings

logger = logging.getLogger(__name__)

# Trace/event payloads (prompts, retrieved snippets) can be long; Langfuse is
# built for this, but there's no reason to ship an unbounded blob for a
# pathological giant message.
_MAX_FIELD_CHARS = 4000


def _trim(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS] + "…"
    return value


class _NullSpan:
    """The no-op end of TurnHandle — every method is a safe do-nothing, so
    call sites never need an `if tracer.enabled` guard of their own."""

    def update(self, **_kwargs: Any) -> None:
        pass

    def create_event(self, **_kwargs: Any) -> None:
        pass

    def start_observation(self, **_kwargs: Any) -> "_NullSpan":
        return self

    def end(self, **_kwargs: Any) -> None:
        pass


class TurnHandle:
    """Wraps one Langfuse span (the current chat turn) — or nothing, when
    tracing is disabled/unavailable. Every method swallows its own
    exceptions: a tracing hiccup must never affect the chat turn it's
    describing."""

    def __init__(self, span: Any):
        self._span = span

    def event(self, name: str, **fields: Any) -> None:
        try:
            self._span.create_event(name=name, metadata={k: _trim(v) for k, v in fields.items()})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse event log failed: %s", exc)

    def generation(self, name: str, *, model: str | None, provider: str | None,
                    input: Any = None, output: Any = None, metadata: dict | None = None) -> None:
        """Logs one already-completed model call as a Langfuse generation
        observation — called right after an AIProvider.complete() call
        returns (direct chat, agent-mode, skill drafting), never wrapping
        the call itself, so a tracing failure can never delay or break it."""
        try:
            obs = self._span.start_observation(
                name=name, as_type="generation", model=model,
                input=_trim(input), output=_trim(output),
                metadata={"provider": provider, **{k: _trim(v) for k, v in (metadata or {}).items()}},
            )
            obs.end()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse generation log failed: %s", exc)

    def set_output(self, output: Any, metadata: dict | None = None) -> None:
        try:
            self._span.update(output=_trim(output), metadata={k: _trim(v) for k, v in (metadata or {}).items()})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse set_output failed: %s", exc)


class Tracer:
    """The only object anything outside this file talks to. `enabled` is
    False (and every operation below a safe no-op) whenever Langfuse isn't
    configured or its client fails to initialize."""

    def __init__(self):
        self.enabled = bool(settings.langfuse_public_key and settings.langfuse_secret_key)
        self._client = None
        if not self.enabled:
            return
        try:
            from langfuse import Langfuse
            self._client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                base_url=settings.langfuse_base_url,
            )
        except Exception as exc:  # noqa: BLE001 - observability must never break app startup
            logger.warning("Langfuse client init failed, tracing disabled: %s", exc)
            self.enabled = False
            self._client = None

    @asynccontextmanager
    async def turn(self, name: str, *, input: Any, metadata: dict | None = None) -> AsyncIterator[TurnHandle]:
        """One span per chat turn (CopilotService.chat / chat_stream) —
        everything logged via the yielded TurnHandle nests under it."""
        if not self.enabled or self._client is None:
            yield TurnHandle(_NullSpan())
            return
        try:
            # Sync context manager (OTel span start/stop is synchronous
            # bookkeeping, not I/O) used from inside this async generator —
            # `async with` isn't supported here, `with` is.
            span_cm = self._client.start_as_current_observation(
                name=name, as_type="span", input=_trim(input),
                metadata={k: _trim(v) for k, v in (metadata or {}).items()},
            )
        except Exception as exc:  # noqa: BLE001 - a trace-setup failure must never break the turn it's tracing
            # Genuinely a Langfuse/OTel-side failure (span creation itself
            # raised, before the turn body ever ran) — degrade to untraced
            # and let the turn proceed normally.
            logger.warning("Langfuse span failed, continuing untraced: %s", exc)
            yield TurnHandle(_NullSpan())
            return
        # Deliberately NOT wrapped in a try/except: whatever the turn body
        # raises here must propagate to the caller completely unchanged. An
        # @asynccontextmanager generator that catches an exception thrown
        # into it at `yield` and then yields again violates PEP 342 (a
        # generator may not yield after receiving athrow()) — the caller's
        # `async with tracer.turn(...)` would raise "generator didn't stop
        # after athrow()" instead of the turn's real exception (e.g. a
        # genuine RAG/provider failure), masking it behind an unrelated
        # RuntimeError. Only span *setup* (above) gets the degrade-gracefully
        # treatment; a body failure is the caller's own business, not a
        # tracing concern.
        with span_cm as span:
            yield TurnHandle(span)


tracer = Tracer()


def describe_observability() -> dict:
    """Status for the UI (Settings tab) — never includes the secret key."""
    return {"enabled": tracer.enabled, "host": settings.langfuse_base_url if tracer.enabled else None}
