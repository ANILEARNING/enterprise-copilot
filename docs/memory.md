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

## Visibility: "View context sent to model"

`render_memory_preview()` renders exactly what a turn's buffered/summarized
context resolved to (the synthetic summary turn, if any, plus the raw
buffered recent turns) as flat text, and every call site that talks to a
model attaches it to that turn's `model_call`/`model_call_started` event as
`memory_preview` — alongside the existing `system_preview`/`prompt_preview`.
The frontend (`static/app.js` `contextDisclosureHtml`) shows it in its own
block inside the per-message "🔍 View context sent to model" disclosure, so
what prior-turn context actually reached the model is visible, not just the
current turn's prompt.

Three call sites, three different ways of getting there (same
`compact_history()` math underneath in every case):

- **Plain completion** (`AutoGenOrchestrator.run`, direct-chat fallback) —
  `compact_history()`'s own return value is rendered directly; no extra work.
- **Tool-calling** (`AutoGenOrchestrator._run_with_tools`) — the real
  `CompactingChatCompletionContext` the `AssistantAgent` used is asked for
  its messages again after the turn completes; by then its internal state
  already accounts for any overflow, so this second `get_messages()` call
  makes no extra provider call, it only reads back what was actually sent.
- **Direct streaming** (`app/streaming.py` `stream_chat`) — the context is
  built and asked for its messages *before* the `model_call_started` event
  fires (moved earlier specifically for this), rather than after the turn
  like the other two paths — streaming has no "after the response" moment
  to piggyback on, since the response only exists as deltas.
