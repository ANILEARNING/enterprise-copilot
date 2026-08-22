"""RAG pipeline primitives, per docs/rag.md.

Ingestion:  extract -> clean -> chunk -> metadata -> embed -> index
Retrieval:  understand -> hybrid retrieve (vector + BM25) -> rerank -> context

Every stage here is pure Python and dependency-free so v1 runs fully
in-memory/offline (no network, no model download), per the project's
local-infra-for-v1 rule. Each stage sits behind a narrow, swappable
interface: `embed()` stands in for a real embedding model, the in-memory
cosine search in `RAGStore.search` stands in for Chroma/Milvus, and
`lexical_rerank_score` stands in for a cross-encoder reranker. Swapping any
of them later should not require changes to `RAGStore`'s callers.
"""
from __future__ import annotations

import hashlib
import math
import re

_WORD_RE = re.compile(r"\w+")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

EMBEDDING_DIM = 256


# --- clean --------------------------------------------------------------

def clean_text(text: str) -> str:
    """Normalize whitespace/newlines before chunking."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def tokenize(text: str) -> list[str]:
    return [t for t in _WORD_RE.findall(text.lower()) if len(t) > 2]


# --- chunk ----------------------------------------------------------------

def chunk_text(text: str, chunk_size: int = 600, overlap: int = 80) -> list[str]:
    """Paragraph-aware chunking, bounded to ~chunk_size chars, with a small
    overlap carried into each following chunk so context isn't lost at a
    boundary. Falls back to sentence-by-sentence packing for paragraphs that
    alone exceed chunk_size."""
    cleaned = clean_text(text)
    if not cleaned:
        return []
    paragraphs = [p.strip() for p in _PARAGRAPH_RE.split(cleaned) if p.strip()] or [cleaned]

    chunks: list[str] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            chunks.append(buffer.strip())
        buffer = ""

    for paragraph in paragraphs:
        candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
        if len(candidate) <= chunk_size:
            buffer = candidate
            continue
        flush()
        if len(paragraph) <= chunk_size:
            buffer = paragraph
            continue
        # paragraph alone exceeds chunk_size: pack sentence by sentence
        sentences = [s.strip() for s in _SENTENCE_RE.split(paragraph) if s.strip()] or [paragraph]
        for sentence in sentences:
            candidate = f"{buffer} {sentence}".strip() if buffer else sentence
            if len(candidate) <= chunk_size or not buffer:
                buffer = candidate
            else:
                flush()
                buffer = sentence
    flush()

    if overlap <= 0 or len(chunks) < 2:
        return chunks
    overlapped = [chunks[0]]
    for previous, current in zip(chunks, chunks[1:]):
        carry = previous[-overlap:]
        overlapped.append(f"{carry} {current}".strip())
    return overlapped


# --- embed ------------------------------------------------------------------

def embed(text: str) -> list[float]:
    """Deterministic, offline bag-of-words embedding via the hashing trick
    (sha1-based, so it's stable across runs/processes — unlike Python's
    salted `hash()`). No model weights, no network call. L2-normalized so
    `cosine_similarity` is a plain dot product."""
    vector = [0.0] * EMBEDDING_DIM
    tokens = tokenize(text)
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha1(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both operands are already L2-normalized


# --- BM25 (keyword leg of hybrid search) -------------------------------------

class BM25Index:
    """Minimal BM25Okapi over an in-memory chunk corpus. Statistics are
    recomputed on each `search` rather than maintained incrementally — fine
    at v1's in-memory scale, and simpler than keeping running DF/length
    counters consistent across add/remove."""

    K1 = 1.5
    B = 0.75

    def __init__(self):
        self._docs: dict[str, list[str]] = {}

    def add(self, doc_id: str, tokens: list[str]) -> None:
        self._docs[doc_id] = tokens

    def remove(self, doc_id: str) -> None:
        self._docs.pop(doc_id, None)

    def search(self, query_tokens: list[str]) -> dict[str, float]:
        if not query_tokens or not self._docs:
            return {}
        n_docs = len(self._docs)
        avg_len = sum(len(t) for t in self._docs.values()) / n_docs or 1.0
        doc_freq: dict[str, int] = {}
        for tokens in self._docs.values():
            for term in set(tokens):
                doc_freq[term] = doc_freq.get(term, 0) + 1

        scores: dict[str, float] = {}
        for doc_id, tokens in self._docs.items():
            if not tokens:
                continue
            length = len(tokens)
            term_counts: dict[str, int] = {}
            for term in tokens:
                term_counts[term] = term_counts.get(term, 0) + 1
            score = 0.0
            for term in query_tokens:
                freq = term_counts.get(term)
                if not freq:
                    continue
                df = doc_freq.get(term, 0)
                idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
                denom = freq + self.K1 * (1 - self.B + self.B * length / avg_len)
                score += idf * (freq * (self.K1 + 1)) / denom
            if score > 0:
                scores[doc_id] = score
        return scores


# --- hybrid fusion ------------------------------------------------------------

def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """Standard hybrid-search combiner: merges independently-scaled ranked
    lists (vector cosine similarity, BM25 score) by rank position rather
    than raw score, so the two signals combine without needing to be
    normalized onto the same scale first."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return fused


# --- rerank -------------------------------------------------------------------

def lexical_rerank_score(query_tokens: list[str], chunk_tokens: list[str]) -> float:
    """Heuristic second-stage reranker: term coverage (how much of the
    query is present) plus term density (how concentrated it is), a signal
    independent of the retrieval-stage scores. Placeholder for a real
    cross-encoder reranker — swap it behind this same function signature."""
    if not query_tokens or not chunk_tokens:
        return 0.0
    unique_query = set(query_tokens)
    chunk_set = set(chunk_tokens)
    coverage = len(unique_query & chunk_set) / len(unique_query)
    hits = sum(1 for t in chunk_tokens if t in unique_query)
    density = min(hits / len(chunk_tokens) * 4, 1.0)
    return 0.7 * coverage + 0.3 * density
