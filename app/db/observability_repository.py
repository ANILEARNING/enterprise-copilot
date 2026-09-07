"""Repository layer for the observability/cost tables (app/db/models.py:
GuardrailEventRow, TraceRow, TraceEventRow, ModelCallRow, ModelPricingRow,
UsageDailyRollupRow). Nothing in this app writes to these yet —
GuardrailService (app/services.py), Tracer (app/observability.py), and every
AIProvider/EmbeddingProvider (app/providers.py) are the eventual callers,
not built in this pass. This file establishes the query surface they'll
need, ahead of them, same sequencing as app/db/tenancy_repository.py before
app/tenancy.py was built on top of it.

Same conventions as that file: Core-style (explicit select/insert/update,
no ORM relationship traversal), every method takes an AsyncSession as its
first argument so the caller controls the transaction boundary, and every
tenant-scoped table's queries carry an explicit tenant_id filter (see
app/db/models.py's module docstring).

Grouped into 4 repository classes by aggregate, not 1-per-table:
GuardrailEventRepository stands alone (guardrail_events has no natural
pairing with anything else here); TraceRepository owns both traces and
trace_events together, since a trace_event is never queried independent of
its parent trace; ModelCallRepository and ModelPricingRepository are
separate because a call's cost is computed once at write time from
whatever pricing was current then (see ModelPricingRow's own docstring) —
after that, the two tables don't interact; UsageRollupRepository is its own
class since it's a derived cache over model_calls, not a peer of it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import GuardrailEventRow, ModelCallRow, ModelPricingRow, TraceEventRow, TraceRow, UsageDailyRollupRow


class GuardrailEventRepository:
    """See GuardrailEventRow's docstring in app/db/models.py — findings are
    stored verbatim as GuardrailService already shapes them, never
    normalized into columns."""

    async def record(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, stage: str, allowed: bool, findings: dict,
        session_id: uuid.UUID | None = None, message_id: uuid.UUID | None = None, turn_id: str | None = None,
    ) -> GuardrailEventRow:
        row = GuardrailEventRow(
            tenant_id=tenant_id, session_id=session_id, message_id=message_id, turn_id=turn_id,
            stage=stage, allowed=allowed, findings=findings,
        )
        session.add(row)
        await session.flush()
        return row

    async def list_for_session(self, session: AsyncSession, *, tenant_id: uuid.UUID, session_id: uuid.UUID) -> list[GuardrailEventRow]:
        """Every guardrail check that ran across one chat session — a
        session-level "what did guardrails catch" review, oldest first."""
        result = await session.execute(
            select(GuardrailEventRow)
            .where(GuardrailEventRow.tenant_id == tenant_id, GuardrailEventRow.session_id == session_id)
            .order_by(GuardrailEventRow.created_at)
        )
        return list(result.scalars().all())

    async def list_blocked(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, since: datetime | None = None, limit: int = 100,
    ) -> list[GuardrailEventRow]:
        """Every check that actually blocked something (allowed=False) for
        one tenant, newest first — an admin's "what's getting blocked"
        review, not a per-session drill-down. `since` narrows to a date
        range; omitted returns the most recent `limit` regardless of age."""
        stmt = select(GuardrailEventRow).where(
            GuardrailEventRow.tenant_id == tenant_id, GuardrailEventRow.allowed.is_(False),
        )
        if since is not None:
            stmt = stmt.where(GuardrailEventRow.created_at >= since)
        stmt = stmt.order_by(GuardrailEventRow.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_stage(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, since: datetime | None = None,
    ) -> dict[str, int]:
        """{stage: count} for one tenant — the raw numbers behind a
        "guardrail activity this week" summary tile. Counts every event
        (allowed and blocked both) per stage; a caller wanting only
        blocked-count-by-stage should filter allowed=False itself, since
        that's a materially different question this method doesn't answer."""
        stmt = select(GuardrailEventRow.stage, func.count()).where(GuardrailEventRow.tenant_id == tenant_id)
        if since is not None:
            stmt = stmt.where(GuardrailEventRow.created_at >= since)
        stmt = stmt.group_by(GuardrailEventRow.stage)
        result = await session.execute(stmt)
        return dict(result.all())


class TraceRepository:
    """Owns both `traces` and `trace_events` — see TraceRow/TraceEventRow's
    docstrings in app/db/models.py. A trace_event never makes sense without
    its parent trace, so start_trace/add_event/finish_trace are the actual
    write sequence one chat turn drives (see Tracer's own turn()/event()
    methods in app/observability.py for the Langfuse-side shape this
    mirrors), not independent CRUD on two unrelated tables."""

    async def start_trace(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, turn_id: str, session_id: uuid.UUID | None = None,
        route: str | None = None, langfuse_trace_id: str | None = None,
    ) -> TraceRow:
        trace = TraceRow(
            tenant_id=tenant_id, session_id=session_id, turn_id=turn_id, route=route,
            langfuse_trace_id=langfuse_trace_id, status="ok",
        )
        session.add(trace)
        await session.flush()
        return trace

    async def add_event(
        self, session: AsyncSession, *, trace_id: uuid.UUID, tenant_id: uuid.UUID, stage: str,
        label: str | None = None, metadata: dict | None = None,
    ) -> TraceEventRow:
        event = TraceEventRow(trace_id=trace_id, tenant_id=tenant_id, stage=stage, label=label, metadata_=metadata or {})
        session.add(event)
        await session.flush()
        return event

    async def finish_trace(
        self, session: AsyncSession, trace_id: uuid.UUID, *, status: str = "ok", error: str | None = None,
        ended_at: datetime | None = None, duration_ms: int | None = None,
    ) -> None:
        """Closes out a trace once the turn it describes completes — status
        "ok" | "blocked" | "error" (see TraceRow's docstring). Caller
        supplies ended_at/duration_ms rather than this method computing
        them from "now": the caller already has the turn's real start time
        (it's the one that called start_trace) and can measure more
        precisely than a second DB round-trip's clock would.

        Fetches then mutates (not a bulk Core update()) so the ORM's own
        unit-of-work keeps this session's identity map in sync — a bulk
        update() bypasses that entirely, leaving any already-loaded TraceRow
        Python object (e.g. the one start_trace returned earlier in this
        same session) silently stale until something re-queries it (verified
        live: get_trace() right after a bulk update() returned the old
        values). No-op if trace_id doesn't exist, matching this method's
        original "fire and forget" contract — a caller closing out a trace
        that's somehow already gone shouldn't itself fail the turn it's
        describing."""
        trace = await session.get(TraceRow, trace_id)
        if trace is None:
            return
        trace.status = status
        trace.error = error
        trace.ended_at = ended_at
        trace.duration_ms = duration_ms

    async def get_trace(self, session: AsyncSession, trace_id: uuid.UUID) -> TraceRow | None:
        return await session.get(TraceRow, trace_id)

    async def get_events(self, session: AsyncSession, trace_id: uuid.UUID) -> list[TraceEventRow]:
        """A trace's full step-by-step timeline, in the order they actually
        happened — what a turn-detail debug view reconstructs from."""
        result = await session.execute(
            select(TraceEventRow).where(TraceEventRow.trace_id == trace_id).order_by(TraceEventRow.at)
        )
        return list(result.scalars().all())

    async def list_for_session(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, session_id: uuid.UUID,
    ) -> list[TraceRow]:
        """Every turn's trace within one chat session, oldest first — what
        a session-level "show me every turn's trace" view lists before
        drilling into one via get_events."""
        result = await session.execute(
            select(TraceRow)
            .where(TraceRow.tenant_id == tenant_id, TraceRow.session_id == session_id)
            .order_by(TraceRow.started_at)
        )
        return list(result.scalars().all())

    async def list_recent(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, limit: int = 50,
    ) -> list[TraceRow]:
        """Most recent traces across every session for one tenant — an
        operator's "what's been happening lately" feed, not scoped to any
        one conversation."""
        result = await session.execute(
            select(TraceRow).where(TraceRow.tenant_id == tenant_id)
            .order_by(TraceRow.started_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


class ModelCallRepository:
    """See ModelCallRow's docstring in app/db/models.py — the row every
    AIProvider.complete()/EmbeddingProvider.embed() call is meant to write
    once it returns (or fails)."""

    async def record(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, call_type: str, provider: str,
        model: str | None = None, trace_id: uuid.UUID | None = None, session_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None, used_fallback: bool = False, prompt_tokens: int | None = None,
        completion_tokens: int | None = None, total_tokens: int | None = None, cost_micros: int | None = None,
        latency_ms: int | None = None, error: str | None = None,
    ) -> ModelCallRow:
        row = ModelCallRow(
            tenant_id=tenant_id, trace_id=trace_id, session_id=session_id, user_id=user_id, call_type=call_type,
            provider=provider, model=model, used_fallback=used_fallback, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, total_tokens=total_tokens, cost_micros=cost_micros,
            latency_ms=latency_ms, error=error,
        )
        session.add(row)
        await session.flush()
        return row

    async def list_for_trace(self, session: AsyncSession, trace_id: uuid.UUID) -> list[ModelCallRow]:
        """Every model call made during one turn — usually one, sometimes
        more (a routed turn calling the router model, then the answering
        model; a RAG turn also embedding the query)."""
        result = await session.execute(select(ModelCallRow).where(ModelCallRow.trace_id == trace_id))
        return list(result.scalars().all())

    async def sum_cost_for_tenant(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, since: datetime, until: datetime,
    ) -> int:
        """Total cost_micros for one tenant over [since, until) — the exact
        query ix_model_calls_tenant_created (app/db/models.py) exists for.
        Returns 0 (not None) for zero matching calls, so a caller never
        needs an `or 0` at every call site."""
        result = await session.execute(
            select(func.coalesce(func.sum(ModelCallRow.cost_micros), 0)).where(
                ModelCallRow.tenant_id == tenant_id,
                ModelCallRow.created_at >= since, ModelCallRow.created_at < until,
            )
        )
        return result.scalar_one()

    async def list_for_tenant(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, since: datetime, until: datetime, limit: int = 500,
    ) -> list[ModelCallRow]:
        """Raw call rows in a date range, newest first — what a detailed
        usage-log export or drill-down view reads (sum_cost_for_tenant is
        the cheap aggregate-only version of the same query, for a summary
        tile that doesn't need every row)."""
        result = await session.execute(
            select(ModelCallRow)
            .where(
                ModelCallRow.tenant_id == tenant_id,
                ModelCallRow.created_at >= since, ModelCallRow.created_at < until,
            )
            .order_by(ModelCallRow.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


class ModelPricingRepository:
    """See ModelPricingRow's docstring in app/db/models.py — a dated rate
    card, not tenant-scoped (pricing is global, set by whoever operates
    this deployment)."""

    async def add_rate(
        self, session: AsyncSession, *, provider: str, model: str, input_price_micros_per_1k: int,
        output_price_micros_per_1k: int, effective_from: datetime | None = None,
    ) -> ModelPricingRow:
        """Records a new rate, effective from `effective_from` (defaults to
        now — see ModelPricingRow's default). Does NOT overwrite or delete
        the previous rate for this (provider, model): the whole point of
        `effective_from` is that both rows coexist, and get_current_rate
        picks the newest one that's already in effect."""
        row = ModelPricingRow(
            provider=provider, model=model, input_price_micros_per_1k=input_price_micros_per_1k,
            output_price_micros_per_1k=output_price_micros_per_1k,
            **({"effective_from": effective_from} if effective_from is not None else {}),
        )
        session.add(row)
        await session.flush()
        return row

    async def get_current_rate(
        self, session: AsyncSession, *, provider: str, model: str, as_of: datetime,
    ) -> ModelPricingRow | None:
        """The rate that was in effect at `as_of` — the newest row for
        (provider, model) whose effective_from is <= as_of (see
        ModelPricingRow's docstring on why there's no effective_to column).
        Returns None for a (provider, model) with no rate ever recorded —
        the caller (ModelCallRepository.record's future caller) decides
        what "no known price" means for cost_micros (almost certainly:
        leave it NULL, don't guess)."""
        result = await session.execute(
            select(ModelPricingRow)
            .where(
                ModelPricingRow.provider == provider, ModelPricingRow.model == model,
                ModelPricingRow.effective_from <= as_of,
            )
            .order_by(ModelPricingRow.effective_from.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_all_current(self, session: AsyncSession, *, as_of: datetime) -> list[ModelPricingRow]:
        """One row per (provider, model) — whichever rate was in effect at
        `as_of` for each. Powers an admin's "current rate card" view. Uses
        a correlated-subquery-free approach (group by, then re-select the
        max effective_from per group) since this table is small enough
        that a window function isn't worth the extra complexity here."""
        latest_per_pair = (
            select(
                ModelPricingRow.provider, ModelPricingRow.model,
                func.max(ModelPricingRow.effective_from).label("max_effective_from"),
            )
            .where(ModelPricingRow.effective_from <= as_of)
            .group_by(ModelPricingRow.provider, ModelPricingRow.model)
            .subquery()
        )
        result = await session.execute(
            select(ModelPricingRow).join(
                latest_per_pair,
                and_(
                    ModelPricingRow.provider == latest_per_pair.c.provider,
                    ModelPricingRow.model == latest_per_pair.c.model,
                    ModelPricingRow.effective_from == latest_per_pair.c.max_effective_from,
                ),
            )
        )
        return list(result.scalars().all())


class UsageRollupRepository:
    """See UsageDailyRollupRow's docstring in app/db/models.py — a
    rebuildable cache over model_calls, upserted per (tenant, day, provider,
    model) rather than one row appended per call."""

    async def upsert_day(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, day: datetime, provider: str, model: str | None,
        call_count: int, prompt_tokens: int, completion_tokens: int, cost_micros: int,
    ) -> None:
        """Replaces the rollup row for this exact (tenant, day, provider,
        model) with the given totals — the caller (a future rollup job)
        computes fresh totals from model_calls and passes them in whole;
        this method doesn't increment anything itself, since re-running the
        job for a day must be idempotent (uq_usage_rollup_tenant_day_provider_model,
        app/db/models.py), not additive on a retry.

        Mutates the fetched row's attributes directly rather than issuing a
        bulk Core update() against it — see finish_trace's docstring above
        for why: a bulk update() on an already-loaded ORM object leaves that
        Python object's attributes stale for the rest of this session,
        verified live (a read via list_range right after would return the
        pre-upsert totals)."""
        existing = await session.execute(
            select(UsageDailyRollupRow).where(
                UsageDailyRollupRow.tenant_id == tenant_id, UsageDailyRollupRow.day == day,
                UsageDailyRollupRow.provider == provider, UsageDailyRollupRow.model == model,
            )
        )
        row = existing.scalar_one_or_none()
        if row is None:
            session.add(UsageDailyRollupRow(
                tenant_id=tenant_id, day=day, provider=provider, model=model, call_count=call_count,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost_micros=cost_micros,
            ))
        else:
            row.call_count = call_count
            row.prompt_tokens = prompt_tokens
            row.completion_tokens = completion_tokens
            row.cost_micros = cost_micros
            row.updated_at = datetime.now(timezone.utc)

    async def list_range(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, since: datetime, until: datetime,
    ) -> list[UsageDailyRollupRow]:
        """Every rollup row for one tenant in [since, until) — what a usage
        dashboard's date-range chart reads directly, day by day, provider
        by provider, without touching model_calls at all."""
        result = await session.execute(
            select(UsageDailyRollupRow)
            .where(
                UsageDailyRollupRow.tenant_id == tenant_id,
                UsageDailyRollupRow.day >= since, UsageDailyRollupRow.day < until,
            )
            .order_by(UsageDailyRollupRow.day)
        )
        return list(result.scalars().all())
