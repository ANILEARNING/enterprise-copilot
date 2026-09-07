"""Session-wide pytest setup — runs before any test module is imported
(pytest guarantees a root conftest.py loads ahead of test collection), which
matters here specifically: app/services.py builds one real CopilotService()
singleton (`service = CopilotService()`) the moment app.services or app.main
is imported by any test file, and CopilotService's file-backed stores
(SkillPackageStore, SkillRunService — app/services.py) default to this
repo's real data/ directory unless redirected first.

Setting DATA_DIR here, before that import can happen, redirects the whole
data/ tree to a per-test-session temp directory (auto-cleaned by pytest's
own tmp_path_factory machinery) — so running the suite never writes real
skill/skill-run files into this repo's own data/ folder. Individual tests
that want their OWN isolated store (not sharing the one process-wide
singleton) should still pass an explicit data_dir=tmp_path, same as
tests/test_storage.py and tests/test_skills.py already do — this fixture
only covers the module-level `service` singleton other tests import.

Also forcing AI_MODE=mock here for the same reason: build_ai_provider(),
build_embedding_provider(), and build_vector_store() (app/providers.py,
app/vector_store.py) all treat AI_MODE=configured as the one switch that
turns on live network calls (Gemini/Ollama/Qdrant), independent of whether
API keys happen to be present in the environment. A developer's real .env
may legitimately carry live QDRANT_URL/QDRANT_API_KEY/GEMINI_API_KEY for
running the app — the test suite must never pick those up and start making
real network calls just because they're present. Tests that specifically
want to exercise the "configured" path already do so explicitly via
monkeypatch.setattr(settings, "ai_mode", "configured") plus a fake/injected
provider, never by relying on real credentials from the environment.

Sessions (app/session_store.py) get the same "never touch the real backend
in tests" treatment, Upstash's version of it: CopilotService's module-level
`service` singleton constructs its own SessionStore(url=..., token=...) at
import time, so the upstash_redis.asyncio.Redis client it ends up holding
has to already be a fake before that import happens — same ordering
constraint DATA_DIR is here to satisfy for the file-backed stores. Unlike
Postgres (aiosqlite, a real embedded engine) there's no local/embedded
substitute for Upstash's REST API, so this is a small hand-rolled fake
implementing just the surface SessionStore actually calls (get/set/zadd/
zrange, plus multi() pipelining) against an in-process dict — good enough
to exercise the real SessionStore code path without a network call, same
spirit as the small hand-rolled provider fakes already used throughout
tests/ (e.g. _FakeRouterProvider in tests/test_turn_routing.py)."""
import os
import tempfile

# Every env var below MUST be set before anything that transitively imports
# app.config (app.document_store and app.session_store both do, via `from
# .config import settings`) — Settings() is a module-level singleton built
# once at that import, from os.environ as it exists at that exact moment.
# Importing those two modules above this block, instead of below it, was a
# real bug caught here: it constructed `settings` (via app.config) with
# AI_MODE still whatever the real .env said, before this file ever got to
# force AI_MODE=mock — silently letting build_vector_store() select a real
# Qdrant collection during test runs. Order matters; do not move these
# imports back above the os.environ assignments.
_data_dir = tempfile.mkdtemp(prefix="copilot-test-data-")
os.environ.setdefault("DATA_DIR", _data_dir)
os.environ["AI_MODE"] = "mock"
os.environ["UPSTASH_REDIS_REST_URL"] = "https://fake"
os.environ["UPSTASH_REDIS_REST_TOKEN"] = "fake-token"
os.environ["B2_ENDPOINT"] = "https://fake"
os.environ["B2_BUCKET_NAME"] = "fake-bucket"
os.environ["B2_KEY_ID"] = "fake-key-id"
os.environ["B2_APPLICATION_KEY"] = "fake-application-key"
# Same "never touch the real backend in tests" treatment as the Redis/B2 vars
# above — a real .env's DATABASE_URL (live Neon Postgres) leaks straight into
# CopilotService's db_engine/db_session_factory construction (app/services.py)
# otherwise, and this sandbox's environment cannot route the IPv6 address
# Neon's DNS returns first (see app/db/engine.py's _patch_ipv4_preference), so
# every real connection attempt hangs/times out instead of failing fast —
# verified live: the suite took minutes instead of seconds, with a leaked
# asyncpg.Connection warning at the end. Must be a real (empty-string, not
# just unset) override, not os.environ.pop: Settings.model_config sets
# env_file=".env" (see app/config.py), and pydantic-settings reads that file
# directly whenever a key is genuinely absent from os.environ — popping the
# var here doesn't block it, it just removes the one thing that WAS
# overriding it. An explicit "" is falsy, so CopilotService's `if
# settings.database_url:` guard takes the documented no-op path.
os.environ["DATABASE_URL"] = ""

import app.document_store as _document_store_module  # noqa: E402
import app.session_store as _session_store_module  # noqa: E402


class _FakeUpstashPipeline:
    """Enough of upstash_redis.asyncio.AsyncPipeline to exercise
    SessionStore._write: queue set()/zadd() calls, apply them all in exec()
    — matches the real pipeline's "queue now, send as one batch on exec()"
    shape closely enough that swapping the real client for this one changes
    nothing about how SessionStore itself is written or called."""

    def __init__(self, store: "_FakeUpstashClient"):
        self._store = store
        self._ops: list[tuple[str, tuple]] = []

    def set(self, key: str, value: str) -> "_FakeUpstashPipeline":
        self._ops.append(("set", (key, value)))
        return self

    def zadd(self, key: str, mapping: dict) -> "_FakeUpstashPipeline":
        self._ops.append(("zadd", (key, mapping)))
        return self

    async def exec(self) -> list:
        results = []
        for op, args in self._ops:
            if op == "set":
                results.append(await self._store.set(*args))
            elif op == "zadd":
                results.append(await self._store.zadd(*args))
        return results


class _FakeUpstashClient:
    """In-process stand-in for upstash_redis.asyncio.Redis — a plain dict
    for strings, a plain dict-of-dicts for sorted sets. Only implements what
    SessionStore actually calls; anything else raises AttributeError, same
    as it would against a real client missing a method this store never
    uses."""

    def __init__(self):
        self._strings: dict[str, str] = {}
        self._zsets: dict[str, dict[str, float]] = {}

    async def get(self, key: str) -> str | None:
        return self._strings.get(key)

    async def set(self, key: str, value: str) -> str:
        self._strings[key] = value
        return "OK"

    async def zadd(self, key: str, mapping: dict) -> int:
        zset = self._zsets.setdefault(key, {})
        added = sum(1 for member in mapping if member not in zset)
        zset.update(mapping)
        return added

    async def zrange(self, key: str, start: int, stop: int, rev: bool = False) -> list[str]:
        zset = self._zsets.get(key, {})
        ordered = sorted(zset.items(), key=lambda kv: kv[1], reverse=rev)
        members = [m for m, _ in ordered]
        stop_index = len(members) if stop == -1 else stop + 1
        return members[start:stop_index]

    def multi(self) -> _FakeUpstashPipeline:
        return _FakeUpstashPipeline(self)


# Shared across every SessionStore constructed during the test session (just
# like a real Upstash database is one shared backend across every
# SessionStore instance pointed at it) — a fresh dict per instance would
# defeat the point of persistence-across-calls tests rely on.
_fake_upstash_client = _FakeUpstashClient()
_session_store_module.Redis = lambda *args, **kwargs: _fake_upstash_client


class _FakeBlobStore:
    """In-process stand-in for app.blob_store.BlobStore — a plain dict
    keyed by object key, values always stored as bytes (put_text encodes,
    get_text decodes) so one dict backs both the text (DocumentStore) and
    binary (SkillRunService) call patterns identically to the real
    boto3-backed store. Shared across every DocumentStore/SkillRunService
    constructed during the test session, same reasoning as
    _fake_upstash_client above: tests/test_document_store.py's own
    test_update_persists_across_new_store_instances constructs a SECOND
    DocumentStore(data_dir=tmp_path) pointed at the same on-disk metadata
    directory and expects it to see content a first instance wrote — a
    fresh per-instance dict would defeat that, exactly like a fresh
    per-instance dict would defeat SessionStore's restart-persistence tests."""

    def __init__(self):
        self._objects: dict[str, bytes] = {}

    def put_text(self, key: str, content: str) -> None:
        self._objects[key] = content.encode("utf-8")

    def get_text(self, key: str) -> str:
        return self.get_bytes(key).decode("utf-8")

    def put_bytes(self, key: str, content: bytes, content_type: str = "application/octet-stream") -> None:
        self._objects[key] = content

    def get_bytes(self, key: str) -> bytes:
        if key not in self._objects:
            raise KeyError(f"No object at {key}")
        return self._objects[key]

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)


# Patched at the source module (app.blob_store.BlobStore), not just each
# importer's own bound name — app/document_store.py and app/skills.py both
# do `from .blob_store import BlobStore`, a separate name in each module's
# namespace once imported, so patching only e.g. _document_store_module.
# BlobStore would leave app.skills's own BlobStore reference pointing at
# the real (network-calling) class. Patching the shared source here covers
# every future `from .blob_store import BlobStore` too, not just today's two.
_fake_blob_store = _FakeBlobStore()
import app.blob_store as _blob_store_module  # noqa: E402
_blob_store_module.BlobStore = lambda *args, **kwargs: _fake_blob_store
_document_store_module.BlobStore = _blob_store_module.BlobStore
