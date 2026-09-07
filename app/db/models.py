"""SQLAlchemy 2.x declarative schema for every tenant-scoped table this app
persists, plus the `tenants`/`users`/`user_tenants`/`auth_identities` tables
that make tenant-level management and email/password login real (see
docs/database.md and docs/auth.md).

Deliberately Core-style, not relationship-heavy ORM: every table below is a
plain `mapped_column` declaration, no `relationship()`, no lazy-loading, no
cascades. Callers (app/db/*_repository.py, and the rewritten
SessionStore/DocumentStore) write explicit `select()`/`insert()`/`update()`
statements against these tables rather than walking ORM-managed object
graphs — see .claude/rules/architecture.md's "keep abstractions minimal"
and CLAUDE.md's same instruction. This mirrors the explicit-dict style the
file-backed stores already used (app/storage.py, app/document_store.py).

Tenant isolation is enforced by convention, not by the database: every
tenant-scoped table carries a `tenant_id` column, and every repository
method below (and in the sibling *_repository.py modules) must include
`WHERE tenant_id = :tenant_id` on every query. There is no Postgres RLS
policy backing this — row-level *security* was not asked for, only
row-level *scoping* in application code (see the approved plan's "shared
database, tenant_id column... row-level query scoping").
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, CHAR


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GUID(TypeDecorator):
    """Platform-independent UUID column.

    Postgres has a native UUID type; SQLite (used for the test suite — see
    conftest.py and docs/database.md's "why SQLite is enough for tests")
    does not. Stored as a 36-char string on backends without native UUID
    support, a real UUID type on Postgres. Values are always Python `uuid.UUID`
    at the application boundary regardless of backend — this is the one
    place that distinction is handled, so no calling code needs an
    if-Postgres-else-SQLite branch anywhere else.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID as PG_UUID
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return str(value)
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(GUID(), primary_key=True, default=uuid.uuid4)


# --- Tenancy + auth ----------------------------------------------------------

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # URL/id-safe identifier, slugified from the signup-time tenant_name (see
    # app/tenancy.py). No longer client-supplied per-request now that tenant
    # identity comes from the JWT — kept as a stable, readable identifier for
    # URLs/logs, and to enforce "no two tenants with a colliding readable name."
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class User(Base):
    """`platform_role` and `approval_status` are both global (not
    tenant-scoped) because they answer questions that must hold true across
    every tenant a user belongs to: "can this person administer the whole
    deployment" and "should this account be able to sign in at all yet".
    Per-tenant standing (member vs. tenant-admin, and whether *that specific*
    membership has been approved) lives on UserTenant below — the two are
    independent: a globally-approved user can still have a pending
    membership row in a second tenant they just requested to join."""
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    # NULL for a user who only ever signs in via Google/SSO once those ship
    # (see AuthIdentity below) — password auth is optional per-user, not
    # every account has one.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Platform-level role, independent of any tenant — "superadmin" can
    # administer every tenant on this deployment (approve users, edit the
    # model_pricing rate card, etc.); "user" is everyone else, whose actual
    # permissions come entirely from their UserTenant.role rows. Deliberately
    # not on UserTenant: a superadmin's authority isn't scoped to one tenant,
    # so it has no natural home on a per-membership row.
    platform_role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    # Signup gating: a new account starts "pending" and cannot sign in
    # (enforced in app code, alongside is_active) until an admin approves it
    # — see UserApprovalRow below for the full decision history this
    # summarizes. "active" | "pending" | "rejected" | "suspended".
    approval_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    # Denormalized pointer to the most recent UserApprovalRow, so "who
    # approved this account" reads without a join in the common case — the
    # full history (including a later suspend/reinstate) still lives in
    # user_approvals; this is a cache of its latest row, not a replacement.
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_users_approval_status", "approval_status"),
    )


class UserTenant(Base):
    """One row per (user, tenant) membership — a user can belong to more
    than one tenant (see the approved plan's user<->tenant decision), each
    with its own role. Composite PK: a user can only have one membership
    row per tenant.

    `role` is deliberately still a plain string, not a DB enum — Postgres
    enum types are painful to extend later (ALTER TYPE ... ADD VALUE can't
    run inside a transaction on older Postgres); the valid set ("owner" |
    "admin" | "member") is enforced in app code at the same layer that
    already enforces tenant_id scoping (see this file's module docstring)."""
    __tablename__ = "user_tenants"

    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(50), default="member", nullable=False)
    # A membership's own approval state, independent of User.approval_status
    # above — covers "already-approved user requests to join a second
    # tenant" without touching their global account standing. "active" |
    # "pending" | "rejected".
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_user_tenants_tenant_id", "tenant_id"),
        Index("ix_user_tenants_tenant_status", "tenant_id", "status"),
    )


class UserApprovalRow(Base):
    """Append-only audit log of every approve / reject / suspend / reinstate
    decision made about a user — User.approval_status and
    approved_by_user_id/approved_at above are a cache of the latest row
    here, kept for cheap reads; this table is the source of truth for "who
    decided what, and when" (see security.md — admin actions on other
    users' accounts need an audit trail, not just a current-state flag).

    Scoped to the acting admin's tenant (`tenant_id`) even though
    User.approval_status is global — a platform superadmin can act from any
    tenant context, but the record still shows which tenant's admin screen
    the decision was made from."""
    __tablename__ = "user_approvals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    decided_by_user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    # "approved" | "rejected" | "suspended" | "reinstated"
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_user_approvals_user_id", "user_id"),
    )


class AuthIdentity(Base):
    """Reserved for Google OAuth / SSO — no provider integration exists yet
    (see app/tenancy.py's module docstring and docs/auth.md). Password auth
    lives on User.password_hash directly, not as a row here; this table only
    ever holds external-provider identities once those are wired up."""
    __tablename__ = "auth_identities"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # "google" | "sso"
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_auth_identities_provider_subject"),
        Index("ix_auth_identities_user_id", "user_id"),
    )


# --- Sessions / messages — Redis, not Postgres --------------------------------
#
# Chat sessions and their transcripts live in Redis (Upstash), not here — see
# app/session_store.py (RedisSessionStore). Deliberate choice, not an
# oversight: a session is accessed key-by-key by session_id, never joined
# against or aggregated over in SQL, so it never needed a relational home —
# Redis's own TTL/eviction is the retention policy instead of a DELETE. This
# comment is the only trace of the SessionRow/MessageRow tables that used to
# live in this file; see the Alembic revision that dropped them for the
# migration that undid the original Postgres-first design.


# --- Documents (app/document_store.py::DocumentStore) ------------------------

class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Raw document text lives in Backblaze B2 (S3-compatible), not here — this
    # is the pointer to that object, not the content itself. See
    # app/blob_store.py. Metadata (filename/status/chunk_hashes/blocks) stays
    # relational because it's genuinely queried/filtered/joined on; the
    # content itself never is.
    b2_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="indexed")
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # {chunk_id: content_hash} — always replaced wholesale (see
    # DocumentStore.set_chunk_hashes), never queried by individual key.
    chunk_hashes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # Serialized ExtractedBlock list (app/extraction.py) — opaque, only ever
    # read back whole by RAGStore._resolve_blocks.
    blocks: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_documents_tenant_id", "tenant_id"),
    )


# --- HITL requests (app/services.py::HitlService) -----------------------------

class HitlRequestRow(Base):
    __tablename__ = "hitl_requests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # "code_execution" | "deck_generation"
    # Not FK-enforced — today's records tolerate a null/foreign session_id
    # (see HitlService.submit_code_execution's session_id param), kept as-is.
    session_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    code: Mapped[str | None] = mapped_column(Text, nullable=True)  # kind == code_execution only
    deck_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # kind == deck_generation only
    skill_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # kind == deck_generation only
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    downloadable_artifacts: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_hitl_requests_tenant_id", "tenant_id"),
    )


# --- Skill packages / runs (app/skills.py) ------------------------------------

class SkillPackageRow(Base):
    """Uploaded (non-builtin) skill packages only — builtins stay checked-in
    files under skills/, loaded at startup, visible to every tenant. This
    row is a tenant-scoped index over an uploaded skill's on-disk file tree
    (SKILL.md, scripts/, reference/), not a replacement for it."""
    __tablename__ = "skill_packages"

    id: Mapped[uuid.UUID] = _uuid_pk()  # == the on-disk folder name
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    trigger: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_skill_packages_tenant_id", "tenant_id"),
    )


class SkillRunRow(Base):
    __tablename__ = "skill_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    answers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Output files themselves stay on disk (data/skill-runs/<run_id>/output/...);
    # only this list of path strings moves to the DB.
    output_paths: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    used_fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_skill_runs_tenant_id", "tenant_id"),
    )


# --- Login sessions (app/tenancy.py — JWT issuance/revocation) ---------------

class RefreshTokenRow(Base):
    """One row per issued refresh token, so login sessions are revocable and
    listable ("log out this device") instead of living only as an unverifiable
    JWT. The short-lived access token itself is never stored (see docs/auth.md)
    — it's a signed JWT verified statelessly on every request; only the
    longer-lived refresh token needs a server-side record, because revoking it
    is the only way to end a session early.

    `token_hash` never stores the raw token (same posture as User.password_hash)
    — the value handed to the client is only ever compared by hashing the
    presented token and looking up the hash, so a DB read never exposes a
    live credential."""
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # Denormalized request metadata at issuance time, shown back on a
    # "your active sessions" page — never used for anything security-critical
    # (an IP/UA header is trivially spoofable, see security.md).
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set on logout / explicit revoke / password change (revoke-all). NULL
    # (not yet revoked) is the common case — a still-valid session simply
    # has no row to check beyond expires_at.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_tenant_id", "tenant_id"),
    )


# --- Guardrails (app/services.py::GuardrailService) ---------------------------

class GuardrailEventRow(Base):
    """One row per guardrail check actually run — up to three per chat turn
    (check_input, check_context, check_output; see guardrails.md's "every
    chat request must pass an input check ... and an output check"), plus
    one for redact_context_pii when RAG sources are involved. Written
    fire-and-forget after each check returns, same as Tracer in
    app/observability.py: a write failure here must never block or fail the
    turn it's describing.

    Findings are stored exactly as GuardrailService already shapes them
    (`{"pii": [...], "sensitive_data": [...], "unsafe_content": [...],
    "matched_rules": [...]}`) rather than normalized into separate columns —
    the UI's guardrail-activity panel (static/app.js:guardrailActivityHtml)
    already renders this shape directly, and per guardrails.md nothing here
    ever holds the actual matched value, only category + count."""
    __tablename__ = "guardrail_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # "check_input" | "check_context" | "check_output" | "redact_context_pii"
    stage: Mapped[str] = mapped_column(String(30), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # GuardrailService's own returned dict, verbatim — pii/sensitive_data/
    # unsafe_content/matched_rules/message. redacted_text is deliberately
    # dropped before storage (see _sanitize_findings in the repository layer)
    # — persisting the post-redaction text is fine, but there is no reason to
    # keep a second, DB-resident copy of message content already in `messages`.
    findings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_guardrail_events_tenant_id", "tenant_id"),
        Index("ix_guardrail_events_session_id", "session_id"),
    )


# --- Observability (app/observability.py::Tracer) -----------------------------

class TraceRow(Base):
    """Local mirror of one Langfuse trace (one per chat turn) — exists so
    "what happened on this turn" is queryable from this app's own DB without
    round-tripping to Langfuse, and so trace history survives when
    LANGFUSE_PUBLIC_KEY/SECRET_KEY are unset (Tracer.enabled is False in
    every dev/test run — see observability.py's module docstring — but a
    turn still deserves a durable local record of what ran and what it
    cost). `langfuse_trace_id` is nullable and only ever populated when
    Langfuse actually accepted the trace, so the two systems can be
    cross-referenced without requiring Langfuse to be configured."""
    __tablename__ = "traces"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Mirrors AgentRegistry's routed agent ("research-agent" | "coding-agent"
    # | "data-analysis-agent" | "general") plus "deck-builder" / "skill:<id>"
    # for the non-chat entry points that also emit progress events.
    route: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")  # "ok" | "blocked" | "error"
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_traces_tenant_id", "tenant_id"),
        Index("ix_traces_session_id", "session_id"),
    )


class TraceEventRow(Base):
    """Child of `traces` — one row per progress-event dict already threaded
    through CopilotService.chat()'s on_event callback (app/agents.py,
    app/skills.py, app/mcp_tools.py). This table is the same "single source
    of truth" data Tracer.event() forwards to Langfuse, just persisted
    locally too — see observability.py's module docstring. Ordered by `at`
    within a trace to reconstruct a turn's full step-by-step timeline."""
    __tablename__ = "trace_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    trace_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("traces.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    stage: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. "routing", "thinking", "model_call"
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Remaining event fields verbatim (mirrors TurnHandle.event's **fields) —
    # opaque, only ever rendered back as a timeline entry, never queried into.
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_trace_events_trace_id", "trace_id"),
    )


class ModelCallRow(Base):
    """One row per completed AIProvider.complete() / EmbeddingProvider.embed()
    call — the "generation" observations Tracer.generation() already logs to
    Langfuse (see observability.py), plus the token/cost accounting Langfuse
    itself doesn't give this app back structurally. This is the row
    token-usage and cost reporting are built from; traces/trace_events answer
    "what happened", this table answers "what did it cost".

    Token counts come from each provider's own usage payload — Gemini's
    `usageMetadata.{promptTokenCount,candidatesTokenCount,totalTokenCount}`
    (see GeminiProvider.complete, app/providers.py — not currently read out
    of the response, this table is what motivates capturing it), Ollama's
    `prompt_eval_count`/`eval_count`. A provider that reports no usage
    (MockProvider; a provider call that errors before a response body exists)
    leaves the token columns NULL rather than 0 — NULL means "unknown", 0
    means "measured, zero"."""
    __tablename__ = "model_calls"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    trace_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("traces.id"), nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    # "chat_completion" | "embedding" | "agent_routing" | "skill_draft" | "guardrail_classifier"
    # (the last two reserved for when check_input/check_output stop being
    # pure regex — see GuardrailService's docstring "swappable later for a
    # real PII/toxicity model behind this same method contract").
    call_type: Mapped[str] = mapped_column(String(30), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # ProviderResult.provider / EmbeddingResult.provider
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)  # ProviderResult.model
    used_fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Computed at write time from ModelPricingRow (below) rather than joined
    # at read time — pricing changes over time, and a historical call must
    # keep costing what it cost when it ran, not be silently repriced by a
    # later rate-card edit. Stored in USD micro-dollars (1e-6 USD, i.e.
    # $1.00 == 1_000_000) as an integer so cost math never hits float
    # rounding error across millions of cheap calls; convert to display
    # dollars only at render time.
    cost_micros: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_model_calls_tenant_id", "tenant_id"),
        Index("ix_model_calls_trace_id", "trace_id"),
        # Primary access pattern for a usage/cost dashboard: "this tenant's
        # calls in [date range]" — see docs/database.md's index-selection note.
        Index("ix_model_calls_tenant_created", "tenant_id", "created_at"),
    )


class ModelPricingRow(Base):
    """Rate card ModelCallRow.cost_micros is computed from at write time.
    Not tenant-scoped — pricing is global, set by whoever operates this
    deployment (see docs/database.md), not per-customer. `effective_from`
    lets a price change be recorded without losing what a call cost under
    the previous rate: a lookup takes the newest row for
    (provider, model) with effective_from <= the call's created_at, so
    historical ModelCallRow.cost_micros values computed under an old rate
    are never contradicted by a new one. No effective_to column — the next
    row's effective_from is that boundary, so there's exactly one way to
    read "what was the rate on date X", not two columns that could disagree."""
    __tablename__ = "model_pricing"

    id: Mapped[uuid.UUID] = _uuid_pk()
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_price_micros_per_1k: Mapped[int] = mapped_column(Integer, nullable=False)
    output_price_micros_per_1k: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_model_pricing_provider_model", "provider", "model", "effective_from"),
    )


class UsageDailyRollupRow(Base):
    """Pre-aggregated per-tenant-per-day usage, refreshed from `model_calls`
    (see app/db/usage_repository.py's rollup job) rather than computed live
    on every dashboard load — a usage/cost page reads N rows here (N = days
    in range) instead of scanning every model call in that range. This is a
    cache of model_calls, not a new source of truth: it can always be
    rebuilt by re-aggregating model_calls for the affected day, and doing so
    is the correct fix if a rollup and its source rows ever disagree."""
    __tablename__ = "usage_daily_rollups"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # truncated to date, tz-normalized UTC
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_micros: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "day", "provider", "model", name="uq_usage_rollup_tenant_day_provider_model"),
        Index("ix_usage_daily_rollups_tenant_id", "tenant_id"),
    )
