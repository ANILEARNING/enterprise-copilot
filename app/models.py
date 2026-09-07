from typing import Literal

from pydantic import BaseModel, Field

class HealthResponse(BaseModel):
    status: str

class ImageAttachment(BaseModel):
    """One multimodal chat attachment — raw base64, no data: URI prefix (the
    frontend strips it before sending). See AIProvider.complete()'s `images`
    param (app/providers.py) and docs/multimodal.md."""
    data: str = Field(min_length=1, max_length=15_000_000)  # ~11MB decoded
    mime_type: str = Field(default="image/png", pattern=r"^image/[a-zA-Z0-9.+-]+$")

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    session_id: str | None = None
    conversation_id: str | None = None
    model: str | None = None
    images: list[ImageAttachment] = Field(default_factory=list)
    # UI "Auto-generate" toggle (see static/app.js) — only meaningful for the
    # Deck Builder flow (a "pptx" chat-trigger match, see CopilotService.chat).
    # False (default, the safer state): a drafted deck spec is queued through
    # HitlService for approval before generate_pptx.py runs, same review flow
    # as the coding skill's code-execution HITL gate. True: generates
    # immediately once the spec is ready, no approval step. No effect on any
    # other chat path.
    auto_generate: bool = False

class ChatResponse(BaseModel):
    response: str
    mode: str
    guardrails: dict
    session_id: str
    agent: str | None = None
    skills: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    used_fallback: bool = False
    hitl_pending: list[str] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)
    skill_run: dict | None = None
    # Names of MCP tools actually invoked this turn (agent-mode only) — see
    # OrchestrationResult.tool_calls, app/agents.py.
    tool_calls: list[str] = Field(default_factory=list)
    # Real web_search results the model actually saw this turn — {"title",
    # "url", "content"} each. See OrchestrationResult.web_sources,
    # app/agents.py:_parse_web_search_result.
    web_sources: list[dict] = Field(default_factory=list)
    # A generated .html/.pdf the coding agent's approved code produced this
    # turn — {"artifact_id", "filename", "mime_type", "size_bytes",
    # "view_url"} each. See OrchestrationResult.downloadable_artifacts,
    # app/artifacts.py:StoredArtifact.public(). Only ever populated on the
    # live-SSE-wait path (agent-mode POST /api/chat/stream).
    downloadable_artifacts: list[dict] = Field(default_factory=list)
    # What this turn was routed to and why — {"route", "skill_id",
    # "needs_web", "routed_by_llm", "reason"}. See
    # app/agents.py:TurnPlan. None when no routing decision was made: a
    # blocked input, or a turn that continued an already-pending skill Q&A /
    # deck clarification rather than classifying a new request.
    routing: dict | None = None

class ChatCancelRequest(BaseModel):
    stream_id: str = Field(min_length=1)

class SessionStartRequest(BaseModel):
    pass

class SessionStartResponse(BaseModel):
    session_id: str
    created_at: str

class SessionGet(BaseModel):
    session_id: str = Field(min_length=1)

class SessionGetResponse(BaseModel):
    """Full session state — see app/storage.py's module docstring for the
    on-disk shape this mirrors. `turn_checkpoint`/`checkpoints` are the
    Checkpointer feature's fields: `turn_checkpoint` is non-null only when
    the last turn on this session was interrupted before completing (a
    resume UI can show "your last turn didn't finish"); `checkpoints` is
    this session's user-saved named restore points (see POST
    /session/checkpoint/*). `pending_deck_builder`/`last_deck_spec` are the
    Deck Builder flow's fields (see app/services.py:DeckBuilderService) —
    `pending_deck_builder` is non-null only mid-clarification (before a spec
    is ready); `last_deck_spec` is the most recently generated/approved
    deck's spec, used to seed an "enhance this" follow-up."""
    session_id: str
    created_at: str
    updated_at: str
    messages: list[dict] = Field(default_factory=list)
    memory_state: dict | None = None
    pending_skill_run: dict | None = None
    turn_checkpoint: dict | None = None
    checkpoints: list[dict] = Field(default_factory=list)
    pending_deck_builder: dict | None = None
    last_deck_spec: dict | None = None

class CheckpointSaveRequest(BaseModel):
    session_id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=200)

class CheckpointListRequest(BaseModel):
    session_id: str = Field(min_length=1)

class CheckpointRestoreRequest(BaseModel):
    session_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)

class CodeExecuteSubmitRequest(BaseModel):
    code: str = Field(min_length=1, max_length=20000)
    session_id: str | None = None

class HitlDecisionRequest(BaseModel):
    request_id: str = Field(min_length=1)
    approved: bool

class HitlRequestGet(BaseModel):
    request_id: str = Field(min_length=1)

class ArtifactGet(BaseModel):
    artifact_id: str = Field(min_length=1)

class DocumentCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    # Bumped from 500_000: a base64-encoded PDF upload (~4/3 of raw bytes) needs
    # headroom up to MAX_UPLOAD_MB; the byte-accurate limit is enforced in
    # app/extraction.py after decoding, this is just an outer sanity cap.
    content: str = Field(min_length=1, max_length=30_000_000)
    content_encoding: Literal["text", "base64"] = "text"

class DocumentUpdate(BaseModel):
    document_id: str = Field(min_length=1)
    filename: str | None = Field(default=None, max_length=255)
    content: str | None = Field(default=None, max_length=30_000_000)
    content_encoding: Literal["text", "base64"] = "text"

class DocumentDelete(BaseModel):
    document_id: str = Field(min_length=1)

class DocumentGet(BaseModel):
    document_id: str = Field(min_length=1)

class SkillUpload(BaseModel):
    # Base64-encoded .zip, same pattern as a PDF document upload (see DocumentCreate).
    content: str = Field(min_length=1, max_length=30_000_000)

class SkillDelete(BaseModel):
    skill_id: str = Field(min_length=1)

class SkillRunStart(BaseModel):
    skill_id: str = Field(min_length=1)

class SkillRunAnswer(BaseModel):
    run_id: str = Field(min_length=1)
    answers: dict[str, str] = Field(default_factory=dict)

class SkillRunGet(BaseModel):
    run_id: str = Field(min_length=1)
    # /run/get ignores this; /run/download uses it to pick which file for a
    # multi-output run (e.g. "docx" or "pdf" — see
    # app/skills.py:SkillRunService.get_output). None (every existing call
    # site) means "the sole output," matching today's single-file behavior.
    format: str | None = Field(default=None, max_length=20)

class SkillRunUploadAnswerFile(BaseModel):
    """One uploaded file for a `type: "file"` skill question — see
    SkillQuestion.accept (app/skills.py) and POST
    /skill-packages/run/upload-answer-file. Same base64-upload shape as
    DocumentCreate; the response's file_id is what the client then puts into
    the plain SkillRunAnswer.answers dict for this question_id."""
    run_id: str = Field(min_length=1)
    question_id: str = Field(min_length=1)
    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=30_000_000)

class SkillRunRegenerate(BaseModel):
    run_id: str = Field(min_length=1)
    spec: dict = Field(default_factory=dict)

class SettingsModelsUpdate(BaseModel):
    """Runtime overrides for the Settings page's Models panel — every field
    optional; only what's provided is changed, everything else keeps its
    current value (see POST /api/settings/models, docs/runtime-settings.md).
    Never includes an API key — those stay .env-only by design."""
    model_provider: Literal["gemini", "ollama", "azure"] | None = None
    gemini_model: str | None = Field(default=None, max_length=200)
    ollama_model: str | None = Field(default=None, max_length=200)
    gemini_embedding_model: str | None = Field(default=None, max_length=200)
    ollama_embedding_model: str | None = Field(default=None, max_length=200)
    agent_router_model: str | None = Field(default=None, max_length=200)


# --- Auth (app/tenancy.py, app/auth.py) ---------------------------------------

class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    # bcrypt silently can't hash past 72 bytes (see app/auth.py) — 72 chars
    # is a safe upper bound for any reasonable password (72 bytes only
    # binds tighter than 72 chars for non-ASCII input, which is rare enough
    # here not to warrant a byte-length check at the request-validation layer).
    password: str = Field(min_length=8, max_length=72)
    display_name: str | None = Field(default=None, max_length=255)
    # The new tenant's display name (see Tenant.name, app/db/models.py) —
    # omitted falls back to "<display_name or email>'s workspace" (see
    # app/tenancy.py:signup).
    workspace_name: str | None = Field(default=None, max_length=255)

class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=72)
    # Picks which of the user's tenant memberships this login's token is
    # scoped to (see app/tenancy.py:login) — omitted uses their
    # earliest-joined active membership, the common single-tenant case.
    tenant_id: str | None = None

class AuthTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    user_id: str
    tenant_id: str
    email: str
    # Also embedded in access_token's own JWT claims (see
    # app/auth.py:create_access_token) — duplicated here too so the
    # frontend can show/hide role-gated UI (e.g. the Admin nav item)
    # immediately on login/signup without decoding the JWT client-side.
    platform_role: str

class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)

class RefreshResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"

class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1)

class CurrentUserResponse(BaseModel):
    user_id: str
    tenant_id: str
    platform_role: str


# --- Admin: approve / reject / suspend / reinstate users (app/tenancy.py) -----

class PendingUserSummary(BaseModel):
    user_id: str
    email: str
    display_name: str | None
    created_at: str

class PendingUsersResponse(BaseModel):
    users: list[PendingUserSummary]

class UserApprovalDecisionRequest(BaseModel):
    user_id: str
    reason: str | None = Field(default=None, max_length=2000)

class UserSuspendRequest(BaseModel):
    user_id: str
    reason: str | None = Field(default=None, max_length=2000)

class UserApprovalHistoryRequest(BaseModel):
    user_id: str

class UserApprovalHistoryEntry(BaseModel):
    action: str
    decided_by_user_id: str
    reason: str | None
    created_at: str

class UserApprovalHistoryResponse(BaseModel):
    history: list[UserApprovalHistoryEntry]
