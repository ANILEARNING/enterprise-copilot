"""Upstash Redis-backed session/chat-history storage — replaces the old
file-backed SessionStore (app/storage.py, kept only as history; see
app/db/models.py's "Sessions / messages — Redis, not Postgres" note for why
this never became a Postgres table pair).

Talks to Upstash over its HTTPS REST API (via the `upstash-redis` SDK), not
a redis:// TCP connection — that's what UPSTASH_REDIS_REST_URL/_TOKEN in
.env actually are, and it's a different wire protocol/client library than
plain redis-py. See app/config.py's upstash_redis_rest_url/_token.

Same method contract as the file-backed store it replaces (create/
get_or_create/append/set_field/add_checkpoint/list_checkpoints/
restore_checkpoint/list/get) — CopilotService (app/services.py) constructs
one `SessionStore` and calls these methods without knowing or caring which
backend is behind them, same seam discipline as AIProvider/CodeSandbox (see
app/providers.py, app/sandbox.py).

On-Redis shape, one string + one sorted-set-index per session:

    session:<id>              -- STRING, JSON-encoded session dict (same
                                  shape the old file-backed store wrote:
                                  session_id, created_at, updated_at,
                                  messages[], memory_state, pending_skill_run,
                                  turn_checkpoint, pending_deck_builder,
                                  last_deck_spec, checkpoints[])
    sessions:by_updated        -- ZSET, member=session_id, score=updated_at
                                  epoch millis — lets list() page newest-first
                                  without a full KEYS/SCAN + per-key read.

No in-memory cache layer (unlike the old file-backed store) — every method
round-trips to Upstash over HTTPS; that round trip IS the source of truth.
A process-local cache would risk serving stale data across multiple app
workers/processes sharing one Upstash database, which a per-process cache
can't ever see invalidated.

Retention is Upstash's own problem, not this module's: point Upstash's own
eviction/TTL policy at the `session:*` keyspace if sessions should expire.
This store itself never expires anything — matches the old file-backed
store's behavior (a session file was kept forever unless something else
deleted it)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from upstash_redis.asyncio import Redis
from upstash_redis.errors import UpstashError

logger = logging.getLogger(__name__)

PREVIEW_CHARS = 120
_SESSION_KEY = "session:{}"
_INDEX_KEY = "sessions:by_updated"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000


class SessionStore:
    """Async Upstash-Redis-backed session/conversation history. See module
    docstring for the on-Redis shape.

    Every public method here is `async def` — this is a deliberate,
    unavoidable change from the old file-backed SessionStore's sync methods
    (disk I/O was sync there; every Upstash call here is an HTTPS round
    trip). Every call site in app/services.py must `await` these calls; see
    that file's own diff for the corresponding `async def`/`await`
    additions at each call site."""

    def __init__(self, url: str, token: str, redis_client: "Redis | None" = None):
        # redis_client lets tests inject a fake client without a real
        # Upstash database — mirrors how app/db/engine.py's build_engine
        # takes a URL, not a pre-built engine, but tests need the seam.
        #
        # An empty url/token is NOT validated here — the SDK only raises on
        # the first actual HTTP call, not at construction — so
        # CopilotService() (the module-level singleton at import time,
        # app/services.py) never crashes just because Upstash isn't
        # configured; the failure surfaces on the first real session
        # read/write instead. Same "let the real error surface at use time,
        # not at import time" posture as build_provider()/build_vector_store()
        # tolerating an unset GEMINI_API_KEY/QDRANT_URL.
        self._redis = redis_client or Redis(url=url or "https://unconfigured", token=token or "unconfigured")

    async def _write(self, session: dict) -> None:
        session["updated_at"] = _now_iso()
        session_id = session["session_id"]
        payload = json.dumps(session)
        try:
            pipe = self._redis.multi()
            pipe.set(_SESSION_KEY.format(session_id), payload)
            pipe.zadd(_INDEX_KEY, {session_id: _now_ms()})
            await pipe.exec()
        except UpstashError as exc:
            # An Upstash write failure shouldn't take the chat request
            # down — same posture as the old file-backed store's OSError
            # handling, just there is no in-memory fallback layer here to
            # fall back to, so this turn's session update is genuinely lost
            # if Upstash is unreachable. Logged loudly rather than silently
            # swallowed.
            logger.warning("Could not persist session %s to Upstash: %s", session_id, exc)

    async def _load(self, session_id: str) -> dict | None:
        try:
            raw = await self._redis.get(_SESSION_KEY.format(session_id))
        except UpstashError as exc:
            logger.warning("Could not read session %s from Upstash: %s", session_id, exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Corrupt session JSON for %s: %s", session_id, exc)
            return None

    async def create(self) -> dict:
        session_id = str(uuid4())
        now = _now_iso()
        session = {"session_id": session_id, "created_at": now, "updated_at": now, "messages": []}
        await self._write(session)
        return session

    async def get_or_create(self, session_id: str | None) -> dict:
        if session_id:
            existing = await self._load(session_id)
            if existing is not None:
                return existing
        return await self.create()

    async def append(self, session_id: str, role: str, content: str) -> None:
        session = await self._load(session_id)
        if session is None:
            return
        session["messages"].append({"role": role, "content": content, "at": _now_iso()})
        await self._write(session)

    async def set_field(self, session_id: str, key: str, value) -> None:
        """Persists arbitrary session-scoped state alongside the transcript —
        e.g. an in-progress skill Q&A (see CopilotService._start_chat_skill_run).
        A no-op for an unknown session, matching append()'s behavior."""
        session = await self._load(session_id)
        if session is None:
            return
        session[key] = value
        await self._write(session)

    async def add_checkpoint(self, session_id: str, label: str) -> dict | None:
        """Snapshots the session's current message_count + memory_state as a
        new named checkpoint the user can restore to later (see
        restore_checkpoint). Returns the created checkpoint dict, or None for
        an unknown session — same no-op-on-unknown posture as append()/
        set_field(), since there's nothing meaningful to snapshot."""
        session = await self._load(session_id)
        if session is None:
            return None
        checkpoint = {
            "checkpoint_id": str(uuid4()),
            "label": label,
            "created_at": _now_iso(),
            "message_count": len(session.get("messages", [])),
            "memory_state": session.get("memory_state"),
        }
        checkpoints = session.setdefault("checkpoints", [])
        checkpoints.append(checkpoint)
        await self._write(session)
        return checkpoint

    async def list_checkpoints(self, session_id: str) -> list[dict]:
        """This session's saved checkpoints, oldest first (creation order).
        Raises KeyError for an unknown session, matching get()'s contract —
        unlike add_checkpoint's no-op posture, listing implies the caller
        already believes the session exists."""
        session = await self.get(session_id)
        return list(session.get("checkpoints", []))

    async def restore_checkpoint(self, session_id: str, checkpoint_id: str) -> dict:
        """Rolls the session back to a previously saved checkpoint: truncates
        `messages` to the checkpoint's message_count, resets `memory_state`
        to its snapshot, and clears any pending_skill_run/turn_checkpoint
        marker (both describe in-progress work that no longer applies once
        history has been rewound under it). Every field changes in one
        _write() call so Upstash never shows a torn intermediate state to a
        concurrent reader. This is truncation, not branching — messages
        after the checkpoint are discarded, not preserved on some side
        branch; callers (the UI) must confirm this destructively before
        calling.

        Raises KeyError if the session or the checkpoint_id doesn't exist."""
        session = await self.get(session_id)  # raises KeyError if unknown
        checkpoint = next(
            (c for c in session.get("checkpoints", []) if c["checkpoint_id"] == checkpoint_id), None,
        )
        if checkpoint is None:
            raise KeyError("Checkpoint not found")
        session["messages"] = session.get("messages", [])[: checkpoint["message_count"]]
        session["memory_state"] = checkpoint["memory_state"]
        session["pending_skill_run"] = None
        session["turn_checkpoint"] = None
        await self._write(session)
        return session

    async def list(self) -> list[dict]:
        """Summaries (no message bodies) for a session picker — newest first.
        Reads the sessions:by_updated ZSET (score = updated_at epoch millis,
        maintained by every _write()) rather than scanning every session:*
        key — the ZSET already holds the exact newest-first order, no
        per-session tiebreak logic needed (unlike the old file-backed
        store's mtime_ns/write-seq tiebreak, which existed only because the
        filesystem's own mtime resolution wasn't fine-grained enough; a ZSET
        score doesn't have that problem)."""
        try:
            session_ids = await self._redis.zrange(_INDEX_KEY, 0, -1, rev=True)
        except UpstashError as exc:
            logger.warning("Could not read session index from Upstash: %s", exc)
            return []
        summaries = []
        for session_id in session_ids:
            session = await self._load(session_id)
            if session is None:
                continue
            messages = session.get("messages", [])
            first_user = next((m["content"] for m in messages if m.get("role") == "user"), "")
            summaries.append({
                "session_id": session["session_id"],
                "created_at": session["created_at"],
                "updated_at": session.get("updated_at", session["created_at"]),
                "message_count": len(messages),
                "preview": first_user[:PREVIEW_CHARS],
            })
        return summaries

    async def get(self, session_id: str) -> dict:
        session = await self._load(session_id)
        if session is None:
            raise KeyError("Session not found")
        return session
