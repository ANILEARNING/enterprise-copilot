# Context Management (Memory)

Every chat turn — direct, streaming, agent-mode, tool-calling — is grounded
in a **buffered chat completion context**: the last `BUFFER_SIZE` (5) turns
verbatim, plus a running **compact summary** of everything older, instead of
older turns just silently disappearing once a session passes 5 turns.

See `app/memory.py` for the implementation:

- `CompactMemoryState` — `summary` (the running compact summary) +
  `summarized_count` (how many of the session's oldest turns are already
  folded into it, so a later turn only summarizes the *new* overflow).
- `compact_history()` — plain dict-in/dict-out, used by every call site that
  talks to `AIProvider.complete()` directly (the non-streaming direct-chat
  branch in `CopilotService.chat`, `AutoGenOrchestrator`'s plain-completion
  fallback).
- `CompactingChatCompletionContext` — a real
  `autogen_core.model_context.BufferedChatCompletionContext(buffer_size=5)`
  subclass, used everywhere this app builds an actual AutoGen
  `AssistantAgent`: `app/streaming.py`'s direct-chat path and
  `AutoGenOrchestrator`'s MCP tool-calling path. Delegates its own
  compaction decision to `compact_history()`, so the summarization prompt
  exists in exactly one place.

Summarization is a real model call (one per turn, at most, only when new
turns actually fell outside the buffer) — via whichever `AIProvider` is
already configured (`app/providers.py`), never a second/different model. A
summarization failure degrades gracefully: the previous summary is kept,
the raw buffered window is still returned — same posture as every provider
call in this app.

## Session memory across restarts

`SessionStore` (`app/storage.py`) is already file-backed
(`data/sessions/<id>.json`) — the full, unbounded transcript already
survives a process restart. What additionally needs to survive is the
*compaction state* itself (the summary text, and how much has already been
folded into it) — recomputing it from scratch on every restart would work,
but would re-run the summarization call for the entire history again.

`CompactMemoryState.to_dict()`/`.from_dict()` persist via
`SessionStore.set_field(session_id, "memory_state", ...)` — the exact same
mechanism used for the in-progress skill Q&A state — updated at the end of
every turn that touched context management, loaded once at the start of the
next one. This mirrors `ChatCompletionContext.save_state()`/`load_state()`'s
own naming and shape on purpose.
