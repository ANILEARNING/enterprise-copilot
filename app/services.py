from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

from autogen_core import CancellationToken

from .agents import (
    AgentOrchestrator, AutoGenOrchestrator, DeckBuilderOrchestrator, TurnPlan,
    default_agent_registry, default_skill_registry, plan_turn, reset_router_provider_cache,
)
from .artifacts import ArtifactStore
from .config import settings
from .db.engine import build_engine, build_session_factory
from .db.observability_service import ObservabilityService
from .document_store import DocumentStore
from .extraction import ExtractedBlock
from .hitl_agents import run_approved_code
from .memory import BUFFER_SIZE, CompactMemoryState, compact_history
from .observability import tracer
from .providers import (
    EmbeddingProvider, HashEmbeddingProvider, build_embedding_provider, build_provider,
    describe_embedding_config,
)
from .retrieval import (
    BM25Index, ParentChildChunk, chunk_blocks, chunk_text, compress_context, cosine_similarity,
    dedupe_results, embed, lexical_rerank_score, reciprocal_rank_fusion, redact_pii, tokenize,
)
from .sandbox import CodeSandbox, build_sandbox
from .session_store import SessionStore
from .skill_render import render_spec_as_chat_text
from .skills import SkillPackageError, SkillPackageStore, SkillRunService, run_generation_script
from .streaming import stream_chat
from .tenancy import configure_session_factory
from .vector_store import QdrantVectorStore, VectorPoint, VectorStore, build_vector_store

logger = logging.getLogger(__name__)


def _run_sync(coro):
    """Runs an async coroutine to completion from synchronous code — used
    only by RAGStore.__init__'s bootstrap-document seeding (see there),
    which has to be sync (no event loop exists yet the first time
    CopilotService() constructs it at plain app-startup/import time).

    asyncio.run() alone would suffice for THAT case, but RAGStore() is also
    constructed directly by ~20 tests, some of them themselves `async def`
    (pytest-asyncio) — i.e. already inside a running event loop, where
    asyncio.run() raises "cannot be called from a running event loop." This
    falls back to running the coroutine on a throwaway thread with its own
    fresh loop in that case, so RAGStore() behaves identically regardless of
    which context constructs it."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # no loop running — the common, cheap path
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

class HitlService:
    """Phase 0 HITL foundation: PENDING/WAITING_FOR_APPROVAL -> APPROVED/REJECTED -> COMPLETED/CANCELLED.

    Code execution itself runs via a real CodeExecutorAgent wrapping this
    app's own CodeSandbox (app/sandbox.py) — see app/hitl_agents.py — this
    class owns the approval workflow: the WAITING_FOR_APPROVAL/decided
    record bookkeeping and the REST surface, plus (await_decision below) a
    way for a live agent-mode turn to genuinely wait for a decision instead
    of only ever polling.

    Persisted to disk (data/hitl-requests/<request_id>.json), one file per
    request — same file-backed + in-memory-cache + write-through pattern as
    SessionStore (app/storage.py) and SkillRunService (app/skills.py), added
    so a WAITING_FOR_APPROVAL request survives a process restart instead of
    silently vanishing (a session's turn_checkpoint can reference a
    request_id that must still resolve after a restart — see
    app/storage.py's module docstring). _decision_futures stays in-memory
    only: a Future can't survive a restart regardless, so a live SSE wait
    that was in progress during a crash simply reads back as "queued"
    afterward, same as the already-existing non-live path.
    """

    def __init__(
        self, sandbox: CodeSandbox, artifacts: ArtifactStore | None = None, data_dir: Path | None = None,
        skill_store: "SkillPackageStore | None" = None,
    ):
        self.sandbox = sandbox
        # Needed only for kind == "deck_generation" records' decide() branch
        # (resolves record["skill_id"] -> the real SkillPackage to invoke
        # run_generation_script against). Optional/defaulted so existing
        # direct HitlService(sandbox) construction (tests, and any call site
        # that never submits a deck-generation request) keeps working —
        # decide() only dereferences this when it actually needs to.
        self._skill_store = skill_store
        self.requests: dict[str, dict] = {}
        # request_id -> every Future currently awaiting this request's
        # decision (see await_decision/await_human_decision in
        # app/hitl_agents.py). Empty for the overwhelmingly common case — a
        # decision made from the Agents & Tools tab with nothing live
        # waiting on it — decide() just finds nothing to resolve.
        self._decision_futures: dict[str, list[asyncio.Future]] = {}
        # Where an approved run's downloadable artifact_files (a generated
        # .html dashboard, a .pdf report — see app/sandbox.py) actually get
        # persisted — see decide() below. Optional/defaulted so existing
        # direct HitlService(sandbox) construction (tests) keeps working.
        self.artifacts = artifacts or ArtifactStore()
        # A FIXED default (settings.data_dir/hitl-requests when configured,
        # else this repo's real data/hitl-requests/) — same pattern as
        # SessionStore.DEFAULT_DATA_DIR (app/storage.py), deliberately NOT a
        # fresh uuid-per-instance directory like RAGStore.__init__'s bare-
        # construction default: the whole point of persisting HITL requests
        # is that the real CopilotService singleton's data survives a
        # process restart, and a restart constructs a brand new HitlService
        # with data_dir=None — a per-instance uuid default would silently
        # start that singleton's real, in-production storage over from
        # empty on every single restart, defeating this feature entirely.
        # Tests that construct HitlService(sandbox) directly and need
        # isolation from each other pass their own data_dir=tmp_path, same
        # convention already used by SessionStore/SkillPackageStore tests.
        self.data_dir = data_dir or (
            Path(settings.data_dir) / "hitl-requests" if settings.data_dir
            else Path(__file__).resolve().parent.parent / "data" / "hitl-requests"
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._load_requests()

    def _record_path(self, request_id: str) -> Path:
        return self.data_dir / f"{request_id}.json"

    def _persist(self, record: dict) -> None:
        try:
            self._record_path(record["request_id"]).write_text(json.dumps(record, indent=2), encoding="utf-8")
        except OSError as exc:
            # A disk-write failure shouldn't take the request down — the
            # in-memory copy still has this record, it just won't survive a
            # restart, same posture as SessionStore._write/SkillRunService._write.
            logger.warning("Could not persist HITL request %s: %s", record["request_id"], exc)
        self.requests[record["request_id"]] = record

    def _load_requests(self) -> None:
        if not self.data_dir.is_dir():
            return
        for path in sorted(self.data_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read HITL request file %s: %s", path, exc)
                continue
            self.requests[record["request_id"]] = record

    def submit_code_execution(self, code: str, session_id: str | None) -> dict:
        request_id = str(uuid4())
        record = {
            "request_id": request_id,
            "kind": "code_execution",
            "session_id": session_id,
            "code": code,
            "status": "WAITING_FOR_APPROVAL",
            "result": None,
            "downloadable_artifacts": [],  # populated by decide() only if approved code actually produced one
            "created_at": now_iso(),
            "decided_at": None,
        }
        self._persist(record)
        return record

    def submit_deck_generation(self, spec: dict, skill_id: str, session_id: str | None) -> dict:
        """Queues a drafted Deck Builder spec for approval — the Auto-generate
        OFF path (app/services.py:DeckBuilderService, app/models.py:
        ChatRequest.auto_generate). Mirrors submit_code_execution's shape;
        `deck_spec`/`skill_id` are what decide() needs below to actually run
        generate_pptx.py once approved, since (unlike code_execution) there's
        no arbitrary code here to re-extract from the request — the spec
        itself IS the payload."""
        request_id = str(uuid4())
        record = {
            "request_id": request_id,
            "kind": "deck_generation",
            "session_id": session_id,
            "deck_spec": spec,
            "skill_id": skill_id,
            "status": "WAITING_FOR_APPROVAL",
            "result": None,
            "downloadable_artifacts": [],
            "created_at": now_iso(),
            "decided_at": None,
        }
        self._persist(record)
        return record

    def await_decision(self, request_id: str) -> asyncio.Future:
        """Returns a Future that resolves to this request's decided record
        the moment decide() runs for it (from anywhere — the Agents & Tools
        tab's normal POST /api/hitl/decide is the overwhelmingly common
        caller). Safe to call more than once for the same request_id."""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._decision_futures.setdefault(request_id, []).append(future)
        return future

    async def decide(self, request_id: str, approved: bool) -> dict:
        if request_id not in self.requests:
            raise KeyError("HITL request not found")
        record = self.requests[request_id]
        if record["status"] != "WAITING_FOR_APPROVAL":
            raise ValueError("HITL request already decided")
        record["decided_at"] = now_iso()
        if not approved:
            record["status"] = "REJECTED"
        else:
            record["status"] = "APPROVED"
            if record["kind"] == "code_execution":
                result = await run_approved_code(self.sandbox, record["code"])
                record["result"] = result.public()
                # Any downloadable file the approved code produced (a
                # generated .html dashboard, a .pdf report) — persisted into
                # the artifact store here, the ONE place approved code's
                # output is captured, so the record carries a real,
                # servable view/download URL alongside the plain stdout
                # already in `result`. Named distinctly from
                # result["artifacts"] (ExecutionResult.public()'s plain
                # filename list, every artifact regardless of type) — this
                # is only the subset that was actually captured and stored.
                # Empty (the overwhelming common case — most executed code
                # has no downloadable output) means an empty list, not an error.
                record["downloadable_artifacts"] = [
                    self.artifacts.add(
                        filename, content,
                        session_id=record.get("session_id"), hitl_request_id=request_id,
                    ).public()
                    for filename, content in result.artifact_files.items()
                ]
                record["status"] = "COMPLETED"
            elif record["kind"] == "deck_generation":
                # Unlike code_execution, there's no arbitrary code to run
                # here — generate_pptx.py is a fixed, already-reviewed
                # script; the only thing this approval actually gates is
                # WHEN it runs, not whether the script itself is safe to
                # execute (same category as every other skill's generation
                # script, none of which are HITL-gated at all — see
                # DeckBuilderService for the Auto-generate ON path that
                # skips this record/approval entirely).
                try:
                    skill = self._skill_store.get(record["skill_id"])
                    output_paths = run_generation_script(skill, record["deck_spec"])
                    record["downloadable_artifacts"] = [
                        self.artifacts.add(
                            f"{skill.name}.{skill.output}", output_paths[0].read_bytes(),
                            session_id=record.get("session_id"), hitl_request_id=request_id,
                        ).public()
                    ]
                    record["status"] = "COMPLETED"
                except (SkillPackageError, KeyError, OSError) as exc:
                    # Approval was already granted — a generation failure is
                    # a result detail to show the user, not a reason to undo
                    # the decision or leave the request stuck WAITING.
                    record["status"] = "COMPLETED"
                    record["result"] = {"ok": False, "error": str(exc)}
        self._persist(record)
        for future in self._decision_futures.pop(request_id, []):
            if not future.done():
                future.set_result(record)
        return record

    def list(self) -> list[dict]:
        return list(self.requests.values())

    def get(self, request_id: str) -> dict:
        if request_id not in self.requests:
            raise KeyError("HITL request not found")
        return self.requests[request_id]

class GuardrailService:
    """Phase 0 guardrails: prompt-injection screening, PII detection with
    redaction, a toxic/unsafe-content policy, and sensitive-data (secrets)
    filtering — run before the model sees a request and again before the
    reply reaches the user. Every check returns structured findings (not
    just allow/block) so the UI can show exactly what guardrail activity
    happened on a given turn; see docs/guardrails.md and
    static/app.js:guardrailActivityHtml for how this surfaces.

    Detection here is pattern/regex-based by design, matching the rest of
    v1's "always-available, no external dependency" posture (same reasoning
    as the hash embedder in app/retrieval.py) — swappable later for a real
    PII/toxicity model behind this same method contract."""

    BLOCKED_PATTERNS = (
        "ignore previous instructions",
        "reveal system prompt",
        "show me your api key",
        "print environment variables",
    )

    # --- Sensitive data / secrets: config-shaped key=value & token literals
    _SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
        ("api_key", re.compile(r"(?i)\b(api[_-]?key|apikey)\s*[:=]\s*\S+")),
        ("password", re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+")),
        ("bearer_token", re.compile(r"(?i)\bbearer\s+[a-z0-9._-]{10,}")),
        ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
        # Provider-shaped secret literals — catches a pasted/echoed real key
        # even without a "key=" label in front of it.
        ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
        ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    )

    # --- Toxic / unsafe content: keyword-family policy, not an ML classifier.
    # Grouped by category so findings say *what kind* of content tripped the
    # policy, never the matched phrase itself (matched_terms stays out of
    # every returned dict — see docs/guardrails.md's "never expose ... policy").
    _UNSAFE_TERM_GROUPS: dict[str, tuple[str, ...]] = {
        "violence": ("kill you", "murder", "how to build a bomb", "mass shooting"),
        "self_harm": ("kill myself", "end my life", "suicide method"),
        "hate_harassment": ("racial slur", "ethnic slur"),
        "illegal_activity": ("how to make meth", "buy stolen credit card", "hire a hacker to"),
    }

    def _redact_pii(self, text: str) -> tuple[str, list[dict]]:
        # Delegates to the shared primitive in app/retrieval.py — see that
        # module's "PII redaction" section for why it lives there (needed by
        # app/memory.py too, which can't import GuardrailService without a
        # circular import). Kept as a thin instance-method wrapper so
        # check_input/check_output below (and any other GuardrailService
        # caller) don't need to change.
        return redact_pii(text)

    def redact_context_pii(self, chunks: list[dict]) -> tuple[list[dict], dict]:
        """Redacts PII in each retrieved RAG chunk's `snippet` field — see
        AutoGenOrchestrator.run() (app/agents.py), which calls this on
        `sources` right after the existing check_context() indirect-
        prompt-injection screen and before they're joined into the prompt /
        returned as citations (OrchestrationResult.sources ->
        ChatResponse.sources). Unlike check_context, a chunk is NEVER
        dropped here — a PII-bearing snippet is still relevant grounding
        content once masked, so this redacts and keeps every chunk, in the
        same order, returning fresh dicts (the caller's list/dicts are never
        mutated in place).

        Returns (chunks_with_redacted_snippets, findings) where findings
        mirrors check_input/check_output's shape: {"pii": [{"category",
        "count"}, ...], "redacted_count": N} — N is how many chunks actually
        had something redacted (not how many PII matches total), so the UI
        can say "PII redacted in 2 retrieved chunk(s)."."""
        redacted_chunks: list[dict] = []
        all_pii: list[dict] = []
        redacted_count = 0
        for chunk in chunks:
            redacted_snippet, pii = self._redact_pii(chunk.get("snippet", ""))
            if pii:
                chunk = dict(chunk)
                chunk["snippet"] = redacted_snippet
                redacted_count += 1
                all_pii.extend(pii)
            redacted_chunks.append(chunk)
        merged: dict[str, int] = {}
        for finding in all_pii:
            merged[finding["category"]] = merged.get(finding["category"], 0) + finding["count"]
        findings = {"pii": [{"category": k, "count": v} for k, v in merged.items()], "redacted_count": redacted_count}
        return redacted_chunks, findings

    def _detect_secrets(self, text: str) -> list[dict]:
        findings = []
        for category, pattern in self._SECRET_PATTERNS:
            count = len(pattern.findall(text))
            if count:
                findings.append({"category": category, "count": count})
        return findings

    def _detect_unsafe_content(self, text: str) -> list[dict]:
        lowered = text.lower()
        findings = []
        for category, terms in self._UNSAFE_TERM_GROUPS.items():
            count = sum(1 for t in terms if t in lowered)
            if count:
                findings.append({"category": category, "count": count})
        return findings

    def _prompt_injection_rules(self, text: str) -> list[str]:
        lowered = text.lower()
        return [p for p in self.BLOCKED_PATTERNS if p in lowered]

    def check_input(self, text: str) -> dict:
        """Runs on the raw user message before it reaches the model.
        Blocks on prompt-injection indicators or an unsafe-content policy
        hit; PII is redacted (not blocked) so the model never sees the raw
        value but the turn still proceeds — see docs/guardrails.md."""
        injection_rules = self._prompt_injection_rules(text)
        unsafe = self._detect_unsafe_content(text)
        redacted_text, pii = self._redact_pii(text)
        secrets = self._detect_secrets(text)

        blocked = bool(injection_rules) or bool(unsafe)
        matched_rules = list(injection_rules) + [f"unsafe-content:{f['category']}" for f in unsafe]
        return {
            "allowed": not blocked,
            "phase": 0,
            "matched_rules": matched_rules,
            "message": "Input allowed" if not blocked else "Input blocked by Phase 0 guardrails",
            "pii": pii,
            "redacted_text": redacted_text if pii else None,
            "sensitive_data": secrets,
            "unsafe_content": unsafe,
        }

    def check_output(self, text: str) -> dict:
        """Runs on the model's reply before it's shown to the user. Secrets
        and PII are both redacted from the returned text (defense against
        the model echoing something sensitive it saw in context); an
        unsafe-content hit blocks the reply outright, same as check_input."""
        unsafe = self._detect_unsafe_content(text)
        secrets = self._detect_secrets(text)
        redacted_text, pii = self._redact_pii(text)
        # Secrets get masked too, using the same key=value span the pattern matched.
        for category, pattern in self._SECRET_PATTERNS:
            redacted_text = pattern.sub(f"[REDACTED_{category.upper()}]", redacted_text)

        blocked = bool(unsafe)
        matched_rules = [f"unsafe-content:{f['category']}" for f in unsafe]
        if secrets:
            matched_rules.append("sensitive-data-redacted")
        if pii:
            matched_rules.append("pii-redacted")
        return {
            "allowed": not blocked,
            "phase": 0,
            "matched_rules": matched_rules,
            "message": "Output allowed" if not blocked else "Output blocked by Phase 0 guardrails",
            "pii": pii,
            "redacted_text": redacted_text if (pii or secrets) else None,
            "sensitive_data": secrets,
            "unsafe_content": unsafe,
        }

    def check_context(self, chunks: list[dict]) -> dict:
        """Screens retrieved chunks for indirect prompt injection — a
        compromised/malicious document trying to override instructions —
        before they're placed into the model's context. Distinct from
        check_input (user message) and check_output (model response); see
        docs/rag.md."""
        flagged = [c["chunk_id"] for c in chunks if any(p in c["snippet"].lower() for p in self.BLOCKED_PATTERNS)]
        return {
            "allowed": not flagged,
            "phase": 0,
            "matched_rules": ["retrieved-context-injection"] if flagged else [],
            "flagged_chunk_ids": flagged,
            "message": "Context allowed" if not flagged
                       else f"{len(flagged)} retrieved chunk(s) blocked by Phase 0 guardrails",
        }


def _serialize_blocks(blocks: list[ExtractedBlock] | None) -> list[dict] | None:
    """ExtractedBlock -> plain JSON-serializable dict, for DocumentStore
    persistence (see its `blocks` param). None passes through unchanged —
    "no structural extraction available for this document," not an empty list."""
    if blocks is None:
        return None
    return [{"kind": b.kind, "text": b.text, "level": b.level} for b in blocks]


def _deserialize_blocks(raw: list[dict]) -> list[ExtractedBlock]:
    return [ExtractedBlock(kind=b["kind"], text=b["text"], level=b.get("level")) for b in raw]


class RAGStore:
    """Ingestion: extract(format-aware) -> clean -> chunk(structure-aware,
    parent-child) -> metadata -> embed -> index (Qdrant + BM25).
    Retrieval: understand -> hybrid retrieve (vector + BM25) -> RRF fusion ->
    rerank -> dedupe -> context compression -> citation. See docs/rag.md for
    the full design.

    Vectors live in a `VectorStore` (app/vector_store.py — Qdrant Cloud when
    configured, an in-memory cosine scan otherwise); document metadata (full
    text, filename, timestamps, per-chunk content hashes) lives in a
    `DocumentStore` (app/document_store.py, file-backed, survives a restart
    even though vectors don't unless Qdrant is configured); BM25 stays an
    in-memory index (`self.bm25`) since neither backend does lexical search.
    `self.chunk_meta` is a slim in-memory chunk_id -> citation-metadata cache
    (filename, chunk_index, parent_text, heading_path, tokens for BM25/
    rerank) — never the embedding vector itself, which is VectorStore's job
    alone — rebuilt from DocumentStore + a re-chunk pass at construction so
    a restart's first search doesn't need a Qdrant round-trip per citation.

    Embedding is behind `EmbeddingProvider` (app/providers.py) — the offline
    hash embedder by default, or a real model (Gemini/Ollama) in configured
    mode, with graceful fallback to the hash embedder on any failure.
    add/update/search are async because a real embedding call (and a real
    Qdrant call) is a network call.

    Public method surface (add/update/delete/list/get/public/search) is
    unchanged from v1 — AgentOrchestrator, routes, and the UI need zero
    changes to keep working, same boundary discipline as the AutoGen/MAF
    orchestrator swap.
    """

    VECTOR_TOP_K = 8
    BM25_TOP_K = 8
    RERANK_POOL = 8

    def __init__(
        self, embedding_provider: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None, data_dir=None,
    ):
        # Same settings.data_dir resolution CopilotService.__init__ does for
        # its own stores (SessionStore, SkillPackageStore, ...) — a bare
        # RAGStore() must still honor DATA_DIR (see conftest.py) rather than
        # silently falling through to DocumentStore's real repo-root default.
        #
        # A caller-omitted data_dir additionally gets its OWN fresh uuid-
        # named subdirectory each construction, rather than every bare
        # RAGStore() sharing one directory: the old in-memory-dict RAGStore
        # was naturally isolated per instance (a plain Python dict, never
        # persisted), and ~19 existing tests construct RAGStore() this way
        # expecting exactly that isolation. DocumentStore's real persistence
        # is what CopilotService's one long-lived singleton wants (it
        # explicitly resolves and passes its own fixed data_dir below), not
        # what a fresh ad-hoc RAGStore() in a test wants — those callers get
        # a throwaway directory, not a real shared one.
        if data_dir:
            resolved_data_dir = Path(data_dir)
        elif settings.data_dir:
            resolved_data_dir = Path(settings.data_dir) / "documents" / str(uuid4())
        else:
            resolved_data_dir = None
        self.documents = DocumentStore(data_dir=resolved_data_dir)
        # chunk_id -> {document_id, filename, chunk_index, tokens, parent_text,
        # heading_path, is_table, content_hash, embedding_provider, embedding_model}
        self.chunk_meta: dict[str, dict] = {}
        self.bm25 = BM25Index()
        self.embedding_provider = embedding_provider or HashEmbeddingProvider()
        self.vector_store = vector_store or build_vector_store()
        self.tenant_id = settings.rag_tenant_id
        # Last actual embed() outcome (ingestion or query) — see describe_embedding().
        self.last_embedding: dict | None = None

        # Bootstrap doc, seeded synchronously at construction time (no event
        # loop available yet — CopilotService() is constructed at plain
        # synchronous app-startup time). Which embedder seeds it matters more
        # than it looks: whichever embedding call upserts the FIRST vector
        # ever written to this vector_store determines that collection's
        # permanent dimension (see VectorStore._ensure_collection/Qdrant's
        # "vector dimension error" on any later mismatch — a Qdrant
        # collection can't hold two different vector sizes). For the
        # in-memory fallback that's harmless (InMemoryVectorStore tolerates
        # mixed dimensions per search, matching by length) so this trivial
        # internal doc doesn't warrant a real embedding call there — but for
        # a real, persistent store like Qdrant it must be seeded with the
        # SAME embedding_provider real documents will actually use, or every
        # later document add fails with a dimension mismatch the moment a
        # real provider embeds at a different size than the offline hash
        # embedder's fixed EMBEDDING_DIM. See _run_sync's docstring for why
        # this can't just be asyncio.run().
        bootstrap_embedding_provider = (
            self.embedding_provider if isinstance(self.vector_store, QdrantVectorStore) else HashEmbeddingProvider()
        )
        existing = self.documents.list()
        if not existing:
            content = "Enterprise Copilot knowledge base. Documents can be added, updated, deleted and retrieved."
            doc = self.documents.create("welcome.md", content, tenant_id=self.tenant_id)
            _run_sync(self._reindex(doc["document_id"], embedding_provider=bootstrap_embedding_provider))
        else:
            # A restart with documents already on disk (DocumentStore
            # persisted them, or Qdrant still has their vectors) — rebuild
            # chunk_meta/BM25 from what's stored so citations/BM25 work
            # immediately without waiting on a query to trigger it. Vectors
            # themselves are NOT re-embedded here (that would defeat the
            # whole point of Qdrant persisting them) — only the local
            # metadata cache is rebuilt, from each document's own
            # chunk_hashes, by re-chunking (cheap, no network) and matching
            # hashes rather than re-embedding.
            for doc in existing:
                self._rebuild_chunk_meta_from_document(doc)

    async def add(self, filename: str, content: str, blocks: list[ExtractedBlock] | None = None) -> dict:
        """`blocks`: the document's real structural blocks (see
        app/extraction.py:extract_document), when the caller has them —
        app/routes.py passes these through from ingestion so DOCX/HTML/MD
        headings and tables actually reach chunk_blocks' structure-aware
        chunking instead of being flattened away. None (every existing
        `rag.add(filename, text)` caller/test) falls back to flat
        paragraph-only blocks derived from `content` at reindex time —
        today's pre-structural-chunking behavior, unchanged."""
        doc = self.documents.create(filename, content, tenant_id=self.tenant_id, blocks=_serialize_blocks(blocks))
        await self._reindex(doc["document_id"])
        return self.public(doc)

    async def update(
        self, document_id: str, filename: str | None, content: str | None,
        blocks: list[ExtractedBlock] | None = None,
    ) -> dict:
        # blocks only applies alongside new content (see DocumentStore.update) —
        # a content-less update (e.g. a filename-only rename) has nothing new
        # to derive blocks from anyway.
        doc = self.documents.update(  # raises KeyError if unknown
            document_id, filename, content, blocks=_serialize_blocks(blocks) if content is not None else None,
        )
        await self._reindex(document_id)  # re-chunk/re-embed immediately — v1 has no separate reindex step
        return self.public(doc)

    async def delete(self, document_id: str) -> None:
        self.documents.get(document_id)  # raises KeyError if unknown, before any side effect
        stale_chunk_ids = self._drop_chunks(document_id)
        await self.vector_store.delete(stale_chunk_ids)
        self.documents.delete(document_id)

    def list(self) -> list[dict]:
        return [self.public(d) for d in self.documents.list()]

    def get(self, document_id: str) -> dict:
        # Single-document fetch includes content (needed to view/edit); list() stays
        # content-stripped since it's a summary view over potentially many documents.
        return dict(self.documents.get(document_id))

    def public(self, doc: dict) -> dict:
        result = dict(doc)
        result.pop("content", None)
        result.pop("chunk_hashes", None)
        result.pop("blocks", None)
        result["chunk_count"] = sum(1 for c in self.chunk_meta.values() if c["document_id"] == doc["document_id"])
        return result

    # --- ingestion: extract -> clean -> chunk -> metadata -> embed -> index ---

    def _resolve_blocks(self, doc: dict) -> list[ExtractedBlock]:
        """The blocks to feed chunk_blocks() for this document: its real
        persisted structural blocks (app/extraction.py, threaded through
        add/update — see their docstrings) when present, otherwise a flat
        paragraph-only fallback derived from chunk_text(content) — today's
        pre-structural-chunking behavior, for any document added via the
        plain `rag.add(filename, text)` path (no extraction step ran) or
        ingested before structural blocks existed at all."""
        if doc.get("blocks"):
            return _deserialize_blocks(doc["blocks"])
        if not doc["content"]:
            return []
        return [ExtractedBlock("paragraph", p) for p in chunk_text(doc["content"])]

    def _rebuild_chunk_meta_from_document(self, doc: dict) -> None:
        """Re-chunks a document's stored content/blocks (no network call —
        pure text processing) and matches each resulting chunk's
        content_hash against the document's persisted chunk_hashes to
        recover its chunk_id, so a restart's chunk_meta/BM25 cache lines up
        with whatever's actually in the vector store without re-embedding
        anything. A chunk whose hash isn't found (the persisted mapping
        predates this rebuild, or was hand-edited) gets a fresh chunk_id —
        its vector is orphaned in the store until the next real reindex,
        same graceful-degrade posture as everywhere else in this app."""
        chunks = chunk_blocks(self._resolve_blocks(doc))
        hash_to_chunk_id = {v: k for k, v in doc.get("chunk_hashes", {}).items()}
        for index, chunk in enumerate(chunks):
            chunk_id = hash_to_chunk_id.get(chunk.content_hash) or str(uuid4())
            self._register_chunk_meta(chunk_id, doc["document_id"], doc["filename"], index, chunk)

    def _register_chunk_meta(
        self, chunk_id: str, document_id: str, filename: str, index: int, chunk: ParentChildChunk,
        embedding_provider: str | None = None, embedding_model: str | None = None,
    ) -> None:
        # metadata: filename folded into the indexed/tokenized text (not the
        # stored/cited snippet) so retrieval can match on it, same as a real
        # chunk's source path.
        index_text = f"{filename} {chunk.text}"
        self.chunk_meta[chunk_id] = {
            "chunk_id": chunk_id, "document_id": document_id, "filename": filename,
            "chunk_index": index, "text": chunk.text, "tokens": tokenize(index_text),
            "parent_text": chunk.parent_text, "heading_path": chunk.heading_path,
            "is_table": chunk.is_table, "content_hash": chunk.content_hash,
            "embedding_provider": embedding_provider, "embedding_model": embedding_model,
        }
        self.bm25.add(chunk_id, self.chunk_meta[chunk_id]["tokens"])

    def _drop_chunks(self, document_id: str, chunk_ids: list[str] | None = None) -> list[str]:
        """Removes chunk_ids (or every chunk of document_id when chunk_ids
        is None — full-document delete) from chunk_meta and BM25, and
        returns the chunk_ids that were dropped so the caller can also
        remove them from the vector store (this method itself is sync and
        can't await that). Used both by delete() (whole document) and
        _reindex()'s incremental diff (just the chunks that changed)."""
        stale = chunk_ids if chunk_ids is not None else [
            cid for cid, c in self.chunk_meta.items() if c["document_id"] == document_id
        ]
        for chunk_id in stale:
            self.chunk_meta.pop(chunk_id, None)
            self.bm25.remove(chunk_id)
        return stale

    async def _reindex(self, document_id: str, embedding_provider: EmbeddingProvider | None = None) -> None:
        """Incremental reindex: re-chunk the document's current content,
        diff each resulting chunk's content_hash against what was indexed
        last time (DocumentStore's persisted chunk_hashes), and only
        embed+upsert chunks that are new or changed. Unchanged chunks keep
        their existing chunk_id/Qdrant point/vector completely untouched —
        this is the literal implementation of "if the user updates the doc
        just re-embed that part." A fresh document (no prior chunk_hashes)
        naturally has every chunk as "new," so this is also just `add`'s
        ingestion path with no special-casing needed."""
        provider = embedding_provider or self.embedding_provider
        doc = self.documents.get(document_id)
        # extract/chunk: real structural blocks when add/update supplied
        # them (app/extraction.py, via app/routes.py), else a flat
        # paragraph-only fallback — see _resolve_blocks.
        new_chunks = chunk_blocks(self._resolve_blocks(doc))

        old_hash_to_chunk_id = {v: k for k, v in doc.get("chunk_hashes", {}).items()}
        new_hashes = {c.content_hash for c in new_chunks}
        old_hashes = set(old_hash_to_chunk_id.keys())

        removed_hashes = old_hashes - new_hashes
        removed_chunk_ids = [old_hash_to_chunk_id[h] for h in removed_hashes]
        if removed_chunk_ids:
            self._drop_chunks(document_id, chunk_ids=removed_chunk_ids)
            await self.vector_store.delete(removed_chunk_ids)

        final_chunk_hashes: dict[str, str] = {}
        points: list[VectorPoint] = []
        for index, chunk in enumerate(new_chunks):
            existing_chunk_id = old_hash_to_chunk_id.get(chunk.content_hash)
            if existing_chunk_id is not None:
                # Unchanged chunk: keep its id, its metadata (chunk_index may
                # shift if earlier chunks were added/removed — cheap to
                # refresh, doesn't touch the vector), and crucially never
                # re-embed or re-upsert it.
                self._register_chunk_meta(
                    existing_chunk_id, document_id, doc["filename"], index, chunk,
                    embedding_provider=self.chunk_meta.get(existing_chunk_id, {}).get("embedding_provider"),
                    embedding_model=self.chunk_meta.get(existing_chunk_id, {}).get("embedding_model"),
                )
                final_chunk_hashes[existing_chunk_id] = chunk.content_hash
                continue

            chunk_id = str(uuid4())
            index_text = f"{doc['filename']} {chunk.text}"
            result = await provider.embed(index_text)
            self._register_chunk_meta(
                chunk_id, document_id, doc["filename"], index, chunk,
                embedding_provider=result.provider, embedding_model=result.model,
            )
            final_chunk_hashes[chunk_id] = chunk.content_hash
            points.append(VectorPoint(point_id=chunk_id, vector=result.vector, payload={
                "tenant_id": self.tenant_id, "document_id": document_id, "chunk_id": chunk_id,
                "filename": doc["filename"], "chunk_index": index, "parent_text": chunk.parent_text,
                "heading_path": chunk.heading_path, "is_table": chunk.is_table,
                "content_hash": chunk.content_hash, "embedding_provider": result.provider,
                "embedding_model": result.model,
            }))
            # Remembered so RAGStore.describe_embedding() can report what
            # actually embedded the most recent document, not just what's
            # configured — the two can differ if a provider fell back.
            self.last_embedding = {"provider": result.provider, "model": result.model, "used_fallback": result.used_fallback}

        if points:
            await self.vector_store.upsert(points)
        self.documents.set_chunk_hashes(document_id, final_chunk_hashes)

    # --- retrieval: understand -> hybrid retrieve -> rerank -> context -------

    async def search(self, query: str, limit: int = 3) -> list[dict]:
        if not self.chunk_meta:
            return []
        query_tokens = tokenize(query)  # understand: normalize the question
        if not query_tokens:
            return []
        query_result = await self.embedding_provider.embed(query)
        query_vector = query_result.vector
        self.last_embedding = {
            "provider": query_result.provider, "model": query_result.model,
            "used_fallback": query_result.used_fallback,
        }

        # Vector leg: delegates to VectorStore (Qdrant's own ANN search, or
        # the in-memory cosine scan) — the dimension-mismatch guard that
        # used to live here (a chunk embedded by a different provider than
        # the query lives in an incomparable vector space) is now
        # InMemoryVectorStore's job; Qdrant enforces a single vector size
        # per collection by construction, so the same class of mismatch
        # simply can't occur there. BM25 still covers any chunk regardless.
        scored_points = await self.vector_store.search(query_vector, tenant_id=self.tenant_id, limit=self.VECTOR_TOP_K)
        vector_scores = {p.point_id: p.score for p in scored_points}
        vector_ranked = [p.point_id for p in scored_points]

        bm25_scores = self.bm25.search(query_tokens)
        bm25_ranked = [cid for cid, _ in sorted(bm25_scores.items(), key=lambda p: p[1], reverse=True)][: self.BM25_TOP_K]

        # hybrid retrieve: fuse the vector leg and the BM25 leg
        fused = reciprocal_rank_fusion([vector_ranked, bm25_ranked])
        if not fused:
            return []
        # Only chunk ids this process actually knows the metadata for (a
        # restart-recovered chunk_meta might lag a Qdrant point briefly, or
        # vice versa mid-reindex) — never index into chunk_meta with an id
        # it doesn't have.
        fused = {cid: score for cid, score in fused.items() if cid in self.chunk_meta}
        candidates = sorted(fused.items(), key=lambda p: p[1], reverse=True)[: self.RERANK_POOL]

        # rerank: independent lexical-coverage signal over the fused candidate pool
        reranked = sorted(
            ((lexical_rerank_score(query_tokens, self.chunk_meta[cid]["tokens"]), hybrid_score, cid)
             for cid, hybrid_score in candidates),
            reverse=True,
        )

        results = []
        for rerank_score, hybrid_score, chunk_id in reranked:
            chunk = self.chunk_meta[chunk_id]
            results.append({
                "document_id": chunk["document_id"],
                "filename": chunk["filename"],
                "chunk_id": chunk_id,
                "chunk_index": chunk["chunk_index"],
                "snippet": chunk["text"][:400].strip(),
                "parent_text": chunk["parent_text"],
                "heading_path": chunk["heading_path"],
                "is_table": chunk["is_table"],
                "content_hash": chunk["content_hash"],
                "tokens": chunk["tokens"],
                "vector_score": round(vector_scores.get(chunk_id, 0.0), 4),
                "bm25_score": round(bm25_scores.get(chunk_id, 0.0), 4),
                "rerank_score": round(rerank_score, 4),
                "embedding_provider": chunk["embedding_provider"],
                "embedding_model": chunk.get("embedding_model"),
            })

        # dedupe: drop near-duplicate chunks (parent-child overlap can
        # surface two children of the same section both scoring well)
        results = dedupe_results(results)
        # ordering + context compression: rerank-sorted, budget-capped —
        # this is also where the final top-`limit` cut happens, AFTER dedup
        # (deduping post-limit would silently return fewer than `limit`
        # results even when enough genuinely distinct chunks existed).
        results = compress_context(results[:limit], settings.rag_context_token_budget)
        return results

    def describe_embedding(self) -> dict:
        """RAG status for the UI: what embedding is configured (from
        settings, static) plus what actually ran the most recent ingest/query
        (dynamic — can differ if a provider fell back mid-session)."""
        return {"configured": describe_embedding_config(), "last_used": self.last_embedding}

    async def describe_vector_store(self) -> dict:
        """Vector-store backend status for the UI (Qdrant Cloud vs.
        in-memory fallback, reachability, point count) — see
        VectorStore.health()."""
        return await self.vector_store.health()


async def _deck_emit(on_event, event: dict) -> None:
    if on_event is not None:
        await on_event(event)


class DeckBuilderService:
    """Owns the Deck Builder session-state machinery — a conversational,
    web-search-capable alternative to SkillRunService's fixed-question flow,
    specifically for the "pptx" skill (app/agents.py:DeckBuilderOrchestrator
    does the actual Magentic-One run; this class owns what happens around
    it: accumulating the running task_brief across chat turns, and the
    Auto-generate ON (generate immediately) vs. OFF (queue through
    HitlService) split — see app/models.py:ChatRequest.auto_generate).

    Mirrors SkillRunService's shape (start/continue, a small owned bit of
    session state) but for an open-ended loop rather than a fixed question
    list — deliberately NOT built on SkillRunSession/pending_skill_run,
    which is shaped for exactly-linear Q&A, a poor fit here.
    """

    def __init__(self, skill_packages: SkillPackageStore, hitl: HitlService,
                 artifacts: ArtifactStore, sessions: SessionStore):
        self.skill_packages = skill_packages
        self.hitl = hitl
        self.artifacts = artifacts
        self.sessions = sessions

    def _orchestrator_for(self, skill) -> DeckBuilderOrchestrator:
        return DeckBuilderOrchestrator(skill)

    async def _run_and_handle(
        self, session_id: str, skill_id: str, task_brief: str, auto_generate: bool,
        on_event=None, delivery: str = "file",
    ) -> tuple[str, dict]:
        """Runs one Deck Builder turn and applies its result: a clarifying
        question keeps `pending_deck_builder` set (phase stays "clarifying"),
        a ready spec either generates immediately (auto_generate=True) or is
        queued through HitlService (False, the default) — unless `delivery`
        is "inline" and app/skill_render.py has a renderer for this skill, in
        which case the spec is rendered as chat text instead and neither
        generation nor HITL ever runs (see TurnPlan.delivery, app/agents.py).
        Always clears `pending_deck_builder` except on the clarifying-question
        path. Returns (chat response text, meta dict for CopilotService.chat's
        `meta`/`skill_run`-equivalent surface). That meta always carries
        `hitl_pending` — the ids of any approval this turn actually queued.
        It is what tells the UI an approval is waiting: the frontend attaches
        the inline approve/reject card and refreshes the Pending approvals
        panel off `ChatResponse.hitl_pending` (static/app.js, sendMessage) and
        does neither when it's empty. Reporting `[]` here while
        submit_deck_generation had just created a record left the queued deck
        invisible until something else happened to reload the queue — opening
        the Agents &amp; Tools tab, which calls loadHitlRequests() and populates
        both panels at once."""
        skill = self.skill_packages.get(skill_id)
        orchestrator = self._orchestrator_for(skill)
        result = await orchestrator.run_turn(task_brief, {"session_id": session_id}, on_event=on_event)
        # Carried on every return path below: research the deck actually read
        # is worth citing whether the deck was generated, queued, rendered
        # inline, or is still being clarified.
        web_sources = result.web_sources

        if result.spec is None:
            # Still clarifying — keep the conversation going. `delivery` is
            # persisted alongside auto_generate so a multi-turn clarification
            # keeps the user's original choice once a spec is finally ready,
            # same reasoning as auto_generate's own persistence here.
            await self.sessions.set_field(session_id, "pending_deck_builder", {
                "skill_id": skill_id, "phase": "clarifying", "auto_generate": auto_generate,
                "delivery": delivery, "task_brief": task_brief, "hitl_request_id": None,
            })
            return result.clarifying_text or "Could you tell me a bit more about the deck you want?", {
                "deck_builder": {"phase": "clarifying"}, "downloadable_artifacts": [],
                "web_sources": web_sources, "hitl_pending": [],
            }

        if delivery == "inline":
            rendered = render_spec_as_chat_text(skill_id, result.spec)
            if rendered is not None:
                await self.sessions.set_field(session_id, "pending_deck_builder", None)
                await self.sessions.set_field(session_id, "last_deck_spec", result.spec)
                return rendered, {
                    "deck_builder": {"phase": "completed_inline"}, "downloadable_artifacts": [],
                    "web_sources": web_sources, "hitl_pending": [],
                }
            # No renderer for this skill yet (see app/skill_render.py) — fall
            # through to the file path exactly as if delivery had been "file"
            # all along, rather than silently producing nothing.

        if auto_generate:
            try:
                output_paths = run_generation_script(skill, result.spec)
                artifact = self.artifacts.add(
                    f"{skill.name}.{skill.output}", output_paths[0].read_bytes(), session_id=session_id,
                ).public()
                await self.sessions.set_field(session_id, "pending_deck_builder", None)
                await self.sessions.set_field(session_id, "last_deck_spec", result.spec)
                return (
                    f"Done! Generated **{artifact['filename']}** — view or download it below.",
                    {"deck_builder": {"phase": "completed"}, "downloadable_artifacts": [artifact],
                     "web_sources": web_sources, "hitl_pending": []},
                )
            except SkillPackageError as exc:
                await self.sessions.set_field(session_id, "pending_deck_builder", None)
                return f"Couldn't generate the deck: {exc}", {
                    "deck_builder": {"phase": "failed"}, "downloadable_artifacts": [],
                    "web_sources": web_sources, "hitl_pending": [],
                }

        # Auto-generate OFF (default): queue for approval, don't run yet.
        record = self.hitl.submit_deck_generation(result.spec, skill_id, session_id)
        await self.sessions.set_field(session_id, "pending_deck_builder", None)
        await self.sessions.set_field(session_id, "last_deck_spec", result.spec)
        await _deck_emit(on_event, {
            "stage": "queued_for_approval",
            "label": "Deck spec ready — waiting for your approval in Agents & Tools.",
            "request_id": record["request_id"],
        })
        return (
            f"I've drafted the deck (**{result.spec.get('title', 'Untitled')}**) and queued it for your "
            "approval — review and generate it right here, or from Pending approvals.",
            {"deck_builder": {"phase": "awaiting_approval", "hitl_request_id": record["request_id"]},
             "downloadable_artifacts": [], "web_sources": web_sources,
             "hitl_pending": [record["request_id"]]},
        )

    async def start(self, session_id: str, skill, message: str, auto_generate: bool,
                    on_event=None, delivery: str = "file") -> tuple[str, dict]:
        session = await self.sessions.get(session_id)
        last_spec = session.get("last_deck_spec")
        task_brief = (
            f"The user previously had this deck generated:\n{json.dumps(last_spec)}\n\n"
            f"They now want this change: {message}"
        ) if last_spec else message
        return await self._run_and_handle(
            session_id, skill.skill_id, task_brief, auto_generate, on_event=on_event, delivery=delivery,
        )

    async def continue_turn(self, session_id: str, pending: dict, message: str, on_event=None) -> tuple[str, dict]:
        task_brief = f"{pending['task_brief']}\n\nUser: {message}"
        return await self._run_and_handle(
            session_id, pending["skill_id"], task_brief, pending["auto_generate"], on_event=on_event,
            # .get, not ["delivery"]: a pending_deck_builder written before this
            # field existed must not KeyError mid-conversation.
            delivery=pending.get("delivery", "file"),
        )


class CopilotService:
    def __init__(self, data_dir: Path | str | None = None):
        """`data_dir`: root directory for every file-backed store this
        service owns (SessionStore, SkillPackageStore, SkillRunService —
        each gets its own named subdirectory below it). Resolution order:
        the explicit argument, then settings.data_dir (DATA_DIR env var —
        see app/config.py, set by the repo-root conftest.py to a throwaway
        temp dir so test runs never write into this repo's real data/), then None
        (each store falls back to its own real repo-root data/<name>/
        default — normal production/local-dev behavior, unchanged)."""
        resolved_dir = Path(data_dir) if data_dir else (Path(settings.data_dir) if settings.data_dir else None)
        self.provider = build_provider()
        self.embedding_provider = build_embedding_provider()
        self.guardrails = GuardrailService()
        self.rag = RAGStore(
            embedding_provider=self.embedding_provider,
            data_dir=resolved_dir / "documents" if resolved_dir else None,
        )
        # Sessions live in Upstash Redis now, not under data_dir — see
        # app/session_store.py's module docstring for why chat history never
        # became a Postgres table pair either. settings.upstash_redis_rest_*
        # come from UPSTASH_REDIS_REST_URL/_TOKEN; a blank value fails the
        # first real session call rather than at construction time, matching
        # the rest of this method's "let the actual missing-config error
        # surface" posture for provider/store setup that already tolerates
        # being unconfigured in tests (see build_provider()/build_vector_store()
        # above).
        self.sessions = SessionStore(
            url=settings.upstash_redis_rest_url, token=settings.upstash_redis_rest_token,
        )
        self.sandbox = build_sandbox()
        self.artifacts = ArtifactStore()
        self.skill_packages = SkillPackageStore(data_dir=resolved_dir / "skills" if resolved_dir else None)
        self.hitl = HitlService(
            self.sandbox, artifacts=self.artifacts,
            data_dir=resolved_dir / "hitl-requests" if resolved_dir else None,
            skill_store=self.skill_packages,
        )
        self.agent_registry = default_agent_registry()
        self.skill_registry = default_skill_registry()
        self.orchestrator: AgentOrchestrator = AutoGenOrchestrator(
            self.provider, self.agent_registry, self.skill_registry, self.rag, self.hitl,
            guardrails=self.guardrails,
        )
        self.skill_runs = SkillRunService(
            self.skill_packages, self.provider, data_dir=resolved_dir / "skill-runs" if resolved_dir else None,
        )
        # Deck Builder: a conversational, research-capable alternative to the
        # fixed-question skill-run flow, specifically for the "pptx" skill —
        # see CopilotService.chat()'s routing and _start_deck_builder/
        # _continue_deck_builder below. DeckBuilderOrchestrator itself is
        # stateless (app/agents.py) — this service owns the session-state
        # machinery (pending_deck_builder) and the HITL/auto-generate split.
        self.deck_builder = DeckBuilderService(self.skill_packages, self.hitl, self.artifacts, self.sessions)

        # Postgres engine + session factory — the one composition root every
        # DB-backed repository (app/db/*_repository.py) is meant to share
        # (see app/db/engine.py's module docstring). Built lazily-tolerant,
        # not lazily-deferred: unlike sessions/blob storage above, a missing
        # DATABASE_URL here does NOT fail CopilotService() construction —
        # auth/observability simply aren't usable until it's set, same as
        # every other optional-until-configured piece in this constructor,
        # so importing this module (e.g. from a test that never touches
        # either) never requires Postgres.
        if settings.database_url:
            self.db_engine = build_engine(settings.database_url)
            self.db_session_factory = build_session_factory(self.db_engine)
            configure_session_factory(self.db_session_factory)
        else:
            self.db_engine = None
            self.db_session_factory = None
        # Persists guardrail_events/traces/model_calls for every chat turn
        # (see chat()/chat_stream() below) — a thin wrapper over the
        # observability/cost repository layer, itself a no-op when
        # db_session_factory is None (see ObservabilityService.enabled).
        self.observability = ObservabilityService(self.db_session_factory)

    def reload_providers(self) -> None:
        """Rebuilds every provider instance derived from `settings` after a
        runtime settings change (see POST /api/settings/models,
        docs/runtime-settings.md) — MODEL_PROVIDER, GEMINI_MODEL,
        OLLAMA_MODEL, *_EMBEDDING_MODEL, or AGENT_ROUTER_MODEL.

        Everywhere else in this app already reads `settings.X` fresh on
        every call (build_streaming_model_client, list_available_models,
        the router prompt in AgentRegistry.select_llm, ...) — this only
        needs to rebuild the handful of instances CopilotService caches at
        construction time and hands out by reference: self.provider,
        self.embedding_provider, and everything holding onto either of
        those (self.orchestrator, self.skill_runs, self.rag). Rebuilding
        RAGStore itself would wipe every indexed document, so its
        embedding_provider is swapped in place instead — same object,
        fresh provider.

        Called once, synchronously, right after a settings write — takes
        effect on the very next request; nothing needs a process restart.
        """
        self.provider = build_provider()
        self.embedding_provider = build_embedding_provider()
        self.rag.embedding_provider = self.embedding_provider
        self.orchestrator.provider = self.provider
        self.skill_runs.provider = self.provider
        reset_router_provider_cache()

    async def chat(
        self, message: str, session_id: str | None,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
        images: list[dict] | None = None, allow_live_hitl_wait: bool = False,
        auto_generate: bool = False,
        plan: TurnPlan | None = None,
    ) -> dict:
        """`on_event`, when given, receives live progress events for the
        multi-step paths below (skill drafting/generation, agent-mode
        retrieval/thinking) as they actually happen — see CopilotService.
        chat_stream, which is the only caller that passes one. None (the
        default, and every non-streaming caller) means those calls are
        no-ops, so this costs nothing outside the streaming path.

        Every one of those same events, plus each completed model call, is
        also mirrored to Langfuse (app/observability.py) as one span/event/
        generation nested under a single per-turn trace — a no-op unless
        Langfuse is configured, so this costs nothing when it isn't.

        `images`: optional multimodal attachments for this turn (see
        app/models.py:ImageAttachment).

        There are no more Agent mode / Web search toggles: every turn gets
        full agent capability unconditionally (multi-step tool use, live web
        research whenever Tavily is configured, this organisation's own
        indexed documents) — see app/agents.py:plan_turn. The only remaining
        turn-level control is `auto_generate` below, kept as a deliberate
        safety gate rather than made autonomous.

        `auto_generate`: whether a finished deck spec may generate without a
        human approving it first. False (default): the spec is queued through
        HitlService. True: generates immediately. Deck Builder only — the
        fixed-question skill flow has its own download step and no HITL gate
        for this to lift.

        `plan`: an already-computed routing decision (app/agents.py:TurnPlan).
        Only chat_stream passes one — it has to know the route before it can
        choose between token streaming and delegating here, so it computes the
        plan once and hands it over rather than paying for a second router
        call that could also disagree with the first. None (every other
        caller, including the plain /api/chat route) means "decide here".

        `allow_live_hitl_wait`: only CopilotService.chat_stream's real-time
        SSE delegate branch sets this — a long-lived connection that can
        genuinely wait for a human's HITL decision (app/hitl_agents.py) mid-
        turn. The plain /api/chat route leaves this False: a bare POST can't
        sensibly stay open for arbitrary approval time, so its coding-agent
        turns keep the original "queue and return immediately" behavior.

        Context management (buffered + compact-summary memory, see
        app/memory.py) persists across restarts via
        SessionStore.set_field(session_id, "memory_state", ...) — loaded
        once at the top of this method, updated by whichever branch below
        actually talks to a model, and always persisted before returning.

        This turn's progress is also mirrored, stage by stage, into the
        session's "turn_checkpoint" field (see emit() below and
        app/storage.py's module docstring) — a step-boundary checkpoint that
        survives a process restart, cleared again the instant this method
        returns on ANY path. It's advisory only: it never gates this or any
        future call to chat(), it just lets a resumed UI say "your last turn
        didn't finish, here's where it got to.\""""
        session = await self.sessions.get_or_create(session_id)
        sid = session["session_id"]
        history = list(session["messages"])  # snapshot before appending this turn
        memory_state = CompactMemoryState.from_dict(session.get("memory_state"))
        turn_id = str(uuid4())

        # PII/secrets are redacted BEFORE anything (session history, the trace,
        # the model prompt) ever stores or forwards the raw value — check_input
        # runs first and message is reassigned to its redacted_text when it
        # found anything, so every downstream use in this method already sees
        # the safe version. See GuardrailService.check_input.
        input_check = self.guardrails.check_input(message)
        await self.observability.record_guardrail_check(
            stage="check_input", findings=input_check, session_id=sid, turn_id=turn_id,
        )
        if input_check["redacted_text"] is not None:
            message = input_check["redacted_text"]
        await self.sessions.append(sid, "user", message)

        # Populated by emit() below on every "model_call" progress event —
        # what record_turn (this method's finally block) writes to
        # model_calls, one row per completed AIProvider.complete() call
        # this turn actually made (usually one; a tool-calling agent turn
        # or a routed turn can make more). started_at/turn_status/turn_error
        # are this same finally block's other inputs, set on whichever
        # return/exception path this method actually takes.
        collected_model_calls: list[dict] = []
        turn_started_at = datetime.now(timezone.utc)
        turn_status = "ok"
        turn_error: str | None = None
        turn_route: str | None = None

        try:
            async with tracer.turn(
                "chat_turn", input=message, metadata={"session_id": sid},
            ) as turn:
                async def emit(event: dict) -> None:
                    if event.get("stage") == "model_call":
                        turn.generation(
                            event.get("provider") or "model", model=event.get("model"), provider=event.get("provider"),
                            input=event.get("prompt_preview"), output=event.get("response_preview"),
                            metadata={"used_fallback": event.get("used_fallback"), "tool_calls": event.get("tool_calls")},
                        )
                        prompt_tokens, completion_tokens = event.get("prompt_tokens"), event.get("completion_tokens")
                        collected_model_calls.append({
                            "call_type": "chat_completion", "provider": event.get("provider") or "unknown",
                            "model": event.get("model"), "used_fallback": bool(event.get("used_fallback")),
                            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                            "total_tokens": (
                                prompt_tokens + completion_tokens
                                if prompt_tokens is not None and completion_tokens is not None else None
                            ),
                            # cost_micros intentionally omitted (left NULL by
                            # ModelCallRepository.record's own default) — no
                            # model_pricing rate lookup is wired in yet (see
                            # ModelPricingRepository, app/db/observability_repository.py);
                            # pricing every call here would mean guessing a
                            # rate rather than reading a real one.
                        })
                    else:
                        turn.event(event.get("stage") or "progress", **{k: v for k, v in event.items() if k != "stage"})
                    stage = event.get("stage")
                    if event.get("agent"):
                        nonlocal turn_route
                        turn_route = event["agent"]
                    if stage:
                        # Merge, don't replace — later stages (e.g. code_queued
                        # after agent_selected) add fields without discarding
                        # what an earlier stage already recorded this turn.
                        existing_session = await self.sessions.get(sid)
                        existing = existing_session.get("turn_checkpoint") or {}
                        checkpoint = {
                            **({} if existing.get("turn_id") != turn_id else existing),
                            "turn_id": turn_id, "stage": stage, "label": event.get("label"),
                            "started_at": existing.get("started_at") or now_iso(), "updated_at": now_iso(),
                            "user_message": message,
                            "agent": event.get("agent", existing.get("agent")),
                            "skills": event.get("tools", existing.get("skills")),
                            "sources_count": event.get("count", existing.get("sources_count")),
                            "hitl_request_id": event.get("request_id", existing.get("hitl_request_id")),
                            "tool_calls": event.get("tool_calls", existing.get("tool_calls")),
                        }
                        await self.sessions.set_field(sid, "turn_checkpoint", checkpoint)
                    if on_event is not None:
                        await on_event(event)

                if not input_check["allowed"]:
                    response = input_check["message"]
                    await self.sessions.append(sid, "assistant", response)
                    await self.sessions.set_field(sid, "turn_checkpoint", None)
                    turn.set_output(response, metadata={"blocked": "input"})
                    turn_status = "blocked"
                    return {
                        "response": response, "session_id": sid,
                        "guardrails": {"input": input_check, "context": None, "output": None},
                        "agent": None, "skills": [], "provider": None, "model": None,
                        "used_fallback": False, "hitl_pending": [], "sources": [], "skill_run": None,
                        "tool_calls": [], "web_sources": [], "downloadable_artifacts": [],
                        # Blocked before routing ever ran — there is no decision to report.
                        "routing": None,
                    }

                # Routing. An in-progress exchange always continues first — a
                # half-finished skill Q&A or deck clarification is a conversation
                # already underway, not a new request to classify. Only once nothing
                # is pending does plan_turn() decide what this message should do.
                #
                # No more Agent mode / Web search toggles gating this — every turn
                # gets full agent capability unconditionally (see app/agents.py:
                # plan_turn's header comment). The only remaining permission is
                # auto_generate: may a finished file spec generate without HITL
                # approval.
                skill_run_meta = None
                routing: dict | None = None
                pending_skill_run = session.get("pending_skill_run")
                pending_deck_builder = session.get("pending_deck_builder")
                if pending_skill_run:
                    response, skill_run_meta = await self._continue_chat_skill_run(
                        sid, pending_skill_run, message, on_event=emit,
                    )
                    context_check = None
                    meta = {
                        "agent": "skill", "skills": [pending_skill_run["skill_id"]],
                        # Only the finish turn actually calls a provider (to draft the
                        # spec) — skill_run_meta carries provider/used_fallback then
                        # (see SkillRunSession.public()); mid-flow Q&A turns leave
                        # both unset, which correctly renders as "no model call" in
                        # the UI rather than a misleading "mock".
                        "provider": (skill_run_meta or {}).get("provider"), "model": None,
                        "used_fallback": (skill_run_meta or {}).get("used_fallback", False),
                        "hitl_pending": [], "sources": [], "tool_calls": [], "web_sources": [],
                        "downloadable_artifacts": [],
                    }
                elif pending_deck_builder:
                    response, deck_meta = await self.deck_builder.continue_turn(
                        sid, pending_deck_builder, message, on_event=emit,
                    )
                    context_check = None
                    meta = {
                        "agent": "deck-builder", "skills": ["pptx"], "provider": None, "model": None,
                        "used_fallback": False, "hitl_pending": deck_meta["hitl_pending"],
                        "sources": [], "tool_calls": [],
                        "web_sources": deck_meta["web_sources"],
                        "downloadable_artifacts": deck_meta["downloadable_artifacts"],
                    }
                else:
                    if plan is None:
                        keyword_match = self.skill_packages.select_for_chat(message)
                        plan = await plan_turn(
                            message, skills=self.skill_packages.list(),
                            keyword_match=keyword_match.public() if keyword_match else None,
                            provider=self.provider,
                        )
                    routing = plan.public()
                    await emit({
                        "stage": "routing",
                        "label": f"Routing to {plan.route}" + (f" — {plan.reason}." if plan.reason else "."),
                        **routing,
                    })
                    if plan.route == "deck":
                        response, deck_meta = await self.deck_builder.start(
                            sid, self.skill_packages.get(plan.skill_id), message, auto_generate,
                            on_event=emit, delivery=plan.delivery,
                        )
                        context_check = None
                        meta = {
                            "agent": "deck-builder", "skills": ["pptx"], "provider": None, "model": None,
                            "used_fallback": False, "hitl_pending": deck_meta["hitl_pending"],
                            "sources": [], "tool_calls": [],
                            "web_sources": deck_meta["web_sources"],
                            "downloadable_artifacts": deck_meta["downloadable_artifacts"],
                        }
                    elif plan.route == "skill":
                        response, skill_run_meta = await self._start_chat_skill_run(
                            sid, self.skill_packages.get(plan.skill_id), on_event=emit,
                            delivery=plan.delivery,
                        )
                        context_check = None
                        meta = {
                            "agent": "skill", "skills": [plan.skill_id],
                            "provider": (skill_run_meta or {}).get("provider"), "model": None,
                            "used_fallback": (skill_run_meta or {}).get("used_fallback", False),
                            "hitl_pending": [], "sources": [], "tool_calls": [], "web_sources": [],
                            "downloadable_artifacts": [],
                        }
                    elif plan.route == "agent":
                        result = await self.orchestrator.run(message, {
                            "session_id": sid, "history": history, "images": images,
                            "memory_state": memory_state.to_dict(), "allow_live_hitl_wait": allow_live_hitl_wait,
                        }, on_event=emit)
                        response = result.text
                        context_check = result.context_guardrail
                        if result.memory_state is not None:
                            memory_state = CompactMemoryState.from_dict(result.memory_state)
                        meta = {
                            "agent": result.agent, "skills": result.skills, "provider": result.provider,
                            "model": result.model,
                            "used_fallback": result.used_fallback, "hitl_pending": result.hitl_pending,
                            "sources": result.sources, "tool_calls": result.tool_calls,
                            "web_sources": result.web_sources,
                            "downloadable_artifacts": result.downloadable_artifacts,
                        }
                    else:
                        # plan.route == "direct" — router judged this message needs no
                        # augmentation (no tools, no RAG grounding). A plain model call,
                        # so there's no retrieved context for the context guardrail.
                        context_check = None
                        await emit({"stage": "thinking", "label": "Thinking…"})
                        compacted, memory_state = await compact_history(self.provider, history, memory_state, BUFFER_SIZE)
                        provider_result = await self.provider.complete(message, compacted, images=images)
                        response = provider_result.text
                        await emit({
                            "stage": "model_call",
                            "label": f"Answered ({provider_result.provider}"
                                     f"{f'/{provider_result.model}' if provider_result.model else ''}).",
                            "provider": provider_result.provider, "model": provider_result.model,
                            "used_fallback": provider_result.used_fallback,
                            "prompt_preview": message[:2000], "response_preview": response[:2000],
                            "prompt_tokens": provider_result.prompt_tokens,
                            "completion_tokens": provider_result.completion_tokens,
                        })
                        meta = {
                            "agent": None, "skills": [], "provider": provider_result.provider,
                            "model": provider_result.model,
                            "used_fallback": provider_result.used_fallback, "hitl_pending": [], "sources": [],
                            "tool_calls": [], "web_sources": [], "downloadable_artifacts": [],
                        }

                output_check = self.guardrails.check_output(response)
                await self.observability.record_guardrail_check(
                    stage="check_output", findings=output_check, session_id=sid, turn_id=turn_id,
                )
                if not output_check["allowed"]:
                    response = "The response was blocked by the configured guardrails."
                elif output_check["redacted_text"] is not None:
                    response = output_check["redacted_text"]

                await self.sessions.append(sid, "assistant", response)
                await self.sessions.set_field(sid, "memory_state", memory_state.to_dict())
                turn.set_output(response, metadata={
                    "agent": meta.get("agent"), "skills": meta.get("skills"),
                    "provider": meta.get("provider"), "model": meta.get("model"),
                    "used_fallback": meta.get("used_fallback"), "tool_calls": meta.get("tool_calls"),
                })
                return {
                    "response": response, "session_id": sid,
                    "guardrails": {"input": input_check, "context": context_check, "output": output_check},
                    "skill_run": skill_run_meta, "routing": routing,
                    **meta,
                }
        finally:
            # Belt-and-suspenders: the two return paths above already clear
            # turn_checkpoint on their own successful completion, but a raised
            # exception (an orchestrator bug, a provider call that escapes
            # every existing degrade-gracefully guard) must not leave a stale
            # "in progress" marker behind either — see this method's own
            # docstring and app/storage.py's module docstring. A no-op if it
            # was already cleared (set_field is idempotent for an unknown
            # session too, so this is always safe to call).
            await self.sessions.set_field(sid, "turn_checkpoint", None)
            # A plain try/finally (no except) has no local exception variable
            # to read — sys.exc_info() is the standard way to see "is this
            # finally running because something raised" without adding an
            # except clause that would have to immediately re-raise anyway.
            exc = sys.exc_info()[1]
            if exc is not None:
                turn_status, turn_error = "error", str(exc)
            await self.observability.record_turn(
                turn_id=turn_id, session_id=sid, route=turn_route, status=turn_status,
                started_at=turn_started_at, ended_at=datetime.now(timezone.utc), error=turn_error,
                model_calls=collected_model_calls,
            )

    @staticmethod
    def _parse_model_choice(model: str | None) -> tuple[str | None, str | None]:
        """"provider/model" -> (provider, model); None/"auto" -> (None, None)
        meaning "use the server's configured default", same as today."""
        if not model or model in ("auto", "default"):
            return None, None
        provider, _, model_name = model.partition("/")
        return (provider or None), (model_name or None)

    async def chat_stream(
        self, message: str, session_id: str | None, cancellation_token: CancellationToken,
        model: str | None = None, images: list[dict] | None = None,
        auto_generate: bool = False,
    ) -> AsyncIterator[dict]:
        """SSE-friendly variant of chat(): yields incremental event dicts
        instead of returning one final dict.

        Real token-by-token AutoGen streaming (app/streaming.py) powers the
        plain direct-chat case (the router picked "agent" for this turn — no
        toggle-off state any more — but a plain "agent" turn with no tool
        calls is still, in effect, a bare "stream the model's answer"; only
        input-blocked and file-generation turns skip this) — see
        `attempt_real_stream` below. It also yields a "model_info" event (the
        actual resolved provider/model, not a guess) and a "status" event
        ("thinking") before the first token.

        Every other case (blocked input, skill Q&A) delegates to
        chat() via a background task, but still streams live: chat() accepts
        an `on_event` callback (see its docstring) that fires real progress
        events — knowledge retrieval, thinking, skill drafting/generation —
        as they happen; this generator bridges that callback to further
        "status" SSE events through an asyncio.Queue while awaiting the task,
        then delivers the same single "done" event as before once it
        finishes.

        `model` ("provider/model", from the Copilot model picker) is None for
        "use the server default" — same silent-fallback behavior as before.
        When set, it's an explicit user choice: a failure is always reported
        (see app/providers.py's describe_model_error), never silently
        swapped for mock — that would hide exactly what the picker is for.
        """
        session = await self.sessions.get_or_create(session_id)
        sid = session["session_id"]
        yield {"type": "session", "session_id": sid}

        explicit_provider, explicit_model = self._parse_model_choice(model)

        # See chat()'s matching comment: redact before anything downstream
        # (the model call, session history, the trace) ever sees the raw value.
        input_check = self.guardrails.check_input(message)
        # No turn_id here (unlike chat()) — chat_stream doesn't mint one or
        # participate in the tracer.turn()/checkpoint machinery; guardrail_
        # events.turn_id is nullable specifically for this case (see that
        # column's own comment, app/db/models.py).
        await self.observability.record_guardrail_check(stage="check_input", findings=input_check, session_id=sid)
        if input_check["redacted_text"] is not None:
            message = input_check["redacted_text"]
        pending_skill_run = session.get("pending_skill_run")
        pending_deck_builder = session.get("pending_deck_builder")
        # Whether this turn generates a file is now a routing decision, not a
        # substring match (see app/agents.py:plan_turn) — so this pre-check has
        # to ask the same question chat() will, or the two disagree and a turn
        # the router sends to a generator gets token-streamed as plain chat
        # instead. The plan is computed once here and handed to chat() below so
        # the router model is called once per turn, not twice.
        plan = None
        would_skill_route = bool(pending_skill_run) or bool(pending_deck_builder)
        if input_check["allowed"] and not would_skill_route:
            keyword_match = self.skill_packages.select_for_chat(message)
            plan = await plan_turn(
                message, skills=self.skill_packages.list(),
                keyword_match=keyword_match.public() if keyword_match else None,
                provider=self.provider,
            )
            would_skill_route = plan.route in ("deck", "skill")
        # Real token streaming only for plan.route == "direct" — the router's
        # own signal that this turn needs no augmentation (no RAG grounding,
        # no tools). Any turn that might benefit from either ("agent") goes
        # through the slower delegate path below so it actually gets them —
        # stream_chat is a bare completion with no grounding/tool-calling of
        # its own, so real-streaming an "agent" turn would silently drop both.
        attempt_real_stream = (
            input_check["allowed"] and not would_skill_route and plan is not None and plan.route == "direct"
        )

        if attempt_real_stream:
            history = list(session["messages"])
            full_text = ""
            cancelled = False
            error = None
            result_provider = None
            result_model = None
            memory_state = CompactMemoryState.from_dict(session.get("memory_state"))
            async with tracer.turn(
                "chat_turn_stream", input=message, metadata={"session_id": sid},
            ) as turn:
                async for event in stream_chat(
                    message, history, cancellation_token,
                    provider=explicit_provider, model=explicit_model, strict=bool(explicit_provider),
                    images=images, memory_provider=self.provider, memory_state=memory_state.to_dict(),
                ):
                    if event["type"] == "delta":
                        full_text += event["text"]
                        yield event
                    elif event["type"] in ("team_event", "status"):
                        turn.event(event.get("stage") or event.get("event_type") or "progress",
                                   **{k: v for k, v in event.items() if k not in ("type", "stage")})
                        yield event
                    elif event["type"] == "model_info":
                        # The real, resolved identity of what's answering —
                        # replaces the old "autogen-stream" placeholder, which
                        # named the streaming mechanism, not the model. Forwarded
                        # live so the UI can show it immediately, and kept for
                        # the "done" event below.
                        result_provider, result_model = event["provider"], event["model"]
                        yield event
                    elif event["type"] == "memory_state":
                        memory_state = CompactMemoryState.from_dict(event["state"])
                        await self.sessions.set_field(sid, "memory_state", memory_state.to_dict())
                    elif event["type"] == "cancelled":
                        cancelled = True
                    elif event["type"] == "error":
                        error = event["message"]

                if error and not full_text and not cancelled and not explicit_provider:
                    # Automatic path, unavailable -> fall through to chat() below (which
                    # opens its own "chat_turn" trace), no error shown. This trace still
                    # closes with a note of why, rather than a silently empty one.
                    turn.set_output(None, metadata={"fallback": "delegate"})
                else:
                    await self.sessions.append(sid, "user", message)
                    result_provider = result_provider or explicit_provider or "unknown"
                    result_model = result_model or explicit_model
                    if cancelled:
                        response = full_text or "(cancelled before any output)"
                        await self.sessions.append(sid, "assistant", response)
                        turn.set_output(response, metadata={"cancelled": True, "provider": result_provider, "model": result_model})
                        yield {
                            "response": response, "session_id": sid, "type": "done", "cancelled": True,
                            "guardrails": {"input": input_check, "context": None, "output": None},
                            "agent": None, "skills": [], "provider": result_provider, "model": result_model,
                            "used_fallback": False,
                            "hitl_pending": [], "sources": [], "skill_run": None, "tool_calls": [],
                            "web_sources": [], "downloadable_artifacts": [], "routing": plan.public(),
                        }
                        return
                    if error:
                        response = (f"Couldn't get a response from {explicit_provider}/{explicit_model}: {error}"
                                    if explicit_provider else f"Something went wrong generating that response ({error}).")
                        await self.sessions.append(sid, "assistant", response)
                        turn.set_output(response, metadata={"error": error, "provider": result_provider, "model": result_model})
                        yield {
                            "response": response, "session_id": sid, "type": "done", "model_error": True,
                            "guardrails": {"input": input_check, "context": None, "output": None},
                            "agent": None, "skills": [], "provider": result_provider, "model": result_model,
                            "used_fallback": True,
                            "hitl_pending": [], "sources": [], "skill_run": None, "tool_calls": [],
                            "web_sources": [], "downloadable_artifacts": [], "routing": plan.public(),
                        }
                        return
                    output_check = self.guardrails.check_output(full_text)
                    await self.observability.record_guardrail_check(
                        stage="check_output", findings=output_check, session_id=sid,
                    )
                    if not output_check["allowed"]:
                        full_text = "The response was blocked by the configured guardrails."
                    elif output_check["redacted_text"] is not None:
                        # NOTE: real token-by-token streaming (stream_chat above)
                        # has already sent every delta to the client by this
                        # point — redaction here can only fix what gets stored/
                        # returned in the "done" payload and session history,
                        # not what was already rendered live. The guardrail
                        # activity panel still reports the finding either way
                        # (see updateGuardrailChip/guardrailActivityHtml), so a
                        # PII/secret leak in a streamed reply is visible even
                        # though it couldn't be intercepted mid-stream.
                        full_text = output_check["redacted_text"]
                    await self.sessions.append(sid, "assistant", full_text)
                    turn.generation(
                        "stream_chat", model=result_model, provider=result_provider,
                        input=message[:2000], output=full_text[:2000],
                    )
                    turn.set_output(full_text, metadata={"provider": result_provider, "model": result_model})
                    yield {
                        "response": full_text, "session_id": sid, "type": "done",
                        "guardrails": {"input": input_check, "context": None, "output": output_check},
                        "agent": None, "skills": [], "provider": result_provider, "model": result_model,
                        "used_fallback": False,
                        "hitl_pending": [], "sources": [], "skill_run": None, "tool_calls": [],
                        "web_sources": [], "downloadable_artifacts": [], "routing": plan.public(),
                    }
                    return

        # Delegate: input blocked, skill-routed, an "agent" turn (needs
        # augmentation), or real streaming unavailable. Still streams live:
        # chat()'s on_event callback (fired
        # from real, already-happening steps — knowledge retrieval, thinking,
        # skill drafting/generation) is bridged through this queue to "status"
        # SSE events while chat() runs as a background task, instead of the
        # request going quiet until one final lump response.
        #
        # Wrapped in its own task, with the SAME cancellation_token linked only to
        # this task (not shared with any outer task-level link) — a genuine stop
        # button even for a path with no AutoGen involvement, and no race against
        # the real-stream branch's own cancellation handling above.
        event_queue: asyncio.Queue[dict] = asyncio.Queue()

        async def on_event(event: dict) -> None:
            await event_queue.put(event)

        # allow_live_hitl_wait=True: this SSE connection is already long-lived
        # (that's the whole point of this delegate path), so a coding-agent
        # turn here can genuinely wait for a live HITL decision instead of
        # only ever reporting "queued" — see AutoGenOrchestrator.run's coding
        # skill section and app/hitl_agents.py.
        # `plan` is the routing decision already made above (None when this
        # turn never needed one — blocked input, or a pending skill/deck
        # continuation), passed down so chat() reuses it instead of calling the
        # router a second time and possibly landing somewhere else.
        chat_task = asyncio.ensure_future(self.chat(
            message, sid, on_event=on_event, images=images, allow_live_hitl_wait=True,
            auto_generate=auto_generate, plan=plan,
        ))
        cancellation_token.link_future(chat_task)
        try:
            while True:
                get_event = asyncio.ensure_future(event_queue.get())
                done, _ = await asyncio.wait({chat_task, get_event}, return_when=asyncio.FIRST_COMPLETED)
                if get_event in done:
                    yield {"type": "status", **get_event.result()}
                if chat_task in done:
                    if not get_event.done():
                        get_event.cancel()
                    break
            # Drain anything queued between the task finishing and this loop's
            # last check (asyncio.wait's two futures can both resolve in the
            # same tick) so no progress event is silently dropped.
            while not event_queue.empty():
                yield {"type": "status", **event_queue.get_nowait()}
            result = await chat_task
        except asyncio.CancelledError:
            yield {"type": "cancelled"}
            yield {
                "response": "(cancelled)", "session_id": sid, "type": "done", "cancelled": True,
                "guardrails": {"input": input_check, "context": None, "output": None},
                "agent": None, "skills": [], "provider": None, "model": None, "used_fallback": False,
                "hitl_pending": [], "sources": [], "skill_run": None, "tool_calls": [],
                "web_sources": [], "downloadable_artifacts": [],
                "routing": plan.public() if plan else None,
            }
            return
        yield {"type": "delta", "text": result["response"]}
        yield {"type": "done", **result}

    # --- chat-integrated skill Q&A: ask the skill's declared questions inline,
    # one per turn, then generate — the same HITL flow as the Skills tab's
    # pre-flight form, but conversational instead of a modal. ---------------

    @staticmethod
    def _question_public(question) -> dict:
        return {
            "id": question.id, "prompt": question.prompt, "type": question.type,
            "options": question.options, "required": question.required,
        }

    @staticmethod
    def _format_skill_question(skill, question, index: int, total: int) -> str:
        lines = [f"I'll help you create a {skill.output or 'file'} with **{skill.name}**.", "", f"**{question.prompt}**"]
        if question.options:
            lines.append("Options: " + ", ".join(question.options))
        note = "required" if question.required else 'optional — reply "skip" to skip'
        lines.append(f"_(question {index + 1} of {total} — {note})_")
        return "\n".join(lines)

    async def _start_chat_skill_run(
        self, session_id: str, skill, on_event: Callable[[dict], Awaitable[None]] | None = None,
        delivery: str = "file",
    ) -> tuple[str, dict]:
        run = self.skill_runs.start(skill.skill_id)
        question_ids = [q.id for q in skill.questions]
        if not question_ids:
            return await self._finish_chat_skill_run(run.run_id, {}, on_event=on_event, delivery=delivery)

        await self.sessions.set_field(session_id, "pending_skill_run", {
            "run_id": run.run_id, "skill_id": skill.skill_id, "question_ids": question_ids, "index": 0,
            "delivery": delivery,
        })
        first_question = skill.questions[0]
        text = self._format_skill_question(skill, first_question, index=0, total=len(question_ids))
        return text, {
            "run_id": run.run_id, "skill_id": skill.skill_id, "status": "AWAITING_ANSWERS",
            "question": self._question_public(first_question), "download_ready": False,
        }

    async def _continue_chat_skill_run(
        self, session_id: str, pending: dict, message: str,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        run_id, skill_id = pending["run_id"], pending["skill_id"]
        question_ids, index = pending["question_ids"], pending["index"]
        try:
            skill = self.skill_packages.get(skill_id)
            run = self.skill_runs.get(run_id)
        except KeyError:
            await self.sessions.set_field(session_id, "pending_skill_run", None)
            return "That skill run is no longer available — say the word again (e.g. \"create a docx\") to start over.", None

        question = next(q for q in skill.questions if q.id == question_ids[index])
        answer = message.strip()
        if not question.required and answer.lower() in ("skip", "none", "n/a", "-"):
            answer = ""
        elif question.required and not answer:
            text = self._format_skill_question(skill, question, index, len(question_ids))
            return f"That one's required — {text}", {
                "run_id": run_id, "skill_id": skill_id, "status": "AWAITING_ANSWERS",
                "question": self._question_public(question), "download_ready": False,
            }

        run.answers[question.id] = answer
        next_index = index + 1
        if next_index < len(question_ids):
            await self.sessions.set_field(session_id, "pending_skill_run", {**pending, "index": next_index})
            next_question = next(q for q in skill.questions if q.id == question_ids[next_index])
            text = self._format_skill_question(skill, next_question, next_index, len(question_ids))
            return text, {
                "run_id": run_id, "skill_id": skill_id, "status": "AWAITING_ANSWERS",
                "question": self._question_public(next_question), "download_ready": False,
            }

        await self.sessions.set_field(session_id, "pending_skill_run", None)
        return await self._finish_chat_skill_run(
            run_id, run.answers, on_event=on_event,
            # .get, not ["delivery"]: a pending_skill_run written before this
            # field existed must not KeyError mid-conversation.
            delivery=pending.get("delivery", "file"),
        )

    async def _finish_chat_skill_run(
        self, run_id: str, answers: dict, on_event: Callable[[dict], Awaitable[None]] | None = None,
        delivery: str = "file",
    ) -> tuple[str, dict]:
        try:
            run = await self.skill_runs.submit_answers(run_id, answers, on_event=on_event, delivery=delivery)
        except Exception as exc:  # noqa: BLE001 - a run failure is a chat response, not a crash
            return f"Couldn't generate that: {exc}", None
        skill = self.skill_packages.get(run.skill_id)
        if run.status == "COMPLETED":
            text = (f"Done! Generated **{skill.name}.{skill.output}**. "
                    "Download it below, or open the Skills tab to view/edit it.")
        elif run.status == "COMPLETED_INLINE":
            text = run.rendered_text or ""
        else:
            text = f"Generation failed: {run.error or 'unknown error'}. Open the Skills tab to try again."
        return text, {**run.public(), "skill_name": skill.name, "output": skill.output}

service = CopilotService()
