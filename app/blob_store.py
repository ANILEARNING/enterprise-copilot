"""Backblaze B2 blob storage — S3-compatible, reached through the plain
boto3 S3 client (no B2-specific SDK needed) against B2_ENDPOINT. Holds raw
document text (see app/document_store.py::DocumentStore) and skill-run
output files (see app/skills.py::SkillRunService); each caller's own
metadata store keeps only the object key that points here (see
app/db/models.py:DocumentRow.b2_key and SkillRunSession.output_keys below).

Sync, not async, deliberately — matches boto3's own sync-native S3 client
(there is no first-party async S3 SDK worth adopting here) and, more
importantly, matches DocumentStore's existing sync method contract:
RAGStore.__init__ seeds its bootstrap document synchronously, before any
event loop exists (see RAGStore.__init__'s docstring on _run_sync), so
DocumentStore.create/get/update/list must all stay callable from that
synchronous path. Making this async would mean reworking RAGStore's
construction-time bootstrap, not just this module — out of scope for what
a blob-storage backend swap should require."""
from __future__ import annotations

import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class BlobStoreError(RuntimeError):
    """Raised when B2 is unreachable or misconfigured. Callers (DocumentStore)
    let this propagate rather than swallowing it — unlike a guardrail or
    observability write, a failed document upload/read is not something the
    rest of the app can silently proceed without; the caller genuinely needs
    to know their content didn't save (or can't be read back)."""


class BlobStore:
    """Thin wrapper over a boto3 S3 client pointed at B2_ENDPOINT. Deliberately
    minimal — put/get/delete by key, nothing else — matching this app's
    "keep abstractions minimal" architecture rule (.claude/rules/architecture.md).
    Key naming is the caller's responsibility (see DocumentStore._b2_key)."""

    def __init__(self, endpoint: str, bucket: str, key_id: str, application_key: str):
        if not (endpoint and bucket and key_id and application_key):
            raise BlobStoreError(
                "B2 is not fully configured — B2_ENDPOINT, B2_BUCKET_NAME, B2_KEY_ID and "
                "B2_APPLICATION_KEY must all be set (see app/config.py)."
            )
        self.bucket = bucket
        self._client = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id=key_id, aws_secret_access_key=application_key,
        )

    def put_text(self, key: str, content: str) -> None:
        """Uploads UTF-8 text under `key`, overwriting whatever was there —
        matches DocumentStore.update's existing "just replace it" semantics
        for the old file-backed store, no versioning."""
        self.put_bytes(key, content.encode("utf-8"), content_type="text/plain; charset=utf-8")

    def get_text(self, key: str) -> str:
        """Downloads and decodes `key`'s content as UTF-8 text. Raises
        KeyError for a missing object — see get_bytes for the shared
        not-found/error handling this wraps."""
        return self.get_bytes(key).decode("utf-8")

    def put_bytes(self, key: str, content: bytes, content_type: str = "application/octet-stream") -> None:
        """Uploads raw bytes under `key`, overwriting whatever was there —
        used for skill-run output files (docx/pptx/pdf — see
        app/skills.py::SkillRunService), which are binary and have a real
        MIME type worth setting so a browser download/preview behaves
        correctly, unlike put_text's fixed text/plain."""
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=content_type)
        except (BotoCoreError, ClientError) as exc:
            raise BlobStoreError(f"Could not upload {key} to B2: {exc}") from exc

    def get_bytes(self, key: str) -> bytes:
        """Downloads `key`'s raw content. Raises KeyError for a missing
        object — matches DocumentStore's own KeyError-on-unknown-id
        convention (see get()/update()/delete()), so a caller catching
        KeyError today doesn't need a second except clause for a B2-specific
        not-found error."""
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except self._client.exceptions.NoSuchKey:
            raise KeyError(f"No object at {key}") from None
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise KeyError(f"No object at {key}") from None
            raise BlobStoreError(f"Could not read {key} from B2: {exc}") from exc
        except BotoCoreError as exc:
            raise BlobStoreError(f"Could not read {key} from B2: {exc}") from exc
        return response["Body"].read()

    def delete(self, key: str) -> None:
        """Deletes `key`. S3-compatible DELETE is idempotent (no error for
        an already-missing key) — matches boto3/S3 semantics directly rather
        than layering a KeyError check on top, since DocumentStore.delete
        already confirms the document exists (via DocumentStore.get) before
        ever calling this."""
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise BlobStoreError(f"Could not delete {key} from B2: {exc}") from exc
