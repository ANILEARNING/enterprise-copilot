"""File-backed document-metadata storage — the RAG v2 counterpart to
app/storage.py::SessionStore, same pattern deliberately mirrored: one JSON
file per record under a data folder, an in-memory cache layered over
disk-as-source-of-truth, a small CRUD-ish method surface.

Vectors live in a VectorStore (app/vector_store.py — Qdrant Cloud or the
in-memory fallback); this store holds everything else a document needs to
survive a restart: full text, filename, timestamps, and each of its chunks'
content_hash (keyed by chunk_id) — the exact list RAGStore's incremental
reindex diffs old vs. new chunks against, so only genuinely changed chunks
get re-embedded and re-upserted on update.

    data/documents/<document_id>.json
    {
      "document_id": "...", "filename": "...", "content": "...",
      "status": "indexed"|"error", "created_at": "<iso8601>", "updated_at": "<iso8601>",
      "size": <bytes>, "tenant_id": "...",
      "chunk_hashes": {"<chunk_id>": "<content_hash>", ...}
    }
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "documents"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DocumentStore:
    """File-backed document metadata. See module docstring for the on-disk
    schema. An in-memory cache avoids re-reading a document's file on every
    request within the same process lifetime; every write still goes
    straight to disk so documents survive a restart even though their
    vectors live in a separate VectorStore."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict] = {}
        # Monotonic in-process write counter, keyed by document_id — the
        # ordering tiebreak list() actually needs. Filesystem mtime_ns
        # (SessionStore's approach) is NOT fine-grained enough on every
        # filesystem to distinguish two writes microseconds apart (verified:
        # two back-to-back writes on this Windows filesystem can land on the
        # identical mtime_ns value) — this counter can't tie under any
        # write cadence, since it's a plain in-memory increment, not a
        # wall-clock reading.
        self._write_seq: dict[str, int] = {}
        self._next_seq = 0

    def _path(self, document_id: str) -> Path:
        # document_id is our own uuid4, but never trust it as a path component blindly.
        safe_id = re.sub(r"[^A-Za-z0-9-]", "", document_id)
        return self.data_dir / f"{safe_id}.json"

    def _write(self, document: dict) -> None:
        document["updated_at"] = _now_iso()
        self._next_seq += 1
        self._write_seq[document["document_id"]] = self._next_seq
        try:
            self._path(document["document_id"]).write_text(json.dumps(document, indent=2), encoding="utf-8")
        except OSError as exc:
            # A disk-write failure shouldn't take the request down; the
            # in-memory cache still has this document, it just won't survive a restart.
            logger.warning("Could not persist document %s: %s", document["document_id"], exc)
        self._cache[document["document_id"]] = document

    def _load(self, document_id: str) -> dict | None:
        if document_id in self._cache:
            return self._cache[document_id]
        path = self._path(document_id)
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read document file %s: %s", path, exc)
            return None
        self._cache[document_id] = document
        return document

    def create(
        self, filename: str, content: str, tenant_id: str, document_id: str | None = None,
        blocks: list[dict] | None = None,
    ) -> dict:
        """`blocks`: the document's structural blocks (app/extraction.py
        ExtractedBlock, pre-serialized to plain dicts by the caller —
        RAGStore.add) — persisted so a restart's chunk rebuild
        (RAGStore._rebuild_chunk_meta_from_document) can re-run the SAME
        structure-aware chunking that originally indexed this document,
        instead of falling back to flat paragraph chunking. None (a caller
        with no structural extraction, or a direct rag.add(filename, text)
        test call) means "flatten via chunk_text at reindex time," matching
        pre-structural-chunking behavior."""
        document_id = document_id or str(uuid4())
        now = _now_iso()
        document = {
            "document_id": document_id, "filename": filename, "content": content,
            "status": "indexed", "created_at": now, "updated_at": now,
            "size": len(content.encode("utf-8")), "tenant_id": tenant_id,
            "chunk_hashes": {}, "blocks": blocks,
        }
        self._write(document)
        return document

    def update(
        self, document_id: str, filename: str | None, content: str | None,
        blocks: list[dict] | None = None,
    ) -> dict:
        document = self._load(document_id)
        if document is None:
            raise KeyError("Document not found")
        if filename is not None:
            document["filename"] = filename
        if content is not None:
            document["content"] = content
            document["size"] = len(content.encode("utf-8"))
            # blocks only makes sense alongside new content — an update with
            # no new content (e.g. filename-only rename) keeps whatever
            # blocks were already stored for the existing content.
            document["blocks"] = blocks
        document["status"] = "indexed"
        self._write(document)
        return document

    def set_chunk_hashes(self, document_id: str, chunk_hashes: dict[str, str]) -> None:
        """Replaces the document's full chunk_id -> content_hash map — called
        once per reindex with the final post-diff state, not incrementally,
        so a partially-failed reindex never leaves stale/orphaned entries."""
        document = self._load(document_id)
        if document is None:
            return
        document["chunk_hashes"] = chunk_hashes
        self._write(document)

    def delete(self, document_id: str) -> None:
        if self._load(document_id) is None:
            raise KeyError("Document not found")
        self._cache.pop(document_id, None)
        try:
            self._path(document_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete document file for %s: %s", document_id, exc)

    def list(self) -> list[dict]:
        """All documents, newest-updated first. Ties on `updated_at` (two
        writes landing on the same ISO-timestamp string under fast/loaded
        conditions — verified, not just theoretical) break on this
        process's own write-order counter when available (a document this
        process actually wrote), falling back to filesystem mtime_ns for a
        document only ever loaded fresh from disk (e.g. right after a
        restart, never written this process lifetime). Offset well past any
        real mtime_ns value so the two scales can never cross — "written
        this process" always tiebreaks after "loaded from disk, unwritten
        this session," which is the correct relative order regardless of
        how the two numbers compare in isolation."""
        documents = []
        for path in self.data_dir.glob("*.json"):
            document = self._load(path.stem)
            if document is None:
                continue
            seq = self._write_seq.get(document["document_id"])
            tiebreak = (2**63 + seq) if seq is not None else path.stat().st_mtime_ns
            documents.append((document, tiebreak))
        documents.sort(key=lambda pair: (pair[0].get("updated_at", ""), pair[1]), reverse=True)
        return [d for d, _ in documents]

    def get(self, document_id: str) -> dict:
        document = self._load(document_id)
        if document is None:
            raise KeyError("Document not found")
        return document
