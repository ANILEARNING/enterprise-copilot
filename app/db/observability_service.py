"""Composition layer between CopilotService's chat turn (app/services.py)
and the observability/cost repository layer (app/db/observability_repository.py)
— same role app/tenancy.py plays for login: the repositories have no
opinion on what a "chat turn" is, and CopilotService.chat/chat_stream
shouldn't need to know DB session management to record one. Every method
here is a thin, best-effort wrapper: a write failure here must never break
or delay the chat turn it's describing, same posture as
app/observability.py's Tracer (Langfuse) — see this module's `_safe`
wrapper.

Unlike app/tenancy.py, there is no logged-in-user context on a plain chat
turn today (chat doesn't require login — see app/tenancy.py's own module
docstring on this being a deliberate, separate decision). Every write here
still needs a valid tenant_id (guardrail_events/traces/model_calls.tenant_id
is a NOT NULL FK to tenants.id), so this module ensures and caches one
well-known "default" Tenant row — see ensure_default_tenant_id — that every
unauthenticated turn's observability rows are scoped to, until routes
require login and a real tenant_id flows through from there instead."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .observability_repository import GuardrailEventRepository, ModelCallRepository, TraceRepository
from .tenancy_repository import TenantRepository

logger = logging.getLogger(__name__)

DEFAULT_TENANT_SLUG = "default"
DEFAULT_TENANT_NAME = "Default"


class ObservabilityService:
    """Owns a DB session factory and the three observability/cost
    repositories, offering CopilotService a handful of `record_*` methods
    instead of requiring it to open sessions and call repositories
    directly. `session_factory=None` (e.g. DATABASE_URL unset — see
    CopilotService.__init__) makes every method here a no-op, same
    graceful-degrade posture as app/observability.py's Tracer when Langfuse
    isn't configured — this is genuinely optional infrastructure, not a
    hard dependency of chat working at all."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None):
        self._session_factory = session_factory
        self._guardrail_events = GuardrailEventRepository()
        self._traces = TraceRepository()
        self._model_calls = ModelCallRepository()
        self._tenants = TenantRepository()
        self._default_tenant_id: uuid.UUID | None = None

    @property
    def enabled(self) -> bool:
        return self._session_factory is not None

    async def _ensure_default_tenant_id(self, session: AsyncSession) -> uuid.UUID:
        """Returns the well-known "default" tenant's id, creating the row
        on first call in this process and caching it after — see module
        docstring. A second process racing to create it (verified live: two
        concurrent first-calls) would hit tenants.slug's UNIQUE constraint
        on the loser's insert; that's handled by re-querying on conflict
        rather than propagating the error, since "the row now exists" is
        exactly the outcome either caller wanted."""
        if self._default_tenant_id is not None:
            return self._default_tenant_id
        existing = await self._tenants.get_by_slug(session, DEFAULT_TENANT_SLUG)
        if existing is not None:
            self._default_tenant_id = existing.id
            return existing.id
        try:
            tenant = await self._tenants.create(session, slug=DEFAULT_TENANT_SLUG, name=DEFAULT_TENANT_NAME)
            await session.commit()
            self._default_tenant_id = tenant.id
            return tenant.id
        except Exception:  # noqa: BLE001 - a concurrent creator won the race; re-read what they wrote
            await session.rollback()
            existing = await self._tenants.get_by_slug(session, DEFAULT_TENANT_SLUG)
            if existing is None:
                raise  # genuinely not a race — something else is wrong, let it surface
            self._default_tenant_id = existing.id
            return existing.id

    async def record_guardrail_check(
        self, *, stage: str, findings: dict, session_id: str | None = None, turn_id: str | None = None,
    ) -> None:
        """Persists one GuardrailService.check_input/check_output/
        check_context result. `findings` is that method's own returned
        dict, passed straight through — matches guardrail_events.findings
        storing it verbatim (see GuardrailEventRow's docstring,
        app/db/models.py). `redacted_text` is stripped before storage here
        (not the repository's job) since it's the one field in that dict
        that duplicates message content already persisted elsewhere."""
        if not self.enabled:
            return
        try:
            async with self._session_factory() as session:
                tenant_id = await self._ensure_default_tenant_id(session)
                sanitized = {k: v for k, v in findings.items() if k != "redacted_text"}
                await self._guardrail_events.record(
                    session, tenant_id=tenant_id, stage=stage, allowed=findings["allowed"], findings=sanitized,
                    session_id=_maybe_uuid(session_id), turn_id=turn_id,
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - never break the chat turn this is describing
            logger.warning("Could not persist guardrail event (stage=%s): %s", stage, exc)

    async def record_turn(
        self, *, turn_id: str, session_id: str | None, route: str | None, status: str,
        started_at: datetime, ended_at: datetime, error: str | None = None,
        model_calls: list[dict] | None = None,
    ) -> None:
        """Persists one completed chat turn's trace, plus every model call
        made during it, in one transaction — called once at the end of
        CopilotService.chat/chat_stream rather than incrementally through
        the turn (unlike Tracer/Langfuse's live span, there is no partial-
        turn visibility requirement for the DB copy; a turn that never
        finishes — process crash mid-turn — simply never gets a trace row,
        same as it never got a Langfuse span closed either).

        `model_calls`: each dict shaped like ModelCallRepository.record's
        kwargs (call_type/provider/model/tokens/cost_micros/latency_ms/
        error), minus tenant_id/trace_id which this method fills in."""
        if not self.enabled:
            return
        try:
            async with self._session_factory() as session:
                tenant_id = await self._ensure_default_tenant_id(session)
                trace = await self._traces.start_trace(
                    session, tenant_id=tenant_id, turn_id=turn_id, session_id=_maybe_uuid(session_id), route=route,
                )
                await self._traces.finish_trace(
                    session, trace.id, status=status, error=error, ended_at=ended_at,
                    duration_ms=int((ended_at - started_at).total_seconds() * 1000),
                )
                for call in (model_calls or []):
                    await self._model_calls.record(
                        session, tenant_id=tenant_id, trace_id=trace.id, session_id=_maybe_uuid(session_id), **call,
                    )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - never break the chat turn this is describing
            logger.warning("Could not persist trace for turn %s: %s", turn_id, exc)


def _maybe_uuid(value: str | None) -> uuid.UUID | None:
    """session_id is a plain string everywhere in this app (SessionStore's
    own id, see app/session_store.py) but a UUID column here — best-effort
    parse, None (not an error) for anything that isn't a valid UUID, since
    a malformed/legacy session_id must never break an observability write."""
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
