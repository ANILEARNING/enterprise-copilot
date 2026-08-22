"""app/vector_store.py — InMemoryVectorStore tested directly (no network);
QdrantVectorStore tested against a fake AsyncQdrantClient (same "inject a
fake dependency" pattern as FakeSemanticEmbeddingProvider in
tests/test_retrieval.py) — never a live network call in the test suite,
consistent with every other provider test in this repo."""
import pytest

from app.config import settings
from app.vector_store import (
    InMemoryVectorStore, QdrantVectorStore, ScoredPoint, VectorPoint, build_vector_store,
)


def _point(point_id: str, vector: list[float], tenant_id: str = "default", **extra) -> VectorPoint:
    return VectorPoint(point_id=point_id, vector=vector, payload={"tenant_id": tenant_id, **extra})


# --- InMemoryVectorStore --------------------------------------------------

@pytest.mark.asyncio
async def test_in_memory_search_ranks_by_cosine_similarity():
    store = InMemoryVectorStore()
    await store.upsert([
        _point("a", [1.0, 0.0], filename="a.md"),
        _point("b", [0.0, 1.0], filename="b.md"),
    ])
    results = await store.search([1.0, 0.0], tenant_id="default", limit=2)
    assert results[0].point_id == "a"
    assert results[0].payload["filename"] == "a.md"


@pytest.mark.asyncio
async def test_in_memory_search_filters_by_tenant():
    store = InMemoryVectorStore()
    await store.upsert([_point("a", [1.0, 0.0], tenant_id="tenant-1")])
    assert await store.search([1.0, 0.0], tenant_id="tenant-2", limit=5) == []
    results = await store.search([1.0, 0.0], tenant_id="tenant-1", limit=5)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_in_memory_search_skips_mismatched_dimensions():
    # A point embedded by a different provider than the query lives in an
    # incomparable vector space — must be skipped, not zip-truncated into a
    # meaningless score (same guard RAGStore.search used to inline).
    store = InMemoryVectorStore()
    await store.upsert([_point("a", [1.0, 0.0, 0.0])])
    results = await store.search([1.0, 0.0], tenant_id="default", limit=5)
    assert results == []


@pytest.mark.asyncio
async def test_in_memory_delete_removes_points():
    store = InMemoryVectorStore()
    await store.upsert([_point("a", [1.0, 0.0])])
    await store.delete(["a"])
    assert await store.search([1.0, 0.0], tenant_id="default", limit=5) == []


@pytest.mark.asyncio
async def test_in_memory_upsert_overwrites_same_point_id():
    store = InMemoryVectorStore()
    await store.upsert([_point("a", [1.0, 0.0])])
    await store.upsert([_point("a", [0.0, 1.0])])  # same id, new vector
    results = await store.search([0.0, 1.0], tenant_id="default", limit=5)
    assert len(results) == 1
    assert results[0].point_id == "a"


@pytest.mark.asyncio
async def test_in_memory_health_reports_point_count():
    store = InMemoryVectorStore()
    await store.upsert([_point("a", [1.0]), _point("b", [0.5])])
    health = await store.health()
    assert health == {"backend": "in-memory", "reachable": True, "point_count": 2}


# --- build_vector_store: selection logic ----------------------------------
#
# Mirrors build_ai_provider()/build_embedding_provider() in app/providers.py:
# AI_MODE=configured is the master offline switch. A QDRANT_URL/QDRANT_API_KEY
# present in the environment must NOT, by itself, cause a live Qdrant client
# to be built — mock mode always wins, so the test suite (and any dev running
# with real Qdrant credentials in .env but AI_MODE=mock) never makes a live
# network call just from constructing a RAGStore().

def test_build_vector_store_defaults_to_in_memory_when_mock_mode(monkeypatch):
    monkeypatch.setattr(settings, "ai_mode", "mock")
    monkeypatch.setattr(settings, "qdrant_url", "https://example.qdrant.io")
    monkeypatch.setattr(settings, "qdrant_api_key", "fake-key")
    assert isinstance(build_vector_store(), InMemoryVectorStore)


def test_build_vector_store_defaults_to_in_memory_when_configured_but_unset(monkeypatch):
    monkeypatch.setattr(settings, "ai_mode", "configured")
    monkeypatch.setattr(settings, "qdrant_url", "")
    monkeypatch.setattr(settings, "qdrant_api_key", "")
    assert isinstance(build_vector_store(), InMemoryVectorStore)
    monkeypatch.setattr(settings, "qdrant_url", "https://x")
    monkeypatch.setattr(settings, "qdrant_api_key", "")
    assert isinstance(build_vector_store(), InMemoryVectorStore)  # key missing
    monkeypatch.setattr(settings, "qdrant_url", "")
    monkeypatch.setattr(settings, "qdrant_api_key", "key")
    assert isinstance(build_vector_store(), InMemoryVectorStore)  # url missing


def test_build_vector_store_selects_qdrant_when_configured_and_both_set(monkeypatch):
    monkeypatch.setattr(settings, "ai_mode", "configured")
    monkeypatch.setattr(settings, "qdrant_url", "https://example.qdrant.io")
    monkeypatch.setattr(settings, "qdrant_api_key", "fake-key")
    monkeypatch.setattr(settings, "qdrant_collection", "my_collection")
    store = build_vector_store()
    assert isinstance(store, QdrantVectorStore)


# --- QdrantVectorStore: against a fake AsyncQdrantClient ------------------

class FakeQdrantClient:
    """Stands in for qdrant_client.AsyncQdrantClient — records calls,
    returns deterministic canned responses, never touches the network."""

    def __init__(self, *a, **kw):
        self.collections: set[str] = set()
        self.points: dict[str, dict] = {}  # collection -> {point_id: (vector, payload)}
        self.upsert_calls: list[tuple] = []
        self.delete_calls: list[tuple] = []
        self.payload_indexes: set[tuple] = set()  # (collection, field_name)

    async def collection_exists(self, name: str) -> bool:
        return name in self.collections

    async def create_collection(self, name: str, vectors_config=None, **kw) -> bool:
        self.collections.add(name)
        self.points[name] = {}
        return True

    async def create_payload_index(self, collection_name: str, field_name: str, field_schema=None, **kw):
        self.payload_indexes.add((collection_name, field_name))
        return object()

    async def upsert(self, collection_name: str, points, **kw):
        self.upsert_calls.append((collection_name, points))
        for p in points:
            self.points.setdefault(collection_name, {})[p.id] = (p.vector, p.payload)
        return object()

    async def delete(self, collection_name: str, points_selector, **kw):
        self.delete_calls.append((collection_name, points_selector))
        for pid in points_selector.points:
            self.points.get(collection_name, {}).pop(pid, None)
        return object()

    async def query_points(self, collection_name: str, query, query_filter=None, limit=10, **kw):
        from qdrant_client import models

        tenant = None
        if query_filter is not None:
            tenant = query_filter.must[0].match.value
        scored = []
        for pid, (vector, payload) in self.points.get(collection_name, {}).items():
            if tenant is not None and payload.get("tenant_id") != tenant:
                continue
            score = sum(a * b for a, b in zip(query, vector))
            scored.append(models.ScoredPoint(id=pid, version=0, score=score, payload=payload))
        scored.sort(key=lambda s: s.score, reverse=True)

        class _Response:
            def __init__(self, points):
                self.points = points

        return _Response(scored[:limit])

    async def get_collection(self, collection_name: str, **kw):
        class _Info:
            points_count = len(self.points.get(collection_name, {}))

        return _Info()


@pytest.fixture
def qdrant_store(monkeypatch):
    fake = FakeQdrantClient()
    store = QdrantVectorStore.__new__(QdrantVectorStore)  # bypass __init__'s real client construction
    store._collection = "test_collection"
    store._client = fake
    store._ensured_dim = None
    return store, fake


@pytest.mark.asyncio
async def test_qdrant_upsert_creates_collection_then_upserts(qdrant_store):
    store, fake = qdrant_store
    await store.upsert([_point("a", [1.0, 0.0], filename="a.md")])
    assert "test_collection" in fake.collections
    assert len(fake.upsert_calls) == 1
    # Qdrant Cloud rejects a tenant_id-filtered search with 400 unless a
    # payload index exists for that field — must be created alongside the
    # collection, not left implicit (see _ensure_collection's docstring).
    assert ("test_collection", "tenant_id") in fake.payload_indexes


@pytest.mark.asyncio
async def test_qdrant_search_filters_by_tenant_and_returns_scored_points(qdrant_store):
    store, fake = qdrant_store
    await store.upsert([
        _point("a", [1.0, 0.0], tenant_id="default", filename="a.md"),
        _point("b", [0.0, 1.0], tenant_id="other", filename="b.md"),
    ])
    results = await store.search([1.0, 0.0], tenant_id="default", limit=5)
    assert len(results) == 1
    assert results[0].point_id == "a"
    assert results[0].payload["filename"] == "a.md"


@pytest.mark.asyncio
async def test_qdrant_delete_removes_points(qdrant_store):
    store, fake = qdrant_store
    await store.upsert([_point("a", [1.0, 0.0])])
    await store.delete(["a"])
    results = await store.search([1.0, 0.0], tenant_id="default", limit=5)
    assert results == []


@pytest.mark.asyncio
async def test_qdrant_health_reports_reachable_and_point_count(qdrant_store):
    store, fake = qdrant_store
    await store.upsert([_point("a", [1.0, 0.0])])
    health = await store.health()
    assert health["backend"] == "qdrant"
    assert health["reachable"] is True
    assert health["point_count"] == 1


@pytest.mark.asyncio
async def test_qdrant_health_reports_unreachable_on_error():
    store = QdrantVectorStore.__new__(QdrantVectorStore)
    store._collection = "test_collection"
    store._ensured_dim = None

    class BrokenClient:
        async def get_collection(self, *a, **kw):
            raise ConnectionError("simulated network failure")

    store._client = BrokenClient()
    health = await store.health()
    assert health["reachable"] is False
    assert "simulated network failure" in health["error"]
