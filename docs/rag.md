# Knowledge / RAG

## Document lifecycle (P0 UI)

The Knowledge section supports the full lifecycle end to end:

- Add new document — pasted plain text, or an uploaded file (.txt/.md/.csv/.json/.log as
  plain text; .pdf as a base64-encoded upload, text-extracted server-side — see Pipeline
  design below)
- List documents
- View/edit existing document
- Update document content/name
- Delete document
- Re-index after update (immediate, in-memory)

P0 APIs (all POST — no query-parameter behavior):
- POST /api/rag/document/add
- POST /api/rag/document/list
- POST /api/rag/document/get
- POST /api/rag/document/update
- POST /api/rag/document/delete

Document changes update the in-memory index/state immediately; there is no separate
"re-index" call to make.

## Pipeline design

### Ingestion (document → index)

```
documents → extract → clean → chunk → metadata → embedding → vector DB (Chroma / Milvus)
```

- **Extract** — pull raw text out of the source (uploaded content today; future file types
  such as PDF/DOCX go through a format-specific extractor first).
- **Clean** — normalize whitespace/encoding, strip boilerplate, drop non-content noise.
- **Chunk** — split into retrieval-sized passages (bounded length, sentence/paragraph aware,
  slight overlap) instead of indexing whole documents.
- **Metadata** — attach `document_id`, `filename`, `chunk_index`, timestamps, and any
  source-specific tags to each chunk so results can be traced back and filtered.
- **Embedding** — compute a vector embedding per chunk.
- **Vector DB** — persist chunk text + metadata + embedding in a vector store (Chroma or
  Milvus) for similarity search, alongside a keyword/BM25 index over the same chunks.

### Query (question → cited answer)

```
user query → understand → retrieve → rerank → context → answer + citation
```

- **Understand** — normalize/interpret the query (and any conversation context) before
  retrieval.
- **Retrieve** — hybrid search: run vector similarity search and BM25 keyword search over
  the chunk index and combine both candidate sets.
- **Rerank** — score the combined candidates with a reranker and keep the top-N most
  relevant chunks.
- **Context** — assemble the reranked chunks into the grounding context passed to the
  model.
- **Answer + citation** — the model answers from that context only, and the response cites
  the source document(s)/chunk(s) it drew from. Never fabricate citations or unsupported
  facts.

## v1 implementation status

The pipeline above is implemented end to end (`app/retrieval.py`, `app.services.RAGStore`),
but every stage is intentionally pure-Python and in-memory — no stage here is presented as
more than it is:

| Stage | v1 behavior |
|---|---|
| Extract | Implemented for plain text (no-op passthrough) and PDF (`app/extraction.py`, via `pypdf`) — a base64-encoded PDF upload is decoded, size-checked against `MAX_UPLOAD_MB`, and its text extracted before the clean/chunk stages. Encrypted or scanned/image-only (no text layer) PDFs are rejected with a clear error rather than silently indexing nothing. Other binary formats (DOCX, etc.) remain future work. |
| Clean | Implemented — whitespace/newline normalization (`retrieval.clean_text`). |
| Chunk | Implemented — paragraph-aware, sentence-packed fallback, bounded size with overlap (`retrieval.chunk_text`). |
| Metadata | Implemented — `document_id`, `filename`, `chunk_index`, timestamps, size per chunk. |
| Embedding | Implemented behind `EmbeddingProvider` (`app/providers.py`), the same pattern as the chat `AIProvider`: `AI_MODE=mock` (default) uses the deterministic offline hash embedder (`retrieval.embed`, `HashEmbeddingProvider`); `AI_MODE=configured` builds a `ChainEmbeddingProvider` per `MODEL_PROVIDER`. When `MODEL_PROVIDER=ollama`: `OllamaEmbeddingProvider` tries `OLLAMA_EMBEDDING_MODEL` (default `nomic-embed-text`) then a short list of other well-known Ollama embedding models on the same host (some Cloud accounts/local daemons only serve a subset), caching whichever one works; if every model on that host fails, the chain tries `GeminiEmbeddingProvider` (`GEMINI_EMBEDDING_MODEL`, default `gemini-embedding-001`) as a cross-provider safety net when `GEMINI_API_KEY` is set. When `MODEL_PROVIDER=gemini`, Gemini is used directly. The offline hash embedder is always the final, always-available step, so a missing/failing provider never breaks ingestion. Chunks embedded by different providers (e.g. mid-chain fallback) are guarded by a dimension check in `RAGStore.search` so mismatched vector spaces are never compared. |
| Vector DB | Implemented as an in-memory cosine-similarity index over chunk embeddings — not Chroma/Milvus. Same role, swappable later behind `RAGStore.search`. |
| Retrieve | Hybrid — vector cosine leg + pure-Python BM25 leg (`retrieval.BM25Index`), combined by reciprocal rank fusion. |
| Rerank | Implemented as a heuristic lexical-coverage/density reranker (`retrieval.lexical_rerank_score`) over the fused candidate pool — not a trained cross-encoder. |
| Context guardrail | Implemented — retrieved chunks are screened for indirect prompt injection (`GuardrailService.check_context`) before they enter the prompt; flagged chunks are dropped from context and citations. |
| Context / answer + citation | Implemented: the `knowledge-rag` skill (`app/agents.py`) builds the prompt from the screened, reranked chunks and the model cites `[filename#chunk_index]`. |

Guardrails wrap the whole request, not just this pipeline: `check_input` runs on the user's
message before retrieval, `check_context` runs on retrieved chunks before they're prompted,
and `check_output` runs on the generated answer before it's returned (see
`.claude/rules/guardrails.md` and `GuardrailService` in `app/services.py`).

The embedding model is already swappable (see above); swapping the in-memory index for
Chroma/Milvus, or the lexical reranker for a trained cross-encoder, should happen the same
way — entirely behind `RAGStore`'s existing interface (`add`, `update`, `delete`, `list`,
`get`, `search`) so `AgentOrchestrator`/`AutoGenOrchestrator` and the routes/UI do not need
to change — the same boundary discipline used for the AutoGen/MAF orchestrator swap (see
`docs/agent-architecture.md`).
