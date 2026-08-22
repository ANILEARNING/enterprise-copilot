import asyncio
import base64
import binascii
import json
import logging
from uuid import uuid4

from autogen_core import CancellationToken
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from .config import settings
from .extraction import ExtractionError, extract_text, sanitize_filename
from .models import (
    HealthResponse, ChatRequest, ChatResponse, ChatCancelRequest,
    DocumentCreate, DocumentUpdate, DocumentDelete, DocumentGet,
    SessionStartRequest, SessionStartResponse, SessionGet,
    CodeExecuteSubmitRequest, HitlDecisionRequest, HitlRequestGet,
    SkillUpload, SkillDelete, SkillRunStart, SkillRunAnswer, SkillRunGet, SkillRunRegenerate,
    SkillRunUploadAnswerFile, SettingsModelsUpdate, ArtifactGet,
)
from .mcp_tools import describe_mcp_config
from .observability import describe_observability
from .providers import list_available_embedding_models, list_available_models
from .services import service
from .skills import SkillPackageError

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

def _images_payload(request: ChatRequest) -> list[dict] | None:
    if not request.images:
        return None
    return [{"data": img.data, "mime_type": img.mime_type} for img in request.images]

@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    result = await service.chat(
        request.message, request.agent_mode, request.session_id,
        images=_images_payload(request), web_search=request.web_search,
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
                request.message, request.agent_mode, request.session_id, token, request.model,
                images=_images_payload(request), web_search=request.web_search,
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
    session = service.sessions.create()
    return SessionStartResponse(session_id=session["session_id"], created_at=session["created_at"])

@router.post("/session/list")
async def session_list():
    return {"sessions": service.sessions.list()}

@router.post("/session/get")
async def session_get(request: SessionGet):
    try:
        return service.sessions.get(request.session_id)
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
        "gemini_model": settings.gemini_model,  # "" = GeminiProvider.DEFAULT_MODEL
        "ollama_model": settings.ollama_model,
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
        "checks": ["prompt-injection", "retrieved-context-injection", "secret-like-output"],
        "description": "Lightweight input/output policy checks run before and after model responses."
    }

@router.post("/rag/status")
async def rag_status():
    """What embedding is configured vs. what actually ran the most recent
    ingest/query — see RAGStore.describe_embedding(). Powers the Knowledge
    tab's "embedding model" status line."""
    return service.rag.describe_embedding()

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
        text = extract_text(filename, request.content, request.content_encoding,
                             settings.max_upload_mb * 1024 * 1024)
    except ExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await service.rag.add(filename, text)

@router.post("/rag/document/update")
async def rag_update(request: DocumentUpdate):
    try:
        filename = sanitize_filename(request.filename) if request.filename is not None else None
        content = None
        if request.content is not None:
            content = extract_text(filename or "", request.content, request.content_encoding,
                                    settings.max_upload_mb * 1024 * 1024)
        return await service.rag.update(request.document_id, filename, content)
    except ExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.post("/rag/document/delete")
async def rag_delete(request: DocumentDelete):
    try:
        service.rag.delete(request.document_id)
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

# Real MIME types for skill-run downloads (previously always
# application/octet-stream) — keyed by the actual file extension produced,
# not a per-skill guess, so this covers every skill's output uniformly.
_SKILL_OUTPUT_MIME_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
}

@router.post("/skill-packages/run/download")
async def skill_run_download(request: SkillRunGet):
    """POST rather than a GET-with-path-param, per this API's POST-only
    convention (.claude/rules/api.md) — the frontend fetches this as a blob
    and triggers the browser's save dialog client-side. `format` picks
    which file for a multi-output run (e.g. brd-prd-generator's docx/pdf);
    omitted (every ordinary single-output skill) picks the sole output."""
    try:
        path, filename = service.skill_runs.get_output(request.run_id, format=request.format)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SkillPackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    media_type = _SKILL_OUTPUT_MIME_TYPES.get(extension, "application/octet-stream")
    return FileResponse(path, filename=filename, media_type=media_type)
