import asyncio
import base64
import binascii
import json
import logging
from uuid import UUID, uuid4

from autogen_core import CancellationToken
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from .auth import AccessTokenClaims
from .config import settings
from .extraction import ExtractionError, extract_document, sanitize_filename
from .models import (
    HealthResponse, ChatRequest, ChatResponse, ChatCancelRequest,
    DocumentCreate, DocumentUpdate, DocumentDelete, DocumentGet,
    SessionStartRequest, SessionStartResponse, SessionGet, SessionGetResponse,
    CheckpointSaveRequest, CheckpointListRequest, CheckpointRestoreRequest,
    CodeExecuteSubmitRequest, HitlDecisionRequest, HitlRequestGet,
    SkillUpload, SkillDelete, SkillRunStart, SkillRunAnswer, SkillRunGet, SkillRunRegenerate,
    SkillRunUploadAnswerFile, SettingsModelsUpdate, ArtifactGet,
    SignupRequest, LoginRequest, AuthTokenResponse, RefreshRequest, RefreshResponse,
    LogoutRequest, CurrentUserResponse,
    PendingUsersResponse, PendingUserSummary, UserApprovalDecisionRequest, UserSuspendRequest,
    UserApprovalHistoryRequest, UserApprovalHistoryResponse, UserApprovalHistoryEntry,
)
from .mcp_tools import describe_mcp_config
from .observability import describe_observability
from .providers import (
    _azure_deployment_names, build_vision_provider, list_available_embedding_models, list_available_models,
)
from .services import service
from .skills import SkillPackageError
from .tenancy import (
    AuthError, decide_user_approval, get_approval_history, get_current_user, list_pending_users,
    login, logout, refresh_access_token, require_superadmin, signup,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
# Separate, unprefixed router for GET /artifacts/{id} — a plain navigable
# view URL (an iframe src / new-tab open can't POST), so it lives outside
# the POST-only /api namespace per .claude/rules/api.md's "not application
# behavior" carve-out (same reasoning as static asset serving in app/main.py).
# Included directly in app/main.py, not nested under `router`.
public_router = APIRouter()


@public_router.get("/artifacts/{artifact_id}")
async def artifacts_view(artifact_id: str):
    """Serves a stored artifact's real content, inline (Content-Disposition:
    inline — opens/embeds in the browser rather than forcing a download) so
    a generated HTML dashboard can be opened in a new tab or embedded in an
    iframe. See POST /api/artifacts/download for the explicit-download path."""
    try:
        artifact = service.artifacts.get(artifact_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(
        content=artifact.content, media_type=artifact.mime_type,
        headers={"Content-Disposition": f'inline; filename="{artifact.filename}"'},
    )

# Stream id -> the CancellationToken driving that in-flight /chat/stream call.
# Populated for the duration of one streaming request, popped once it ends
# (normally, cancelled, or errored) — never persisted, never grows unbounded.
_active_streams: dict[str, CancellationToken] = {}

@router.post("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok")


# --- Auth (app/tenancy.py, app/auth.py) ---------------------------------------

def _require_db_session_factory():
    """Every auth route needs an open DB session; DATABASE_URL is optional
    at CopilotService construction time (see its __init__) so auth can be
    unset without breaking the rest of the app — this is where that
    "unset" state actually surfaces to a caller, as a clear 503 rather than
    an AttributeError on a None session_factory."""
    if service.db_session_factory is None:
        raise HTTPException(status_code=503, detail="Login is not configured on this server (DATABASE_URL unset).")
    return service.db_session_factory


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    """(user_agent, ip_address) for a login/signup's RefreshTokenRow —
    display-only metadata (see that model's docstring in app/db/models.py),
    never used for anything security-critical."""
    return request.headers.get("user-agent"), (request.client.host if request.client else None)


@router.post("/auth/signup", response_model=AuthTokenResponse)
async def auth_signup(request: SignupRequest, http_request: Request):
    session_factory = _require_db_session_factory()
    user_agent, ip_address = _client_meta(http_request)
    async with session_factory() as session:
        try:
            result = await signup(
                session, email=request.email, password=request.password, display_name=request.display_name,
                workspace_name=request.workspace_name, user_agent=user_agent, ip_address=ip_address,
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return AuthTokenResponse(
        access_token=result.access_token, refresh_token=result.refresh_token,
        user_id=str(result.user_id), tenant_id=str(result.tenant_id), email=result.email,
        platform_role=result.platform_role,
    )


@router.post("/auth/login", response_model=AuthTokenResponse)
async def auth_login(request: LoginRequest, http_request: Request):
    session_factory = _require_db_session_factory()
    user_agent, ip_address = _client_meta(http_request)
    tenant_id = UUID(request.tenant_id) if request.tenant_id else None
    async with session_factory() as session:
        try:
            result = await login(
                session, email=request.email, password=request.password, tenant_id=tenant_id,
                user_agent=user_agent, ip_address=ip_address,
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return AuthTokenResponse(
        access_token=result.access_token, refresh_token=result.refresh_token,
        user_id=str(result.user_id), tenant_id=str(result.tenant_id), email=result.email,
        platform_role=result.platform_role,
    )


@router.post("/auth/refresh", response_model=RefreshResponse)
async def auth_refresh(request: RefreshRequest):
    session_factory = _require_db_session_factory()
    async with session_factory() as session:
        try:
            access_token = await refresh_access_token(session, raw_refresh_token=request.refresh_token)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return RefreshResponse(access_token=access_token)


@router.post("/auth/logout")
async def auth_logout(request: LogoutRequest):
    session_factory = _require_db_session_factory()
    async with session_factory() as session:
        await logout(session, raw_refresh_token=request.refresh_token)
    return {"status": "ok"}


@router.post("/auth/me", response_model=CurrentUserResponse)
async def auth_me(claims: AccessTokenClaims = Depends(get_current_user)):
    """Confirms a token is valid and reports whose it is — the frontend's
    "am I still logged in" check on load, and the simplest possible example
    of a route gated by get_current_user (see its docstring in
    app/tenancy.py for why no other existing route uses it yet)."""
    return CurrentUserResponse(
        user_id=str(claims.user_id), tenant_id=str(claims.tenant_id), platform_role=claims.platform_role,
    )


# --- Admin: approve / reject / suspend / reinstate users ----------------------
#
# Every route below requires Depends(require_superadmin) — see that
# dependency's docstring in app/tenancy.py. Deliberately platform-wide, not
# tenant-scoped: approval_status/platform_role are global on User (see
# app/db/models.py), so there is no "this tenant's admin" queue distinct
# from "the deployment's superadmin" queue in this pass — every signup gets
# its own tenant (see app/tenancy.py:signup), so a tenant-scoped approval
# queue wouldn't currently mean anything different anyway.

@router.post("/admin/users/pending", response_model=PendingUsersResponse)
async def admin_users_pending(claims: AccessTokenClaims = Depends(require_superadmin)):
    session_factory = _require_db_session_factory()
    async with session_factory() as session:
        users = await list_pending_users(session)
    return PendingUsersResponse(users=[
        PendingUserSummary(
            user_id=str(u.id), email=u.email, display_name=u.display_name, created_at=u.created_at.isoformat(),
        )
        for u in users
    ])


@router.post("/admin/users/approve")
async def admin_users_approve(request: UserApprovalDecisionRequest, claims: AccessTokenClaims = Depends(require_superadmin)):
    session_factory = _require_db_session_factory()
    async with session_factory() as session:
        try:
            await decide_user_approval(
                session, user_id=UUID(request.user_id), decided_by_user_id=claims.user_id, action="approved",
                tenant_id=claims.tenant_id, reason=request.reason,
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return {"status": "ok"}


@router.post("/admin/users/reject")
async def admin_users_reject(request: UserApprovalDecisionRequest, claims: AccessTokenClaims = Depends(require_superadmin)):
    session_factory = _require_db_session_factory()
    async with session_factory() as session:
        try:
            await decide_user_approval(
                session, user_id=UUID(request.user_id), decided_by_user_id=claims.user_id, action="rejected",
                tenant_id=claims.tenant_id, reason=request.reason,
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return {"status": "ok"}


@router.post("/admin/users/suspend")
async def admin_users_suspend(request: UserSuspendRequest, claims: AccessTokenClaims = Depends(require_superadmin)):
    """Suspends a currently-active account — distinct from reject (which is
    for an account that was never approved in the first place). A
    superadmin can suspend anyone, including another superadmin; there's no
    "can't suspend yourself" guard here — that's a UI-level nicety, not a
    security boundary worth enforcing server-side."""
    session_factory = _require_db_session_factory()
    async with session_factory() as session:
        try:
            await decide_user_approval(
                session, user_id=UUID(request.user_id), decided_by_user_id=claims.user_id, action="suspended",
                tenant_id=claims.tenant_id, reason=request.reason,
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return {"status": "ok"}


@router.post("/admin/users/reinstate")
async def admin_users_reinstate(request: UserApprovalDecisionRequest, claims: AccessTokenClaims = Depends(require_superadmin)):
    """Restores a suspended (or previously rejected) account to active —
    the undo for suspend/reject. Unlike suspend/reject, does NOT revoke
    existing refresh tokens (see decide_user_approval) — there shouldn't be
    any still-live ones for an account that's been non-active, and even if
    one somehow survived, reinstating is explicitly restoring access, not
    the moment to also invalidate sessions."""
    session_factory = _require_db_session_factory()
    async with session_factory() as session:
        try:
            await decide_user_approval(
                session, user_id=UUID(request.user_id), decided_by_user_id=claims.user_id, action="reinstated",
                tenant_id=claims.tenant_id, reason=request.reason,
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return {"status": "ok"}


@router.post("/admin/users/approval-history", response_model=UserApprovalHistoryResponse)
async def admin_users_approval_history(
    request: UserApprovalHistoryRequest, claims: AccessTokenClaims = Depends(require_superadmin),
):
    session_factory = _require_db_session_factory()
    async with session_factory() as session:
        history = await get_approval_history(session, UUID(request.user_id))
    return UserApprovalHistoryResponse(history=[
        UserApprovalHistoryEntry(
            action=h.action, decided_by_user_id=str(h.decided_by_user_id), reason=h.reason,
            created_at=h.created_at.isoformat(),
        )
        for h in history
    ])


def _images_payload(request: ChatRequest) -> list[dict] | None:
    if not request.images:
        return None
    return [{"data": img.data, "mime_type": img.mime_type} for img in request.images]

@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    result = await service.chat(
        request.message, request.session_id,
        images=_images_payload(request), auto_generate=request.auto_generate,
    )
    return ChatResponse(
        response=result["response"],
        # "mock" here would falsely claim a model ran (e.g. mid-flow skill
        # Q&A and blocked-input turns call no provider at all) — see `provider`
        # for the precise, nullable signal; this legacy field just needs to
        # not lie when it's null.
        mode=result["provider"] or "none",
        guardrails=result["guardrails"],
        session_id=result["session_id"],
        agent=result["agent"],
        skills=result["skills"],
        provider=result["provider"],
        model=result.get("model"),
        used_fallback=result["used_fallback"],
        hitl_pending=result["hitl_pending"],
        sources=result["sources"],
        skill_run=result["skill_run"],
        tool_calls=result.get("tool_calls", []),
        web_sources=result.get("web_sources", []),
        downloadable_artifacts=result.get("downloadable_artifacts", []),
        routing=result.get("routing"),
    )

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"

@router.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """SSE variant of /chat: real token-by-token AutoGen streaming for the
    plain direct-chat case (see CopilotService.chat_stream / app/streaming.py),
    a single delta+done pair for everything else. The first event is always
    {"type": "start", "stream_id": ...} — POST that id to /chat/cancel to stop
    the in-flight generation via a real autogen_core.CancellationToken."""
    stream_id = str(uuid4())
    token = CancellationToken()
    _active_streams[stream_id] = token

    async def produce(queue: asyncio.Queue) -> None:
        try:
            async for event in service.chat_stream(
                request.message, request.session_id, token, request.model,
                images=_images_payload(request), auto_generate=request.auto_generate,
            ):
                await queue.put(event)
        except asyncio.CancelledError:
            # Not the normal path -- CopilotService.chat_stream() already turns a
            # cancelled `token` into a clean "cancelled"/"done" event pair itself
            # (see its two cancellation-aware branches). This only fires if this
            # task gets cancelled some other way (e.g. server shutdown), as a
            # defensive fallback so the queue still gets its sentinel below.
            await queue.put({"type": "cancelled"})
        except Exception as exc:  # noqa: BLE001 - the stream must end cleanly, never hang the client
            logger.warning("chat_stream producer failed: %s", exc)
            await queue.put({"type": "error", "message": "Unexpected server error."})
        finally:
            await queue.put(None)  # sentinel: no more events

    async def event_source():
        queue: asyncio.Queue = asyncio.Queue()
        # Deliberately NOT linking `token` to this outer task: CopilotService.chat_stream
        # already links it to exactly the right inner scope for whichever path a given
        # turn takes (AutoGen's own handling for real streaming, an explicit inner task
        # for the delegate path) — linking it here too would race that graceful handling
        # with a raw cancellation at an arbitrary await point.
        task = asyncio.ensure_future(produce(queue))
        yield _sse({"type": "start", "stream_id": stream_id})
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield _sse(item)
        finally:
            _active_streams.pop(stream_id, None)

    return StreamingResponse(event_source(), media_type="text/event-stream")

@router.post("/chat/cancel")
async def chat_cancel(request: ChatCancelRequest):
    token = _active_streams.get(request.stream_id)
    if token is None:
        raise HTTPException(status_code=404, detail="That stream isn't active (already finished or unknown).")
    token.cancel()
    return {"cancelled": True}

@router.post("/agents/list")
async def agents_list():
    return {"agents": [a.__dict__ for a in service.agent_registry.list()]}

@router.post("/skills/list")
async def skills_list():
    return {"skills": [s.__dict__ for s in service.skill_registry.list()]}

@router.post("/session/start", response_model=SessionStartResponse)
async def session_start():
    session = await service.sessions.create()
    return SessionStartResponse(session_id=session["session_id"], created_at=session["created_at"])

@router.post("/session/list")
async def session_list():
    return {"sessions": await service.sessions.list()}

@router.post("/session/get", response_model=SessionGetResponse)
async def session_get(request: SessionGet):
    try:
        return await service.sessions.get(request.session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

# --- checkpoints: user-named save points a session can be rolled back to ---
# (see app/storage.py:SessionStore.add_checkpoint/list_checkpoints/
# restore_checkpoint and its module docstring). Distinct from
# turn_checkpoint (automatic, one per in-flight turn, surfaced via plain
# GET .../session/get above) — these are explicit, user-triggered, and
# persist until deleted or restored past.

@router.post("/session/checkpoint/save")
async def session_checkpoint_save(request: CheckpointSaveRequest):
    checkpoint = await service.sessions.add_checkpoint(request.session_id, request.label)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return checkpoint

@router.post("/session/checkpoint/list")
async def session_checkpoint_list(request: CheckpointListRequest):
    try:
        return {"checkpoints": await service.sessions.list_checkpoints(request.session_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.post("/session/checkpoint/restore")
async def session_checkpoint_restore(request: CheckpointRestoreRequest):
    """Rolls the session back to a saved checkpoint — truncates messages,
    resets memory_state, clears any pending_skill_run/turn_checkpoint. This
    is destructive (messages after the checkpoint are discarded, not kept on
    a branch); the client is expected to confirm with the user before
    calling this. Returns the full updated session, same shape as
    POST /session/get."""
    try:
        return await service.sessions.restore_checkpoint(request.session_id, request.checkpoint_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.post("/models/list")
async def models_list():
    """Real, live model catalog per configured provider (Gemini's /v1beta/models,
    Ollama's /api/tags), for the Copilot model picker — never a hardcoded
    guess (see docs/rag.md's caveat on stale hardcoded model names). Partial
    failures don't fail the whole call; see `errors` for any provider that
    couldn't be queried and why."""
    return await list_available_models()

@router.post("/models/list/embedding")
async def models_list_embedding():
    """Real, live embedding-capable model catalog per configured provider —
    for the Settings page's RAG embedding dropdowns. Distinct from
    /models/list above (chat-completion models): Gemini's own listing
    already flags embedContent support; Ollama needs a concurrent /api/show
    scan (cached for the process — see app/providers.py). Same partial-
    failure shape as /models/list."""
    return await list_available_embedding_models()

def _settings_models_public() -> dict:
    """Every model/provider slot the Settings page's Models panel shows and
    can edit — current effective value plus whether that provider is even
    configured (an API key present), so the UI can grey out an option that
    would fail rather than let you pick it and find out later."""
    return {
        "model_provider": settings.model_provider,
        "gemini_configured": bool(settings.gemini_api_key),
        "ollama_configured": bool(settings.ollama_base_url),
        "azure_configured": bool(
            settings.azure_ai_endpoint and settings.azure_ai_api_key and settings.azure_ai_deployment
        ),
        "gemini_model": settings.gemini_model,  # "" = GeminiProvider.DEFAULT_MODEL
        "ollama_model": settings.ollama_model,
        # Not user-editable here (see SettingsModelsUpdate — azure_ai_deployment
        # isn't one of its fields): unlike gemini_model/ollama_model, there's no
        # free-form model choice for Azure to override at runtime, just the
        # default deployment fixed by .env — shown for visibility, not for
        # editing. `azure_deployments` is every deployment this resource has
        # (default plus any extras — see _azure_deployment_names,
        # app/providers.py), which IS selectable from the Copilot chat
        # picker's "provider/model" mechanism even though this Settings-page
        # default isn't runtime-editable.
        "azure_deployment": settings.azure_ai_deployment,
        "azure_deployments": _azure_deployment_names(),
        "azure_embedding_deployment": settings.azure_ai_embedding_deployment,
        "gemini_embedding_model": settings.gemini_embedding_model,
        "ollama_embedding_model": settings.ollama_embedding_model,
        "agent_router_model": settings.agent_router_model,
    }

@router.post("/settings/models")
async def settings_models_get():
    """Current effective model/provider configuration for the Settings
    page's Models panel — see docs/runtime-settings.md. Read-only; use
    POST /api/settings/models/update to change anything."""
    return _settings_models_public()

@router.post("/settings/models/update")
async def settings_models_update(request: SettingsModelsUpdate):
    """Applies a runtime override to one or more model/provider slots and
    rebuilds every derived provider instance (CopilotService.reload_providers)
    so it takes effect on the very next request — no restart needed. Only
    fields actually present in the request body change; everything else
    keeps its current value. In-memory only (this app's v1 architecture,
    .claude/rules/architecture.md) — a process restart reverts to .env.
    Never accepts or changes an API key; those stay .env-only by design."""
    # exclude_none as well as exclude_unset: every field is Optional so the
    # client CAN omit it, but a client sending an explicit `null` must not
    # write None into a str-typed settings field either — both cases mean
    # "leave this one alone."
    updates = request.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in updates.items():
        setattr(settings, field, value)
    if updates:
        service.reload_providers()
    return _settings_models_public()

@router.post("/guardrails/status")
async def guardrails_status():
    return {
        "phase": 0,
        "enabled": True,
        "checks": [
            "prompt-injection", "retrieved-context-injection", "pii-detection-redaction",
            "toxic-unsafe-content-policy", "sensitive-data-filtering",
        ],
        "description": "Input, retrieved-context, and output are screened for prompt injection, PII "
                        "(redacted, not blocked), secrets/sensitive data (redacted), and an unsafe-content "
                        "policy (blocked) — before and after every model response.",
    }

@router.post("/rag/status")
async def rag_status():
    """What embedding is configured vs. what actually ran the most recent
    ingest/query (RAGStore.describe_embedding()), plus which vector-store
    backend is actually live — Qdrant Cloud or the in-memory fallback,
    reachability, point count (RAGStore.describe_vector_store()). Powers the
    Knowledge tab's status line."""
    embedding = service.rag.describe_embedding()
    vector_store = await service.rag.describe_vector_store()
    return {**embedding, "vector_store": vector_store}

@router.post("/tools/mcp/status")
async def mcp_status():
    """MCP tool server configuration plus whatever the most recent load
    attempt found — see mcp_tools.describe_mcp_config(). Powers the
    Settings tab's "MCP tools" panel. Never loads anything itself (cheap to
    poll) — tools actually load lazily on the first agent-mode chat turn
    that needs them."""
    return describe_mcp_config()

@router.post("/observability/status")
async def observability_status():
    """Whether Langfuse tracing is enabled (never the secret key) — see
    observability.describe_observability(). Powers the Settings tab's
    "Observability" panel."""
    return describe_observability()

@router.post("/rag/document/add")
async def rag_add(request: DocumentCreate):
    try:
        filename = sanitize_filename(request.filename)
        extracted = await extract_document(filename, request.content, request.content_encoding,
                                            settings.max_upload_mb * 1024 * 1024, vision_provider=build_vision_provider())
    except ExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await service.rag.add(filename, extracted.text, blocks=extracted.blocks)

@router.post("/rag/document/update")
async def rag_update(request: DocumentUpdate):
    try:
        filename = sanitize_filename(request.filename) if request.filename is not None else None
        content = None
        blocks = None
        if request.content is not None:
            extracted = await extract_document(filename or "", request.content, request.content_encoding,
                                                settings.max_upload_mb * 1024 * 1024,
                                                vision_provider=build_vision_provider())
            content, blocks = extracted.text, extracted.blocks
        return await service.rag.update(request.document_id, filename, content, blocks=blocks)
    except ExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.post("/rag/document/delete")
async def rag_delete(request: DocumentDelete):
    try:
        await service.rag.delete(request.document_id)
        return {"deleted": True, "document_id": request.document_id}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.post("/rag/document/list")
async def rag_list():
    return {"documents": service.rag.list()}

@router.post("/rag/document/get")
async def rag_get(request: DocumentGet):
    try:
        return service.rag.get(request.document_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.post("/tools/code/submit")
async def tools_code_submit(request: CodeExecuteSubmitRequest):
    """Submits code for execution. Requires HITL approval before it runs (dev-only sandbox)."""
    return service.hitl.submit_code_execution(request.code, request.session_id)

@router.post("/hitl/list")
async def hitl_list():
    return {"requests": service.hitl.list()}

@router.post("/hitl/get")
async def hitl_get(request: HitlRequestGet):
    try:
        return service.hitl.get(request.request_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.post("/hitl/decide")
async def hitl_decide(request: HitlDecisionRequest):
    try:
        return await service.hitl.decide(request.request_id, request.approved)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

# --- artifacts: downloadable files an approved code execution produced ------
# (a generated .html dashboard, a .pdf report — see app/sandbox.py's
# ExecutionResult.artifact_files, app/artifacts.py's ArtifactStore, and
# HitlService.decide()'s downloadable_artifacts on a COMPLETED record.)

@router.post("/artifacts/download")
async def artifacts_download(request: ArtifactGet):
    """POST + client-side blob save, per this API's POST-only convention
    (.claude/rules/api.md) — same pattern as
    POST /skill-packages/run/download. See GET /artifacts/{id} below for the
    separate view-in-browser path, which genuinely needs a plain navigable
    URL (an iframe src / new-tab open can't POST)."""
    try:
        artifact = service.artifacts.get(request.artifact_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(
        content=artifact.content, media_type=artifact.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"'},
    )

# --- skill packages: upload/list + the ask-questions-then-generate HITL flow ---

@router.post("/skill-packages/list")
async def skill_packages_list():
    return {"skills": service.skill_packages.list()}

@router.post("/skill-packages/upload")
async def skill_packages_upload(request: SkillUpload):
    try:
        raw = base64.b64decode(request.content, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Uploaded content is not valid base64.")
    try:
        skill = service.skill_packages.add_from_zip(raw)
    except SkillPackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return skill.public()

@router.post("/skill-packages/delete")
async def skill_packages_delete(request: SkillDelete):
    try:
        service.skill_packages.delete(request.skill_id)
        return {"deleted": True, "skill_id": request.skill_id}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SkillPackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/skill-packages/run/start")
async def skill_run_start(request: SkillRunStart):
    """Opens a run awaiting answers and returns the skill's declared
    pre-flight questions (SKILL.md `questions:`) for the UI to render as a
    HITL form — nothing generates until /run/answer is called."""
    try:
        skill = service.skill_packages.get(request.skill_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    run = service.skill_runs.start(request.skill_id)
    return {**run.public(), "skill": skill.public()}

@router.post("/skill-packages/run/answer")
async def skill_run_answer(request: SkillRunAnswer):
    try:
        run = await service.skill_runs.submit_answers(request.run_id, request.answers)
        return run.public()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SkillPackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/skill-packages/run/upload-answer-file")
async def skill_run_upload_answer_file(request: SkillRunUploadAnswerFile):
    """Stages one file for a `type: "file"` skill question — called the
    moment a file is chosen client-side, before the rest of the pre-flight
    form is submitted (POST /skill-packages/run/answer). Same decode-then-
    domain-validate pattern as POST /skill-packages/upload: base64 errors
    get a generic 400, domain errors (unknown question, disallowed
    extension) get the exception's own message, both safe to show a client
    as-is (see SkillPackageError's docstring)."""
    try:
        raw = base64.b64decode(request.content, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Uploaded content is not valid base64.")
    try:
        filename = sanitize_filename(request.filename)
    except ExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"File exceeds the {settings.max_upload_mb} MB upload limit.")
    try:
        return service.skill_runs.upload_answer_file(request.run_id, request.question_id, filename, raw)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SkillPackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/skill-packages/run/get")
async def skill_run_get(request: SkillRunGet):
    try:
        return service.skill_runs.get(request.run_id).public()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.post("/skill-packages/run/list")
async def skill_run_list():
    return {"runs": service.skill_runs.list()}

@router.post("/skill-packages/run/regenerate")
async def skill_run_regenerate(request: SkillRunRegenerate):
    """View/Edit flow: re-runs generation from a user-edited spec, skipping
    the drafting step since the content is already what the user wants."""
    try:
        run = service.skill_runs.regenerate(request.run_id, request.spec)
        return run.public()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SkillPackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.post("/skill-packages/run/download")
async def skill_run_download(request: SkillRunGet):
    """POST rather than a GET-with-path-param, per this API's POST-only
    convention (.claude/rules/api.md) — the frontend fetches this as a blob
    and triggers the browser's save dialog client-side. `format` picks
    which file for a multi-output run (e.g. brd-prd-generator's docx/pdf);
    omitted (every ordinary single-output skill) picks the sole output.

    Output files live in B2 now, not on local disk (see
    SkillRunService.get_output/app/blob_store.py), so this is a plain
    Response over the fetched bytes rather than FileResponse — FileResponse
    needs a real local filesystem path, which no longer exists here."""
    try:
        content, filename, media_type = service.skill_runs.get_output(request.run_id, format=request.format)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SkillPackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
