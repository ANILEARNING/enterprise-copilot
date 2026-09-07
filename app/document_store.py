"""File-backed document-metadata storage — the RAG v2 counterpart to
app/storage.py::SessionStore (that one now Redis-backed, see
app/session_store.py; kept as a written-out comparison point here since
this store's own migration went a different way — see below).

Vectors live in a VectorStore (app/vector_store.py — Qdrant Cloud or the
in-memory fallback); raw document text lives in Backblaze B2 (see
app/blob_store.py), reached via the object key stored in this store's
`b2_key` field. This store itself still holds everything else a document
needs to survive a restart: filename, timestamps, size, status, and each of
its chunks' content_hash (keyed by chunk_id) — the exact list RAGStore's
incremental reindex diffs old vs. new chunks against, so only genuinely
changed chunks get re-embedded and re-upserted on update.

Metadata deliberately did NOT move to B2 alongside content, and did NOT move
to Postgres in this pass either (the `documents` table in app/db/models.py
exists for a future repository-layer migration, not this one) — this file
still is the metadata source of truth, on disk under data/documents/. Only
`content` moved, from an inline JSON field to a B2 object referenced by
`b2_key`; every other field here works exactly as it did before.

    data/documents/<document_id>.json
    {
      "document_id": "...", "filename": "...", "b2_key": "documents/<id>",
      "status": "indexed"|"error", "created_at": "<iso8601>", "updated_at": "<iso8601>",
      "size": <bytes>, "tenant_id": "...",
      "chunk_hashes": {"<chunk_id>": "<content_hash>", ...}
    }

`content` is deliberately NOT a field in the on-disk JSON above — every
caller still gets it back as a plain dict key (see _load's B2 fetch below),
matching the exact same `doc["content"]` access RAGStore already uses
throughout (app/services.py) — but it now round-trips through a B2 GET
instead of the local JSON file, so RAGStore needed zero changes to keep
working."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .blob_store import BlobStore, BlobStoreError
from .config import settings

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "documents"
_B2_KEY_PREFIX = "documents/"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DocumentStore:
    """File-backed document metadata + B2-backed document content. See
    module docstring for the on-disk/on-B2 split. An in-memory cache avoids
    re-reading a document's metadata file (and, for `content`, its B2
    object) on every request within the same process lifetime; every write
    still goes straight to disk/B2 so documents survive a restart."""

    def __init__(self, data_dir: Path | None = None, blob_store: BlobStore | None = None):
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict] = {}
        # blob_store lets tests inject a fake without real B2 credentials —
        # same seam as app/session_store.py's redis_client param. Built
        # lazily from settings, not at import time, so a module import
        # alone (e.g. from a test that never calls create/get/update) never
        # requires B2_* to be configured — mirrors BlobStore itself failing
        # loudly only once actually used, not at construction.
        self._blob_store = blob_store
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

    def _blobs(self) -> BlobStore:
        if self._blob_store is None:
            self._blob_store = BlobStore(
                endpoint=settings.b2_endpoint, bucket=settings.b2_bucket_name,
                key_id=settings.b2_key_id, application_key=settings.b2_application_key,
            )
        return self._blob_store

    def _path(self, document_id: str) -> Path:
        # document_id is our own uuid4, but never trust it as a path component blindly.
        safe_id = re.sub(r"[^A-Za-z0-9-]", "", document_id)
        return self.data_dir / f"{safe_id}.json"

    def _write(self, document: dict) -> None:
        """Persists metadata to disk and `content` to B2 — always both
        together, since a document dict is never meaningfully "half
        written" from any caller's perspective. `content` is popped off
        before the metadata file is written (it was never part of the
        on-disk JSON shape — see module docstring) and put back on the
        cached dict afterward so the in-memory copy still looks exactly
        like what callers expect from create/update/get."""
        document["updated_at"] = _now_iso()
        self._next_seq += 1
        self._write_seq[document["document_id"]] = self._next_seq

        content = document.pop("content", None)
        try:
            if content is not None:
                self._blobs().put_text(document["b2_key"], content)
        except BlobStoreError as exc:
            # A B2 upload failure shouldn't take the request down any more
            # than the old local-disk OSError did — the in-memory cache
            # still has this document's content for the rest of this
            # process's lifetime, it just won't survive a restart. Logged
            # loudly rather than silently swallowed, same posture as the
            # metadata write's own OSError handling below.
            logger.warning("Could not persist content for document %s to B2: %s", document["document_id"], exc)
        finally:
            if content is not None:
                document["content"] = content

        metadata = {k: v for k, v in document.items() if k != "content"}
        try:
            self._path(document["document_id"]).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not persist document metadata %s: %s", document["document_id"], exc)
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
            logger.warning("Could not read document metadata file %s: %s", path, exc)
            return None
        try:
            document["content"] = self._blobs().get_text(document["b2_key"])
        except KeyError:
            # Metadata survived (on local disk) but its B2 object didn't —
            # treat as "content unavailable" rather than "document doesn't
            # exist": the caller still gets filename/status/etc. back, just
            # with empty content, same graceful-degrade posture as a
            # missing chunk_hash entry elsewhere in this app.
            logger.warning("Document %s metadata exists but its B2 object %s is missing",
                            document_id, document.get("b2_key"))
            document["content"] = ""
        except BlobStoreError as exc:
            logger.warning("Could not read content for document %s from B2: %s", document_id, exc)
            document["content"] = ""
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
            "b2_key": f"{_B2_KEY_PREFIX}{document_id}",
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
        document = self._load(document_id)
        if document is None:
            raise KeyError("Document not found")
        self._cache.pop(document_id, None)
        try:
            self._blobs().delete(document["b2_key"])
        except BlobStoreError as exc:
            logger.warning("Could not delete B2 object for document %s: %s", document_id, exc)
        try:
            self._path(document_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete document metadata file for %s: %s", document_id, exc)

    def list(self) -> list[dict]:
        """All documents, newest-updated first, content included (via
        _load()'s B2 fetch — see its docstring) exactly like get() returns
        it. Ties on `updated_at` (two writes landing on the same
        ISO-timestamp string under fast/loaded conditions — verified, not
        just theoretical) break on this process's own write-order counter
        when available (a document this process actually wrote), falling
        back to filesystem mtime_ns for a document only ever loaded fresh
        from disk (e.g. right after a restart, never written this process
        lifetime). Offset well past any real mtime_ns value so the two
        scales can never cross — "written this process" always tiebreaks
        after "loaded from disk, unwritten this session," which is the
        correct relative order regardless of how the two numbers compare in
        isolation.

        Costs one B2 GET per document not already cached — RAGStore.public()
        (app/services.py) strips content back out for the UI's summary view,
        but RAGStore.__init__'s restart-time chunk_meta rebuild
        (_rebuild_chunk_meta_from_document -> _resolve_blocks) genuinely
        needs content for any document with no persisted `blocks`, so
        list() can't skip fetching it without breaking that caller. A
        knowledge base's document count is small enough for v1 that N
        round-trips here is an acceptable cost, not a real bottleneck —
        revisit if that stops being true."""
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
