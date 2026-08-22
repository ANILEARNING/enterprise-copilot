# Multimodal (Image) Input

Chat turns accept optional image attachments (`ChatRequest.images`, see
`app/models.py:ImageAttachment` — raw base64, no `data:` URI prefix, capped
at ~11MB decoded) on both `POST /api/chat` and `POST /api/chat/stream`.

- **`AIProvider.complete(..., images=...)`** (`app/providers.py`) —
  `GeminiProvider` sends them as `inline_data` parts alongside the text part
  (Gemini supports vision natively, no model/config change needed).
  `OllamaProvider` sends them via `/api/generate`'s top-level `images`
  array — this only works against a **vision-capable** `OLLAMA_MODEL`
  (e.g. `gemma3`, `llava`, `qwen2.5vl` — not every model, including
  reasoning-only ones like `gpt-oss`, can see images at all). A non-vision
  model either ignores the field or the request is rejected outright by
  Ollama's API — either way `FallbackProvider` degrades to mock exactly like
  any other provider failure, never a crash or a silently wrong answer.
- **Real AutoGen paths** (`app/streaming.py`'s direct-chat streaming,
  `AutoGenOrchestrator`'s MCP tool-calling path) send a
  `MultiModalMessage([text, autogen_core.Image, ...])` instead of a plain
  `TextMessage` (`build_user_message()`, `app/streaming.py`), and the
  resolved model client's `model_info["vision"]` is set `True` for that call
  — same reasoning as `function_calling` (see `build_streaming_model_client`):
  a model that's actually incapable of vision just fails the request, caught
  and degraded the same way as every other tool-calling/streaming failure.

Only the *current* turn's images are sent — historical image attachments
aren't replayed from session history on later turns (out of scope for v1;
context management, `docs/memory.md`, keeps the *text* of every turn, not
image bytes).
