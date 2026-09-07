# Knowledge / RAG

## Document lifecycle (P0 UI)

The Knowledge section supports the full lifecycle end to end:

- Add new document — pasted plain text, or an uploaded file (.txt/.md/.csv/.json/.log/.html
  as plain text; .pdf/.docx/.xlsx as a base64-encoded upload, extracted server-side into
  structured blocks — see Pipeline design below)
- List documents
- View/edit existing document
- Update document content/name
- Delete document
- Re-index after update (immediate, incremental — only changed chunks are re-embedded)

P0 APIs (all POST — no query-parameter behavior):
- POST /api/rag/document/add
- POST /api/rag/document/list
- POST /api/rag/document/get
- POST /api/rag/document/update
- POST /api/rag/document/delete
- POST /api/rag/status

Document changes update the index/state immediately; there is no separate "re-index" call
to make. An update re-chunks the document's current content and diffs each chunk's content
hash against what was indexed last time — only new/changed chunks are re-embedded and
upserted; unchanged chunks keep their existing vector untouched.

## Pipeline design

### Ingestion (document → index)

```
documents → extract (format-aware) → clean → chunk (structure-aware, parent-child)
          → metadata (+tenant_id, +content_hash) → embed → upsert (vector DB + BM25 + document store)
```

- **Extract** — format-aware dispatch by filename/signature (`app/extraction.py`), producing
  an `ExtractedDocument` (flat text + ordered structural `blocks`: headings, paragraphs,
  tables) rather than just flat text, so downstream chunking can use real document structure.
- **Clean** — normalize whitespace/encoding, strip boilerplate, drop non-content noise.
- **Chunk** — structure-aware, parent-child chunking (`retrieval.chunk_blocks`): headings
  start a new parent section, tables are atomic (never split mid-row), prose packs into
  sentence/paragraph-bounded child chunks with overlap. Each child chunk carries its parent
  section's full text as grounding context and a `heading_path` breadcrumb.
- **Metadata** — attach `document_id`, `filename`, `chunk_index`, `tenant_id`, `content_hash`,
  `heading_path`, `is_table`, timestamps, and embedding provenance to each chunk.
- **Embed** — compute a vector embedding per chunk.
- **Upsert** — persist chunk vector + payload in the vector store (Qdrant Cloud, or an
  in-memory fallback), the chunk text in a BM25 index for the lexical leg, and the document's
  full content + per-chunk hashes in a file-backed document store — only chunks whose content
  hash changed since the last index are actually re-embedded/upserted.

### Query (question → cited answer)

```
user query → understand → hybrid retrieve (vector + BM25) → fusion → rerank
           → dedupe → order → context compression → token budget → answer + citation
```

- **Understand** — normalize/interpret the query (and any conversation context) before
  retrieval.
- **Retrieve** — hybrid search: vector similarity search (tenant-filtered) and BM25 keyword
  search run independently over the chunk index.
- **Fusion** — combine both candidate sets by reciprocal rank fusion.
- **Rerank** — score the fused candidates with a lexical-coverage/density reranker and keep
  the top-N most relevant chunks.
- **Dedupe** — drop near-duplicate chunks from the candidate pool (exact content-hash match,
  or high token-set overlap), keeping the higher-scored copy.
- **Order + context compression** — order by rerank score and greedily include whole chunks'
  parent context until the token budget is hit, truncating rather than dropping the boundary
  chunk so grounding never silently vanishes.
- **Answer + citation** — the model answers from that context only, and the response cites
  the source document(s)/chunk(s) it drew from. Never fabricate citations or unsupported
  facts.

## Multi-tenancy

Every chunk's vector-store payload and document-store record carries `tenant_id`
(`RAG_TENANT_ID`, default `"default"`). Every search filters on it, even though v1 runs a
single fixed tenant — adding a second tenant later is a config/auth change (a new
`tenant_id` value, and a way to select it per request), not a schema migration or backfill.

## v2 implementation status

The pipeline above is implemented end to end (`app/extraction.py`, `app/retrieval.py`,
`app/vector_store.py`, `app/document_store.py`, `app.services.RAGStore`). No stage here is
presented as more than it is:

| Stage | v2 behavior |
|---|---|
| Extract | Format-aware dispatch (`app/extraction.py`, `extract_document`): plain text/Markdown/CSV/JSON pass through with heading/table detection where the format has structure; DOCX via `python-docx` (`iter_inner_content` for true document order); XLSX via `openpyxl` (one table block per sheet); HTML via `beautifulsoup4`; PDF via `pypdf`, including image-only/scanned PDFs OCR'd through Gemini Vision (`AIProvider.complete(images=...)`) when `AI_MODE=configured` and a Gemini key is set. Encrypted PDFs, corrupt DOCX/XLSX containers, and image-only PDFs with no vision provider configured are rejected with a clear error rather than silently indexing nothing. |
| Clean | Implemented — whitespace/newline normalization (`retrieval.clean_text`). |
| Chunk | Structure-aware, parent-child chunking (`retrieval.chunk_blocks`, `ParentChildChunk`): headings start new parent sections, tables are atomic chunks, prose packs via paragraph/sentence bounding with overlap; each chunk carries `parent_text`, `heading_path`, and a `content_hash` used for incremental re-embedding. A flat-text back-compat wrapper (`chunk_text`) remains for callers with no structural blocks. |
| Metadata | Implemented — `document_id`, `filename`, `chunk_index`, `tenant_id`, `content_hash`, `heading_path`, `is_table`, timestamps, embedding provenance per chunk. |
| Embedding | Implemented behind `EmbeddingProvider` (`app/providers.py`), unchanged from v1: `AI_MODE=mock` (default) uses the deterministic offline hash embedder; `AI_MODE=configured` builds a `ChainEmbeddingProvider` per `MODEL_PROVIDER` (Ollama or Azure as the primary depending on which is configured, then Gemini as a cross-provider safety net, hash embedder always the final always-available step). Chunks embedded by different providers are guarded by a dimension check so mismatched vector spaces are never compared. |
| Vector DB | Implemented behind `VectorStore` (`app/vector_store.py`): `QdrantVectorStore` (Qdrant Cloud or any Qdrant instance) when `AI_MODE=configured` and both `QDRANT_URL`/`QDRANT_API_KEY` are set — collection and its `tenant_id` payload index are created lazily on first use, sized to the actual embedding dimension; `InMemoryVectorStore` (an exhaustive cosine scan) is the always-available fallback otherwise, same graceful-degrade posture as the embedding provider chain. A configured-but-unreachable Qdrant falls back to in-memory at construction time, not per-call, so a running store never silently splits one tenant's data across two backends. |

**Caveat — switching embedding providers can break an existing Qdrant
collection.** The collection is sized to whatever embedding dimension its
*first* point had (Gemini's `gemini-embedding-001` is 3072-dim, Azure's
`text-embedding-3-small` is 1536-dim, Ollama's `nomic-embed-text` is
768-dim) — it does NOT resize itself when `MODEL_PROVIDER` changes to a
provider with a different dimension. Every later query/upsert against the
mismatched collection fails outright with Qdrant's own `400 Bad Request:
Vector dimension error`, not a graceful degrade — this bypasses the
embedding-provider fallback chain entirely, since the failure is Qdrant
rejecting the request, not the embedder failing to produce a vector. Hit
live switching this app to `MODEL_PROVIDER=azure` against a collection
already indexed under Gemini. Fix: drop the stale collection (e.g. via
`qdrant_client.AsyncQdrantClient.delete_collection`) and re-add every
existing document through `RAGStore.add` (delete-then-re-add each one first
if easier) — a fresh, prior-chunk-hash-free ingestion re-embeds every chunk
against the now-active provider and `_ensure_collection` recreates the
collection at the correct new dimension on the next upsert. There is no
automatic re-index-on-provider-switch; this is a manual, one-time step
after intentionally changing providers, not routine maintenance.
| Document store | Implemented — `DocumentStore` (`app/document_store.py`), file-backed (one JSON per document, mirrors `SessionStore`), persists full content, structural blocks, and per-chunk content hashes so a restart recovers search/citations without re-embedding anything. |
| Retrieve | Hybrid — vector leg (Qdrant or in-memory, tenant-filtered) + pure-Python BM25 leg (`retrieval.BM25Index`), combined by reciprocal rank fusion. |
| Rerank | Implemented as a heuristic lexical-coverage/density reranker (`retrieval.lexical_rerank_score`) over the fused candidate pool — not a trained cross-encoder. |
| Dedupe | Implemented — `retrieval.dedupe_results` drops near-duplicate chunks (exact content-hash match or high token-set Jaccard overlap), keeping the higher-scored copy. |
| Context compression / token budget | Implemented — `retrieval.compress_context` orders by rerank score and greedily includes whole chunks until `RAG_CONTEXT_TOKEN_BUDGET` (character-based proxy) is hit, truncating rather than dropping the boundary chunk. |
| Incremental re-embed | Implemented — `RAGStore._reindex` diffs each new chunk's content hash against the document's previously stored hashes; only new/changed chunks are embedded and upserted, removed chunks' vectors are deleted, unchanged chunks' ids/vectors are left completely untouched. |
| Multi-tenancy | Implemented as a single fixed `tenant_id` carried through every chunk's payload/record and filtered on every search — see Multi-tenancy above. |
| Context guardrail | Implemented — retrieved chunks are screened for indirect prompt injection (`GuardrailService.check_context`) before they enter the prompt; flagged chunks are dropped from context and citations. |
| Context / answer + citation | Implemented: the `knowledge-rag` skill (`app/agents.py`) builds the prompt from the screened, reranked, deduped, compressed chunks and the model cites `[filename#chunk_index]`. |

Guardrails wrap the whole request, not just this pipeline: `check_input` runs on the user's
message before retrieval, `check_context` runs on retrieved chunks before they're prompted,
and `check_output` runs on the generated answer before it's returned (see
`.claude/rules/guardrails.md` and `GuardrailService` in `app/services.py`).

Swapping the lexical reranker for a trained cross-encoder is the main remaining upgrade,
and should happen entirely behind `RAGStore`'s existing interface (`add`, `update`, `delete`,
`list`, `get`, `search`) so `AgentOrchestrator`/`AutoGenOrchestrator` and the routes/UI do not
need to change — the same boundary discipline used for the AutoGen/MAF orchestrator swap (see
`docs/agent-architecture.md`).
