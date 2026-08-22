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
    agent_mode: bool = False
    # UI "Web Search" toggle (see static/app.js) — offers the Tavily
    # web_search MCP tool for this turn (app/mcp_tools.py). No effect unless
    # agent_mode is also on (tool-calling only happens in agent mode) and
    # TAVILY_API_KEY is configured; otherwise a strictly additive no-op.
    web_search: bool = False
    images: list[ImageAttachment] = Field(default_factory=list)

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

class ChatCancelRequest(BaseModel):
    stream_id: str = Field(min_length=1)

class SessionStartRequest(BaseModel):
    pass

class SessionStartResponse(BaseModel):
    session_id: str
    created_at: str

class SessionGet(BaseModel):
    session_id: str = Field(min_length=1)

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
    model_provider: Literal["gemini", "ollama"] | None = None
    gemini_model: str | None = Field(default=None, max_length=200)
    ollama_model: str | None = Field(default=None, max_length=200)
    gemini_embedding_model: str | None = Field(default=None, max_length=200)
    ollama_embedding_model: str | None = Field(default=None, max_length=200)
    agent_router_model: str | None = Field(default=None, max_length=200)
