"""File-backed session/chat-history storage.

v1 persists each session as one JSON file under a data folder — an interim
step before a real database (per the project owner's direction: "in future
we will design the db tables for that"). The on-disk shape is deliberately
close to a normalized {sessions, messages} table pair so that a future
migration is a straight mapping exercise, not a redesign:

    data/sessions/<session_id>.json
    {
      "session_id": "...", "created_at": "<iso8601>", "updated_at": "<iso8601>",
      "messages": [{"role": "user"|"assistant", "content": "...", "at": "<iso8601>"}, ...]
    }

SessionStore's method surface (create/get_or_create/append/list/get) is the
contract the rest of the app depends on — a future DB-backed implementation
swaps in behind the same methods, matching the AIProvider/CodeSandbox-style
seams already used elsewhere (see app/providers.py, app/sandbox.py).

Session-scoped in-progress-workflow markers — set via set_field(), read back
as plain dict keys, both cleared once whatever they describe finishes:

- "pending_skill_run": the chat-integrated skill Q&A flow's own progress
  (run_id, skill_id, question_ids, index) — spans several distinct chat()
  calls, gates routing itself (see CopilotService.chat). Unrelated in shape
  to turn_checkpoint below; kept separate rather than unified since nothing
  in this app treats them polymorphically as "the same kind of thing."
- "turn_checkpoint": one in-flight chat()/agent-mode turn's last-known
  stage — set by CopilotService.chat()'s emit() closure as
  AutoGenOrchestrator.run()'s on_event stages fire, cleared the instant the
  turn completes (every return path, including a guardrail-blocked input).
  Advisory only: a stale marker (left behind by a crash) never gates a new
  chat() call — it exists purely so a resumed UI can say "your last turn
  didn't finish, here's where it got to," including a hitl_request_id
  pointer when the interruption happened right at a code-execution approval
  (see app/services.py:HitlService, now itself file-backed for exactly this
  case — data/hitl-requests/<request_id>.json).
- "checkpoints": user-triggered named save points (see add_checkpoint/
  list_checkpoints/restore_checkpoint below) — a reference into `messages`
  (message_count) plus a memory_state snapshot, not a duplicated transcript.
  Restoring truncates back to that point; this is deliberately not branching
  (no tree of alternate futures) — the minimal version of "let a user roll
  back to a point they chose," consistent with this app's "keep files and
  abstractions minimal" architecture rule.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "sessions"
PREVIEW_CHARS = 120


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    """File-backed session/conversation history. See module docstring for
    the on-disk schema. An in-memory cache avoids re-reading a session's
    file on every message within the same process lifetime; every write
    still goes straight to disk so history survives a restart."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict] = {}
        # Monotonic in-process write counter, keyed by session_id — see
        # list()'s docstring for why this replaced a pure mtime_ns tiebreak
        # (verified: two writes microseconds apart can land on the identical
        # mtime_ns value on this filesystem, so that alone doesn't actually
        # break the tie it exists for).
        self._write_seq: dict[str, int] = {}
        self._next_seq = 0

    def _path(self, session_id: str) -> Path:
        # session_id is our own uuid4, but never trust it as a path component blindly.
        safe_id = re.sub(r"[^A-Za-z0-9-]", "", session_id)
        return self.data_dir / f"{safe_id}.json"

    def _write(self, session: dict) -> None:
        session["updated_at"] = _now_iso()
        self._next_seq += 1
        self._write_seq[session["session_id"]] = self._next_seq
        try:
            self._path(session["session_id"]).write_text(json.dumps(session, indent=2), encoding="utf-8")
        except OSError as exc:
            # A disk-write failure shouldn't take the chat request down; the
            # in-memory cache still has this turn, it just won't survive a restart.
            logger.warning("Could not persist session %s: %s", session["session_id"], exc)
        self._cache[session["session_id"]] = session

    def _load(self, session_id: str) -> dict | None:
        if session_id in self._cache:
            return self._cache[session_id]
        path = self._path(session_id)
        if not path.is_file():
            return None
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read session file %s: %s", path, exc)
            return None
        self._cache[session_id] = session
        return session

    def create(self) -> dict:
        session_id = str(uuid4())
        now = _now_iso()
        session = {"session_id": session_id, "created_at": now, "updated_at": now, "messages": []}
        self._write(session)
        return session

    def get_or_create(self, session_id: str | None) -> dict:
        if session_id:
            existing = self._load(session_id)
            if existing is not None:
                return existing
        return self.create()

    def append(self, session_id: str, role: str, content: str) -> None:
        session = self._load(session_id)
        if session is None:
            return
        session["messages"].append({"role": role, "content": content, "at": _now_iso()})
        self._write(session)

    def set_field(self, session_id: str, key: str, value) -> None:
        """Persists arbitrary session-scoped state alongside the transcript —
        e.g. an in-progress skill Q&A (see CopilotService._start_chat_skill_run).
        A no-op for an unknown session, matching append()'s behavior."""
        session = self._load(session_id)
        if session is None:
            return
        session[key] = value
        self._write(session)

    def add_checkpoint(self, session_id: str, label: str) -> dict | None:
        """Snapshots the session's current message_count + memory_state as a
        new named checkpoint the user can restore to later (see
        restore_checkpoint). Returns the created checkpoint dict, or None for
        an unknown session — same no-op-on-unknown posture as append()/
        set_field(), since there's nothing meaningful to snapshot."""
        session = self._load(session_id)
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
        self._write(session)
        return checkpoint

    def list_checkpoints(self, session_id: str) -> list[dict]:
        """This session's saved checkpoints, oldest first (creation order).
        Raises KeyError for an unknown session, matching get()'s contract —
        unlike add_checkpoint's no-op posture, listing implies the caller
        already believes the session exists."""
        return list(self.get(session_id).get("checkpoints", []))

    def restore_checkpoint(self, session_id: str, checkpoint_id: str) -> dict:
        """Rolls the session back to a previously saved checkpoint: truncates
        `messages` to the checkpoint's message_count, resets `memory_state`
        to its snapshot, and clears any pending_skill_run/turn_checkpoint
        marker (both describe in-progress work that no longer applies once
        history has been rewound under it). Every field changes in one
        _write() call so the on-disk file never has a torn intermediate
        state. This is truncation, not branching — messages after the
        checkpoint are discarded, not preserved on some side branch; callers
        (the UI) must confirm this destructively before calling.

        Raises KeyError if the session or the checkpoint_id doesn't exist."""
        session = self.get(session_id)  # raises KeyError if unknown
        checkpoint = next(
            (c for c in session.get("checkpoints", []) if c["checkpoint_id"] == checkpoint_id), None,
        )
        if checkpoint is None:
            raise KeyError("Checkpoint not found")
        session["messages"] = session.get("messages", [])[: checkpoint["message_count"]]
        session["memory_state"] = checkpoint["memory_state"]
        session["pending_skill_run"] = None
        session["turn_checkpoint"] = None
        self._write(session)
        return session

    def list(self) -> list[dict]:
        """Summaries (no message bodies) for a session picker — newest first.
        Ties on `updated_at` (two writes can land on the same ISO-timestamp
        string under fast/loaded conditions — observed under CI-like load)
        break on this process's own write-order counter when available (a
        session this process actually wrote), falling back to filesystem
        mtime_ns for one only ever loaded fresh from disk. Offset well past
        any real mtime_ns value so the two scales can never cross — verified
        mtime_ns alone is NOT fine-grained enough on every filesystem to
        break a tie between two fast successive writes (two writes
        microseconds apart can land on the identical mtime_ns value), which
        is why a pure mtime_ns tiebreak wasn't actually solving the problem
        it existed for."""
        summaries = []
        for path in self.data_dir.glob("*.json"):
            session = self._load(path.stem)
            if session is None:
                continue
            messages = session.get("messages", [])
            first_user = next((m["content"] for m in messages if m.get("role") == "user"), "")
            seq = self._write_seq.get(session["session_id"])
            tiebreak = (2**63 + seq) if seq is not None else path.stat().st_mtime_ns
            summaries.append({
                "session_id": session["session_id"],
                "created_at": session["created_at"],
                "updated_at": session.get("updated_at", session["created_at"]),
                "message_count": len(messages),
                "preview": first_user[:PREVIEW_CHARS],
                "_tiebreak": tiebreak,  # not returned to callers, see docstring
            })
        summaries.sort(key=lambda s: (s["updated_at"], s["_tiebreak"]), reverse=True)
        for s in summaries:
            del s["_tiebreak"]
        return summaries

    def get(self, session_id: str) -> dict:
        session = self._load(session_id)
        if session is None:
            raise KeyError("Session not found")
        return session
