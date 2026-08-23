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
from dataclasses import dataclass, field

_WORD_RE = re.compile(r"\w+")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

EMBEDDING_DIM = 256


# --- PII redaction --------------------------------------------------------
#
# Lives here (not app/services.py, where GuardrailService's check_input/
# check_output originally defined this) because it needs to be callable from
# app/memory.py too (the compact-memory summarizer's own defensive pass) —
# app/services.py already imports app/memory.py (`from .memory import
# BUFFER_SIZE, CompactMemoryState, compact_history`), so memory.py importing
# GuardrailService back from services.py would be circular. app/retrieval.py
# is this app's existing home for small, pure, dependency-free functions
# already shared across modules (tokenize, cosine_similarity, ...) and has
# no imports from services.py/memory.py/agents.py in either direction, so
# it's the natural shared leaf for this primitive. GuardrailService
# (app/services.py) delegates to this exact function for check_input/
# check_output; AutoGenOrchestrator (app/agents.py) calls it via
# GuardrailService.redact_context_pii for retrieved RAG chunks; app/memory.py
# calls it directly (no GuardrailService instance available there by design).

# Each pattern is (category, compiled regex, mask). Order matters: card
# before phone (both are digit runs) so a 16-digit card number, once
# redacted, can't also get partially eaten by the phone pattern.
_PII_PATTERNS: tuple[tuple[str, re.Pattern, str], ...] = (
    ("email", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"), "[REDACTED_EMAIL]"),
    ("credit_card", re.compile(r"\b\d(?:[ -]?\d){12,15}\b"), "[REDACTED_CARD]"),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    ("phone", re.compile(r"\b(?:\+?\d{1,2}[ -]?)?\(?\d{3}\)?[ -]\d{3}[ -]\d{4}\b"), "[REDACTED_PHONE]"),
    ("ip_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[REDACTED_IP]"),
)


def redact_pii(text: str) -> tuple[str, list[dict]]:
    """Masks PII in `text`, returning (redacted_text, findings) where
    findings is [{"category": "email", "count": 2}, ...] — one entry per
    category that actually matched, never the raw matched value itself (see
    .claude/rules/guardrails.md: never expose the sensitive value, only what
    kind of thing was caught). Idempotent — running this again on
    already-masked text finds nothing new, so redacting twice (e.g. once in
    a retrieval-time pass, once defensively downstream) is always safe."""
    findings: list[dict] = []
    redacted = text
    for category, pattern, mask in _PII_PATTERNS:
        count = 0

        def _sub(m: re.Match, _mask=mask) -> str:
            nonlocal count
            count += 1
            return _mask

        redacted = pattern.sub(_sub, redacted)
        if count:
            findings.append({"category": category, "count": count})
    return redacted, findings


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

def _pack_paragraphs(paragraphs: list[str], chunk_size: int, overlap: int) -> list[str]:
    """The packing algorithm itself, factored out of chunk_text so
    chunk_blocks (below) can reuse it for a section's prose blocks without
    going back through clean_text/paragraph-splitting on already-structured
    input. Bounded to ~chunk_size chars per chunk, falling back to
    sentence-by-sentence packing for a paragraph that alone exceeds
    chunk_size, then a small overlap carried into each following chunk so
    context isn't lost at a boundary."""
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


def chunk_text(text: str, chunk_size: int = 600, overlap: int = 80) -> list[str]:
    """Paragraph-aware chunking, bounded to ~chunk_size chars, with a small
    overlap carried into each following chunk so context isn't lost at a
    boundary. Falls back to sentence-by-sentence packing for paragraphs that
    alone exceed chunk_size.

    Kept as the flat-text entry point (unstructured input, no document
    structure to key off) — see chunk_blocks() for the structure-aware
    (heading/table/parent-child) chunker used everywhere ingestion has real
    ExtractedBlock structure to work with (app/extraction.py)."""
    cleaned = clean_text(text)
    if not cleaned:
        return []
    paragraphs = [p.strip() for p in _PARAGRAPH_RE.split(cleaned) if p.strip()] or [cleaned]
    return _pack_paragraphs(paragraphs, chunk_size, overlap)


# --- structure-aware, parent-child chunking ---------------------------------

@dataclass
class ParentChildChunk:
    """One retrieval unit. `text` is the child — what gets embedded and
    searched; `parent_text` is that child's full parent section — what gets
    sent to the model as grounding context, so a small precise match doesn't
    lose its surrounding meaning. `heading_path` is the breadcrumb of
    headings above this chunk (["Document", "Chapter 2", "Refund Policy"]),
    empty for a document with no headings at all. `content_hash` is
    sha256(text) — the child text's own hash, not the parent's — used by
    RAGStore's incremental reindex to diff old vs. new chunks and re-embed
    only what actually changed."""
    text: str
    parent_text: str
    heading_path: list[str] = field(default_factory=list)
    is_table: bool = False
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def chunk_blocks(blocks, chunk_size: int = 600, overlap: int = 80) -> list[ParentChildChunk]:
    """Structure-aware chunking over app/extraction.py's ExtractedBlock
    list. One unified, format-aware pipeline — not a strategy the caller
    picks — the content itself decides:

    - A heading starts a new *parent section*: that heading plus every block
      until the next same-or-higher-level heading. A document with no
      headings at all is one single implicit section (heading_path stays
      empty, parent_text is the whole document).
    - Within a section, a table block becomes its own atomic child chunk —
      never merged with adjacent prose, never split mid-row (splitting a
      table mid-row is the single most common cause of "the model can't
      answer a table question" in RAG systems).
    - Prose blocks in a section are packed into child chunks via the same
      paragraph/sentence-packing chunk_text already used (bounded to
      chunk_size/overlap).
    - Every child chunk in a section carries that section's full text as
      parent_text — the grounding context sent to the model is the whole
      section, even though only the small child chunk was what matched.

    `blocks` is `list[ExtractedBlock]` (app/extraction.py) — typed loosely
    here (duck-typed on .kind/.level/.text) to avoid a retrieval.py ->
    extraction.py import for a pure-dataclass shape neither module owns
    exclusively.
    """
    if not blocks:
        return []

    # Split into sections: each section is (heading_path, [blocks in it]).
    sections: list[tuple[list[str], list]] = []
    current_path: list[str] = []
    current_blocks: list = []

    def start_new_section() -> None:
        nonlocal current_blocks
        if current_blocks:
            sections.append((list(current_path), current_blocks))
        current_blocks = []

    for block in blocks:
        if block.kind == "heading":
            start_new_section()
            level = block.level or 1
            # Truncate the path to this heading's level, then append it —
            # e.g. an h2 after an h1/h2/h3 path replaces the h2 and h3 with
            # itself, keeping only ancestors strictly above its own level.
            current_path = current_path[: level - 1] + [block.text]
            current_blocks.append(block)
        else:
            current_blocks.append(block)
    start_new_section()

    if not sections:
        return []

    chunks: list[ParentChildChunk] = []
    for heading_path, section_blocks in sections:
        parent_text = "\n\n".join(b.text for b in section_blocks if b.text.strip())
        prose_buffer: list[str] = []

        def flush_prose() -> None:
            if not prose_buffer:
                return
            for child_text in _pack_paragraphs(list(prose_buffer), chunk_size, overlap):
                chunks.append(ParentChildChunk(text=child_text, parent_text=parent_text, heading_path=heading_path))
            prose_buffer.clear()

        for block in section_blocks:
            if block.kind == "table":
                flush_prose()
                if block.text.strip():
                    chunks.append(ParentChildChunk(
                        text=block.text, parent_text=parent_text, heading_path=heading_path, is_table=True,
                    ))
            elif block.kind == "heading":
                # The heading's own text is part of parent_text (joined
                # above) but isn't itself a searchable child chunk — a query
                # matching only a heading string with no body would retrieve
                # a chunk with nothing to ground an answer in.
                continue
            elif block.text.strip():
                prose_buffer.append(block.text)
        flush_prose()

    return chunks


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


# --- dedup / ordering / context compression (post-rerank query stages) --------

def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedupe_results(results: list[dict], jaccard_threshold: float = 0.9) -> list[dict]:
    """Drops near-duplicate chunks from a ranked result list, keeping the
    higher-scored copy of each. Matters once parent-child chunking + chunk
    overlap can surface two children of the same parent section (or two
    overlapping chunks from adjacent documents) both scoring well for the
    same query — showing both wastes context-budget on redundant text and
    dilutes the citation list.

    Exact duplicates (identical `content_hash`) are always dropped. Near-
    duplicates are caught via token-set Jaccard similarity over each
    result's `tokens` field — the same tokenization already computed for
    BM25/rerank, reused here rather than re-tokenizing. `results` is
    expected sorted by relevance already (as RAGStore.search's reranked
    list is); the FIRST (highest-ranked) occurrence of a duplicate group is
    kept."""
    seen_hashes: set[str] = set()
    kept: list[dict] = []
    kept_token_sets: list[set[str]] = []
    for result in results:
        content_hash = result.get("content_hash")
        if content_hash and content_hash in seen_hashes:
            continue
        tokens = set(result.get("tokens", []))
        if any(_jaccard(tokens, kept_tokens) >= jaccard_threshold for kept_tokens in kept_token_sets):
            continue
        if content_hash:
            seen_hashes.add(content_hash)
        kept.append(result)
        kept_token_sets.append(tokens)
    return kept


def compress_context(results: list[dict], token_budget: int) -> list[dict]:
    """Orders by rerank score (already the case for RAGStore.search's input
    here, but enforced explicitly so this function is correct standalone
    too) and greedily includes whole chunks until `token_budget` (a
    character-count proxy — consistent with this app's existing
    prompt[:2000]-style char-budgeting elsewhere, no tokenizer dependency
    added) is hit.

    Budget is measured against each result's grounding context — its
    `parent_text` when present (parent-child chunking, app/retrieval.py:
    chunk_blocks), falling back to `snippet`/`text` for chunks with no
    parent (flat chunk_text() ingestion). The last chunk that would exceed
    the budget gets its parent_text TRUNCATED rather than dropped entirely,
    so grounding never silently vanishes at the boundary — the model still
    sees a same, if truncated, excerpt of the most relevant remaining
    chunk instead of only the chunks that fit whole."""
    ordered = sorted(results, key=lambda r: r.get("rerank_score", 0.0), reverse=True)
    included: list[dict] = []
    remaining = token_budget
    for result in ordered:
        context_text = result.get("parent_text") or result.get("snippet") or result.get("text", "")
        length = len(context_text)
        if remaining <= 0:
            break
        if length <= remaining:
            included.append(result)
            remaining -= length
        else:
            truncated = dict(result)
            key = "parent_text" if result.get("parent_text") else ("snippet" if result.get("snippet") else "text")
            truncated[key] = context_text[:remaining].rstrip() + "…"
            included.append(truncated)
            remaining = 0
    return included
