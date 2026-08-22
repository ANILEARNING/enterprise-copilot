"""RAG v2's vector-DB seam: `VectorStore` is the interface `RAGStore`
(app/services.py) depends on; `InMemoryVectorStore` (today's exhaustive
cosine scan, extracted verbatim from RAGStore.search) is the always-available
fallback, `QdrantVectorStore` is the production backend. Same seam style as
`EmbeddingProvider`/`AIProvider` (app/providers.py) — a missing/unreachable
Qdrant configuration degrades to the in-memory implementation, never breaks
ingestion or search. See docs/rag.md and .claude/rules/architecture.md's
"in-memory/local for v1 unless the specification requires otherwise" — a
configured Qdrant Cloud instance is exactly that explicit override.

Selection happens once, at construction (`build_vector_store()`), based on
whether Qdrant is configured at all — never per-call, so a transient Qdrant
outage surfaces as a real error rather than silently splitting one tenant's
data across two backends mid-session.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import settings
from .retrieval import cosine_similarity

logger = logging.getLogger(__name__)


@dataclass
class VectorPoint:
    """One embedded chunk, ready to upsert. `point_id` is the chunk_id
    (already a uuid4 string — valid as a Qdrant point id directly). `payload`
    carries everything needed to render a citation/grounding context without
    a second lookup: document_id, filename, chunk_index, parent_text,
    heading_path, is_table, content_hash, tenant_id, plus embedding_provider/
    embedding_model for describe_embedding()."""
    point_id: str
    vector: list[float]
    payload: dict = field(default_factory=dict)


@dataclass
class ScoredPoint:
    point_id: str
    score: float
    payload: dict = field(default_factory=dict)


class VectorStore:
    """Provider-agnostic vector search interface. Every method is async —
    a real backend (Qdrant) is a network call; InMemoryVectorStore's own
    methods are async too so RAGStore never needs to know which one it has."""

    name = "base"

    async def upsert(self, points: list[VectorPoint]) -> None:
        raise NotImplementedError

    async def delete(self, point_ids: list[str]) -> None:
        raise NotImplementedError

    async def search(self, vector: list[float], tenant_id: str, limit: int) -> list[ScoredPoint]:
        raise NotImplementedError

    async def health(self) -> dict:
        raise NotImplementedError


class InMemoryVectorStore(VectorStore):
    """Today's behavior, extracted behind the interface: an exhaustive
    cosine-similarity scan over an in-memory dict. Dimension-guarded exactly
    as RAGStore.search used to inline — a point embedded by a different
    provider than the current query (e.g. an earlier ingest fell back to the
    hash embedder while a later one used a real model) lives in an
    incomparable vector space; comparing them would zip-truncate and return
    a meaningless score, so a mismatched point is skipped for the vector leg
    (BM25 still covers it regardless, same as before)."""

    name = "in-memory"

    def __init__(self):
        self._points: dict[str, VectorPoint] = {}

    async def upsert(self, points: list[VectorPoint]) -> None:
        for point in points:
            self._points[point.point_id] = point

    async def delete(self, point_ids: list[str]) -> None:
        for point_id in point_ids:
            self._points.pop(point_id, None)

    async def search(self, vector: list[float], tenant_id: str, limit: int) -> list[ScoredPoint]:
        scored = [
            ScoredPoint(point_id=pid, score=cosine_similarity(vector, p.vector), payload=p.payload)
            for pid, p in self._points.items()
            if len(p.vector) == len(vector) and p.payload.get("tenant_id") == tenant_id
        ]
        scored = [s for s in scored if s.score > 0]
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:limit]

    async def health(self) -> dict:
        return {"backend": self.name, "reachable": True, "point_count": len(self._points)}


class QdrantVectorStore(VectorStore):
    """Qdrant Cloud (or any Qdrant instance) behind the same interface. The
    collection is created lazily on first upsert/search if it doesn't exist
    yet — no manual provisioning step for the user — sized to whatever
    embedding dimension the first point actually has (Gemini/Ollama/hash all
    differ, so this can't be hardcoded).

    The AsyncQdrantClient itself is also built lazily, on first actual async
    call rather than in __init__ — its internal httpx transport lazily binds
    asyncio primitives (locks/events) to whichever event loop is running the
    first time it's used, and stays bound to that loop forever. RAGStore's
    bootstrap-document seeding (app/services.py) can construct a RAGStore
    (and this store alongside it) and immediately drive its first Qdrant
    call through app/services.py:_run_sync's throwaway thread/loop (needed
    for the no-event-loop-yet startup case) — if __init__ built the client
    eagerly on the caller's loop but first use happened on that throwaway
    loop, or vice versa, every later call from the real running loop would
    fail with "bound to a different event loop". Building on first use, and
    rebuilding if the running loop has changed since, avoids that regardless
    of which loop happens to touch this store first."""

    name = "qdrant"

    def __init__(self, url: str, api_key: str, collection: str):
        import qdrant_client  # local import: optional dependency, only loaded when configured; also validates it's installed

        self._url = url
        self._api_key = api_key
        self._collection = collection
        self._client: qdrant_client.AsyncQdrantClient | None = None
        self._client_loop: object | None = None
        self._ensured_dim: int | None = None

    def _get_client(self):
        import asyncio
        from qdrant_client import AsyncQdrantClient

        loop = asyncio.get_running_loop()
        # getattr, not self._client_loop directly: a test double that
        # constructs this class via QdrantVectorStore.__new__() and injects
        # its own fake client (bypassing __init__ entirely — see
        # tests/test_vector_store.py's qdrant_store fixture) never sets
        # _client_loop. Treat that as "an explicitly injected client, use it
        # as-is, never rebuild it" rather than raising or silently replacing
        # the test's fake with a real network client.
        if self._client is None or getattr(self, "_client_loop", loop) is not loop:
            # check_compatibility=False: skips an extra round-trip probing
            # server version compatibility at construction time (qdrant-client
            # warns without it) — this app already degrades any Qdrant call
            # failure gracefully (see build_vector_store), so that check buys
            # nothing here.
            self._client = AsyncQdrantClient(url=self._url, api_key=self._api_key, check_compatibility=False)
            self._client_loop = loop
        return self._client

    async def _ensure_collection(self, dim: int) -> None:
        # Cheap process-local guard against re-checking on every call once we
        # know the collection exists at this dimension; a genuinely stale
        # cache (collection deleted out-of-band) still surfaces as a normal
        # Qdrant error on the next upsert/search rather than silently
        # misbehaving — this is purely to avoid a redundant round-trip.
        if self._ensured_dim == dim:
            return
        from qdrant_client import models

        client = self._get_client()
        if not await client.collection_exists(self._collection):
            await client.create_collection(
                self._collection,
                vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
            )
            # Qdrant Cloud (unlike a permissive local instance) rejects a
            # filtered search with 400 "Index required but not found" unless
            # a payload index exists for every field used in a query filter.
            # Every search() call here filters on tenant_id (see the
            # multi-tenancy note in docs/rag.md), so the collection is
            # unusable for search without this — created once, right after
            # the collection itself, not on every _ensure_collection call.
            await client.create_payload_index(
                self._collection, field_name="tenant_id", field_schema=models.PayloadSchemaType.KEYWORD,
            )
        self._ensured_dim = dim

    async def upsert(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        from qdrant_client import models

        await self._ensure_collection(len(points[0].vector))
        await self._get_client().upsert(
            self._collection,
            points=[
                models.PointStruct(id=p.point_id, vector=p.vector, payload=p.payload)
                for p in points
            ],
        )

    async def delete(self, point_ids: list[str]) -> None:
        if not point_ids:
            return
        from qdrant_client import models

        await self._get_client().delete(self._collection, points_selector=models.PointIdsList(points=point_ids))

    async def search(self, vector: list[float], tenant_id: str, limit: int) -> list[ScoredPoint]:
        from qdrant_client import models

        await self._ensure_collection(len(vector))
        result = await self._get_client().query_points(
            self._collection,
            query=vector,
            query_filter=models.Filter(
                must=[models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))],
            ),
            limit=limit,
            with_payload=True,
        )
        return [ScoredPoint(point_id=str(pt.id), score=pt.score, payload=pt.payload or {}) for pt in result.points]

    async def health(self) -> dict:
        try:
            info = await self._get_client().get_collection(self._collection)
            return {
                "backend": self.name, "reachable": True, "collection": self._collection,
                "point_count": info.points_count,
            }
        except Exception as exc:  # noqa: BLE001 - health check must never raise, only report
            return {"backend": self.name, "reachable": False, "collection": self._collection, "error": str(exc)}


def build_vector_store() -> VectorStore:
    """Factory: mock mode (the default — AI_MODE != "configured") always
    returns the in-memory fallback, same master offline switch as
    build_ai_provider()/build_embedding_provider() in app/providers.py — a
    QDRANT_URL/QDRANT_API_KEY present in the environment must not, by itself,
    make a real network call; AI_MODE=configured is what opts the whole app
    into live backends. In configured mode, both QDRANT_URL and
    QDRANT_API_KEY must still be set to select Qdrant; either missing, or a
    construction-time failure (bad url, missing extra), degrades to the
    always-available in-memory store."""
    if settings.ai_mode == "configured" and settings.qdrant_url and settings.qdrant_api_key:
        try:
            return QdrantVectorStore(settings.qdrant_url, settings.qdrant_api_key, settings.qdrant_collection)
        except Exception as exc:  # noqa: BLE001 - a construction-time failure (bad url, missing extra) must degrade
            logger.warning("Qdrant vector store unavailable, falling back to in-memory: %s", exc)
    return InMemoryVectorStore()
