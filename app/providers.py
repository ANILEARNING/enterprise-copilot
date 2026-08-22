"""AI provider abstraction.

The rest of the app (routes, orchestrator, services) only ever talks to an
`AIProvider`. Swapping Gemini for Ollama, or adding a new provider, never
touches anything outside this file.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

from .config import settings
from .retrieval import embed as hash_embed


@dataclass
class ProviderResult:
    text: str
    provider: str
    # The actual model name behind `provider` (e.g. "gemini-flash-latest",
    # "gpt-oss:20b") — None for the mock provider, which isn't a model.
    # Mirrors EmbeddingResult.model below; surfaced to the UI/observability
    # so "what model actually answered" is answerable, not just "which
    # provider".
    model: str | None = None
    used_fallback: bool = False
    error: str | None = None


class AIProvider:
    """Provider-agnostic single-turn completion interface."""

    name = "base"

    async def complete(
        self, prompt: str, history: list[dict], max_tokens: int | None = None, json_mode: bool = False,
        images: list[dict] | None = None,
    ) -> ProviderResult:
        """`max_tokens`, when given, overrides the provider's configured
        default for this one call — for tasks (skill drafting, code
        generation) whose expected output routinely exceeds the default
        budget. None means "use the provider's own default".

        `json_mode`, when True, asks the provider to constrain its output to
        valid JSON (used by skill spec-drafting, see app/skills.py). Not
        every provider supports this — implementations that don't are free
        to ignore it rather than error.

        `images`, when given, is a list of {"data": <base64>, "mime_type":
        "image/..."} attachments for multimodal ("what's in this picture?")
        turns — see app/models.py:ImageAttachment. Both configured providers
        support vision natively (Gemini always; Ollama needs a vision-
        capable OLLAMA_MODEL, e.g. gemma3/llava/qwen2.5vl — see
        docs/multimodal.md). Providers that can't use them are free to
        ignore this rather than error, same as json_mode above."""
        raise NotImplementedError


class MockProvider(AIProvider):
    """Always-available deterministic provider. No credentials required."""

    name = "mock"

    async def complete(
        self, prompt: str, history: list[dict], max_tokens: int | None = None, json_mode: bool = False,
        images: list[dict] | None = None,
    ) -> ProviderResult:
        suffix = f" (+{len(images)} image(s), not actually seen — mock mode)" if images else ""
        return ProviderResult(text=f"Demo response: {prompt}{suffix}", provider=self.name)


# Verified live against gemma-4-26b-a4b-it: a trivial ~12-token classification
# prompt still consumed ~172 hidden-reasoning tokens before the real answer
# appeared (thinkingConfig can't disable this for Gemma — see complete()
# below). 512 leaves real headroom above that floor for longer/harder
# prompts; a caller's own max_tokens is still respected if it's already
# bigger than this.
_GEMMA_THINKING_TOKEN_FLOOR = 512


class GeminiProvider(AIProvider):
    name = "gemini"
    # "-latest" alias tracks Google's current recommended flash model, avoiding
    # hardcoding a dated model name that gets retired. Overridable per-instance
    # (see build_provider_for) for an explicit user model choice.
    DEFAULT_MODEL = "gemini-flash-latest"

    def __init__(self, api_key: str, max_output_tokens: int = 256, model: str = DEFAULT_MODEL):
        self.api_key = api_key
        self.max_output_tokens = max_output_tokens
        self.model = model or self.DEFAULT_MODEL

    async def complete(
        self, prompt: str, history: list[dict], max_tokens: int | None = None, json_mode: bool = False,
        images: list[dict] | None = None,
    ) -> ProviderResult:
        contents = [
            {"role": "model" if h["role"] == "assistant" else "user", "parts": [{"text": h["content"]}]}
            for h in history
        ]
        # Vision: inline_data parts alongside the text part on the final
        # turn, exactly like a real multimodal request — Gemini supports
        # this natively, no model/config change needed.
        parts: list[dict] = [{"text": prompt}]
        for image in images or []:
            parts.append({"inline_data": {"mime_type": image.get("mime_type", "image/png"), "data": image["data"]}})
        contents.append({"role": "user", "parts": parts})
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        requested_tokens = max_tokens or self.max_output_tokens
        generation_config: dict = {"maxOutputTokens": requested_tokens}
        # Gemma models (model id starts with "gemma-") reject thinkingConfig
        # outright — verified live: {"thinkingConfig": {"thinkingBudget": 0}}
        # against gemma-4-26b-a4b-it returns 400 INVALID_ARGUMENT "Thinking
        # budget is not supported for this model." Real Gemini models (Flash/
        # Pro) DO need this, or a "thinking" model can spend the whole token
        # budget on hidden reasoning and return no visible text at all
        # (finishReason: MAX_TOKENS, empty content — verified live too).
        # Gemma still thinks regardless (its response's `parts` include
        # {"thought": true} entries ahead of the real answer — see the loop
        # below) — it just can't be told not to, so it needs a generous
        # budget instead of a disable flag; see _GEMMA_THINKING_TOKEN_FLOOR.
        is_gemma = self.model.startswith("gemma-")
        if is_gemma:
            generation_config["maxOutputTokens"] = max(requested_tokens, _GEMMA_THINKING_TOKEN_FLOOR)
        else:
            generation_config["thinkingConfig"] = {"thinkingBudget": 0}
        if json_mode:
            # Constrains sampling to well-formed JSON via Gemini's own
            # structured-output mode — same guarantee OllamaProvider gets via
            # format: "json" above, instead of hoping a "respond with only
            # JSON" prompt instruction is followed. Used by skill spec-
            # drafting and agent routing (AgentRegistry.select_llm).
            generation_config["responseMimeType"] = "application/json"
        async with httpx.AsyncClient(timeout=15) as client:
            # Key travels in a header, never the URL, so it can't leak into logs/exceptions.
            resp = await client.post(
                endpoint,
                headers={"x-goog-api-key": self.api_key},
                json={"contents": contents, "generationConfig": generation_config},
            )
            resp.raise_for_status()
            data = resp.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts")
            if not parts:
                finish_reason = data.get("candidates", [{}])[0].get("finishReason", "unknown")
                raise RuntimeError(f"Gemini returned no content (finishReason={finish_reason})")
            # Skip {"thought": true} parts (Gemma's hidden reasoning, emitted
            # inline ahead of the real answer since thinkingConfig can't
            # disable it — see above) and concatenate whatever's left. Plain
            # Gemini responses have no thought parts at all, so this is a
            # no-op for them — just parts[0]["text"] as before.
            answer_parts = [p["text"] for p in parts if not p.get("thought") and "text" in p]
            if not answer_parts:
                finish_reason = data.get("candidates", [{}])[0].get("finishReason", "unknown")
                raise RuntimeError(
                    f"Gemini returned only hidden-reasoning content, no visible answer "
                    f"(finishReason={finish_reason}) — try a larger max_tokens."
                )
            return ProviderResult(text="".join(answer_parts), provider=self.name, model=self.model)


class OllamaProvider(AIProvider):
    """Works against both a local Ollama daemon (no auth) and Ollama Cloud
    (Bearer auth), since both speak the same /api/generate shape."""

    name = "ollama"

    def __init__(self, base_url: str, model: str, api_key: str = "", max_output_tokens: int = 256):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_output_tokens = max_output_tokens

    async def complete(
        self, prompt: str, history: list[dict], max_tokens: int | None = None, json_mode: bool = False,
        images: list[dict] | None = None,
    ) -> ProviderResult:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "model": self.model, "prompt": prompt, "stream": False,
            # Mirrors GeminiProvider's thinkingBudget: 0 — asks reasoning models
            # (e.g. gpt-oss) to skip hidden reasoning. Not every model honors this
            # fully (observed: still emits a populated "thinking" field), so the
            # token budget below still has to be generous enough to survive it.
            "think": False,
            "options": {"num_predict": max_tokens or self.max_output_tokens},
        }
        if images:
            # Ollama's /api/generate vision support: a top-level "images"
            # array of raw base64 strings (no data: URI prefix, no
            # mime_type — the model infers format from the bytes). Only
            # meaningful against a vision-capable OLLAMA_MODEL (gemma3,
            # llava, qwen2.5vl, ...) — a text-only model just ignores it or
            # replies confused; see docs/multimodal.md.
            payload["images"] = [image["data"] for image in images]
        if json_mode:
            # Constrains sampling to well-formed JSON (Ollama's structured-output
            # mode) instead of hoping the model follows the "respond with only
            # JSON" instruction in the prompt — used by skill spec-drafting,
            # which otherwise occasionally returns prose/markdown around the
            # JSON, or malformed JSON, even with a generous token budget.
            payload["format"] = "json"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/api/generate",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("response", "")
            if not text:
                # A too-small token budget can be entirely consumed by hidden
                # "thinking" tokens on reasoning models, leaving no visible text.
                raise RuntimeError(f"Ollama returned no content (done_reason={data.get('done_reason')})")
            return ProviderResult(text=text, provider=self.name, model=self.model)


class FallbackProvider(AIProvider):
    """Wraps a configured provider; any failure or missing configuration
    degrades gracefully to the mock provider instead of breaking the app."""

    name = "fallback"

    def __init__(self, primary: AIProvider | None, mock: MockProvider):
        self.primary = primary
        self.mock = mock

    async def complete(
        self, prompt: str, history: list[dict], max_tokens: int | None = None, json_mode: bool = False,
        images: list[dict] | None = None,
    ) -> ProviderResult:
        if self.primary is None:
            result = await self.mock.complete(prompt, history, max_tokens, json_mode, images)
            result.used_fallback = True
            result.error = "not_configured"
            return result
        try:
            return await self.primary.complete(prompt, history, max_tokens, json_mode, images)
        except Exception as exc:  # noqa: BLE001 - any provider failure must degrade, not crash
            # Full detail server-side only, for operators; the client only ever gets the class name.
            logger.warning("Provider %s failed, falling back to mock: %s", self.primary.name, exc)
            result = await self.mock.complete(prompt, history, max_tokens, json_mode, images)
            result.used_fallback = True
            result.error = type(exc).__name__
            return result


def build_provider() -> AIProvider:
    """Factory: mock mode returns the mock provider directly; configured mode
    returns a fallback-wrapped provider so missing/broken credentials never
    take the app down."""
    mock = MockProvider()
    if settings.ai_mode != "configured":
        return mock

    primary: AIProvider | None = None
    if settings.model_provider == "gemini" and settings.gemini_api_key:
        primary = GeminiProvider(
            settings.gemini_api_key, settings.max_output_tokens,
            model=settings.gemini_model or GeminiProvider.DEFAULT_MODEL,
        )
    elif settings.model_provider == "ollama" and settings.ollama_model:
        primary = OllamaProvider(
            settings.ollama_base_url, settings.ollama_model, settings.ollama_api_key,
            settings.max_output_tokens,
        )

    return FallbackProvider(primary, mock)


def build_vision_provider() -> AIProvider | None:
    """Image-only PDF OCR (app/extraction.py) always tries Gemini directly,
    independent of MODEL_PROVIDER/AI_MODE — same posture as
    settings.agent_router_model always calling Gemini regardless of the main
    chat provider (app/agents.py:_build_router_provider): OCR is a small,
    self-contained capability need, not "the configured chat model," so a
    user running MODEL_PROVIDER=ollama with a Gemini key still gets OCR.
    None (no GEMINI_API_KEY) means "no vision provider available" — callers
    treat that as a clear, honest rejection, never a silent mock/no-op."""
    if not settings.gemini_api_key:
        return None
    return GeminiProvider(settings.gemini_api_key, settings.max_output_tokens, model=GeminiProvider.DEFAULT_MODEL)


# --- explicit model selection (Copilot's model picker) -----------------------
#
# build_provider()/FallbackProvider above back the *default* chat path, which
# always degrades to mock on failure — right for an automatic, no-user-input
# flow. When the user explicitly picks a model from the Copilot UI, silently
# substituting mock would hide exactly the information they're trying to see
# (does this model work? why not?). build_provider_for() + describe_model_error()
# exist for that path: no fallback wrapping, and a human-readable reason for
# whatever went wrong.

class ModelUnavailableError(ValueError):
    """An explicit provider/model selection can't be used at all (not
    configured, unknown provider) — distinct from a request-time failure
    against a model that IS configured (see describe_model_error)."""


def build_provider_for(provider: str, model: str) -> AIProvider:
    """Builds a raw provider for an explicit (provider, model) choice — not
    wrapped in FallbackProvider. Raises ModelUnavailableError if that
    provider isn't configured in this environment at all."""
    if provider == "mock":
        return MockProvider()
    if provider == "gemini":
        if not settings.gemini_api_key:
            raise ModelUnavailableError("Gemini isn't configured in this environment (no GEMINI_API_KEY).")
        return GeminiProvider(settings.gemini_api_key, settings.max_output_tokens, model=model)
    if provider == "ollama":
        return OllamaProvider(settings.ollama_base_url, model, settings.ollama_api_key, settings.max_output_tokens)
    raise ModelUnavailableError(f"Unknown provider {provider!r}.")


def describe_model_error(exc: Exception) -> str:
    """Turns a raw provider exception into a short, human-readable reason —
    shown directly to the user when their explicitly chosen model fails,
    instead of a bare exception class name or a silent mock substitution."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return "Rate limited (429) — you've hit this model's usage quota. Try again shortly, or pick a different model."
        if status in (401, 403):
            return f"Not authorized ({status}) — check the API key configured for this provider."
        if status == 404:
            return "Model not found (404) — it may not exist, or isn't available on this account."
        if status in (500, 502, 503, 504):
            return f"This model's servers are unavailable right now ({status}). Try again shortly, or pick a different model."
        return f"Request failed ({status})."
    if isinstance(exc, httpx.TimeoutException):
        return "The request timed out. Try again, or pick a different model."
    if isinstance(exc, httpx.ConnectError):
        return "Could not connect to this model's server."
    if isinstance(exc, RuntimeError) and str(exc):
        return str(exc)
    return f"{type(exc).__name__}: {exc}" if str(exc) else f"{type(exc).__name__} (no further detail available)."


async def list_available_models() -> dict:
    """Queries each configured provider for its real, current model catalog
    (never a hardcoded guess — see docs/rag.md's caveat about the risk of
    stale hardcoded model names). Partial failures don't fail the whole
    call: each provider reports its own models or its own reason for having
    none, so the UI can show "Gemini: 12 models" alongside "Ollama: unavailable
    (connection failed)" rather than one failure blanking the whole list."""
    models: list[dict] = [{"provider": "mock", "model": "mock", "label": "Mock (offline, deterministic)"}]
    errors: list[dict] = []

    if settings.gemini_api_key:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": settings.gemini_api_key},
                )
                resp.raise_for_status()
                for m in resp.json().get("models", []):
                    if "generateContent" in (m.get("supportedGenerationMethods") or []):
                        model_id = m["name"].removeprefix("models/")
                        models.append({"provider": "gemini", "model": model_id,
                                       "label": m.get("displayName") or model_id})
        except Exception as exc:  # noqa: BLE001 - report, don't fail the whole listing
            errors.append({"provider": "gemini", "message": describe_model_error(exc)})
    else:
        errors.append({"provider": "gemini", "message": "Not configured (no GEMINI_API_KEY)."})

    if settings.ollama_base_url:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                headers = {"Authorization": f"Bearer {settings.ollama_api_key}"} if settings.ollama_api_key else {}
                resp = await client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", headers=headers)
                resp.raise_for_status()
                for m in resp.json().get("models", []):
                    models.append({"provider": "ollama", "model": m["name"], "label": m["name"]})
        except Exception as exc:  # noqa: BLE001 - report, don't fail the whole listing
            errors.append({"provider": "ollama", "message": describe_model_error(exc)})
    else:
        errors.append({"provider": "ollama", "message": "Not configured (no OLLAMA_BASE_URL)."})

    return {"models": models, "errors": errors}


# Ollama has no equivalent of Gemini's supportedGenerationMethods on its plain
# model listing (/api/tags) — the real per-model capability list only comes
# from a separate /api/show call. Concurrent scanning keeps this fast (~1.5s
# for ~20 models in testing) but it's still real per-request network work
# multiplied by however many models are pulled, so results are cached for
# the life of the process rather than rescanned on every Settings-page load.
# Nothing in this app currently changes OLLAMA_BASE_URL or pulls new Ollama
# models at runtime, so there's no cache-bust hook yet — if a future change
# adds one (e.g. an editable OLLAMA_BASE_URL), reset
# _ollama_embedding_models_cache to None there.
_ollama_embedding_models_cache: list[str] | None = None


async def _discover_ollama_embedding_models(client: httpx.AsyncClient, headers: dict, model_names: list[str]) -> list[str]:
    global _ollama_embedding_models_cache
    if _ollama_embedding_models_cache is not None:
        return _ollama_embedding_models_cache

    async def _capabilities(name: str) -> tuple[str, list[str]]:
        try:
            resp = await client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/show", headers=headers,
                json={"model": name}, timeout=8,
            )
            if resp.status_code == 200:
                return name, resp.json().get("capabilities") or []
        except Exception:  # noqa: BLE001 - one model's /api/show failing must not blank the whole scan
            pass
        return name, []

    results = await asyncio.gather(*[_capabilities(n) for n in model_names])
    _ollama_embedding_models_cache = [name for name, caps in results if "embedding" in caps]
    return _ollama_embedding_models_cache


async def list_available_embedding_models() -> dict:
    """Real, live embedding-capable model catalog per configured provider —
    for the Settings page's RAG embedding dropdowns (see
    docs/runtime-settings.md). Same shape/degrade posture as
    list_available_models() above, one level down: Gemini's own model
    listing already flags embedContent support for free; Ollama needs a
    concurrent /api/show scan per model (see _discover_ollama_embedding_models)
    since /api/tags carries no capability info at all."""
    models: list[dict] = []
    errors: list[dict] = []

    if settings.gemini_api_key:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": settings.gemini_api_key},
                )
                resp.raise_for_status()
                for m in resp.json().get("models", []):
                    if "embedContent" in (m.get("supportedGenerationMethods") or []):
                        model_id = m["name"].removeprefix("models/")
                        models.append({"provider": "gemini", "model": model_id,
                                       "label": m.get("displayName") or model_id})
        except Exception as exc:  # noqa: BLE001 - report, don't fail the whole listing
            errors.append({"provider": "gemini", "message": describe_model_error(exc)})
    else:
        errors.append({"provider": "gemini", "message": "Not configured (no GEMINI_API_KEY)."})

    if settings.ollama_base_url:
        try:
            headers = {"Authorization": f"Bearer {settings.ollama_api_key}"} if settings.ollama_api_key else {}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", headers=headers)
                resp.raise_for_status()
                all_names = [m["name"] for m in resp.json().get("models", [])]
                embedding_names = await _discover_ollama_embedding_models(client, headers, all_names)
                for name in embedding_names:
                    models.append({"provider": "ollama", "model": name, "label": name})
                if all_names and not embedding_names:
                    errors.append({
                        "provider": "ollama",
                        "message": f"None of the {len(all_names)} pulled model(s) report embedding capability "
                                   "(e.g. `ollama pull nomic-embed-text`).",
                    })
        except Exception as exc:  # noqa: BLE001 - report, don't fail the whole listing
            errors.append({"provider": "ollama", "message": describe_model_error(exc)})
    else:
        errors.append({"provider": "ollama", "message": "Not configured (no OLLAMA_BASE_URL)."})

    return {"models": models, "errors": errors}


# --- embedding provider abstraction ------------------------------------------
#
# Same shape as AIProvider above, one level down: RAGStore only ever talks to
# an EmbeddingProvider. Swapping the offline hashing embedder for a real
# model, or adding a new one, never touches RAGStore or anything above it.

@dataclass
class EmbeddingResult:
    vector: list[float]
    provider: str
    # The actual model name behind `provider` (e.g. "nomic-embed-text",
    # "gemini-embedding-001") — None for the hash embedder, which isn't a
    # model. Surfaced to the UI so "what embedding are we actually using"
    # is answerable instead of just "which provider".
    model: str | None = None
    used_fallback: bool = False
    error: str | None = None


class EmbeddingProvider:
    """Provider-agnostic single-text embedding interface."""

    name = "base"

    async def embed(self, text: str) -> EmbeddingResult:
        raise NotImplementedError


class HashEmbeddingProvider(EmbeddingProvider):
    """Always-available deterministic embedder (see app/retrieval.py). No
    credentials, no network call — this is also the fallback target when a
    real embedding provider is unavailable or fails."""

    name = "hash"

    async def embed(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(vector=hash_embed(text), provider=self.name)


class GeminiEmbeddingProvider(EmbeddingProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self._endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"

    async def embed(self, text: str) -> EmbeddingResult:
        async with httpx.AsyncClient(timeout=15) as client:
            # Key travels in a header, never the URL, so it can't leak into logs/exceptions.
            resp = await client.post(
                self._endpoint,
                headers={"x-goog-api-key": self.api_key},
                json={"content": {"parts": [{"text": text}]}},
            )
            resp.raise_for_status()
            values = resp.json().get("embedding", {}).get("values")
            if not values:
                raise RuntimeError("Gemini returned no embedding values")
            return EmbeddingResult(vector=values, provider=self.name, model=self.model)


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Works against both a local Ollama daemon (no auth) and Ollama Cloud
    (Bearer auth), via the /api/embed endpoint.

    Not every Ollama host/account has the configured embedding model
    available (a Cloud plan may not serve it at all, or a local daemon may
    have a different one pulled) — that surfaces as an error on /api/embed,
    not a helpful "model not found". So on failure this tries a short list
    of other well-known Ollama embedding models against the same host before
    giving up, and remembers whichever one actually worked so later calls
    (one per chunk) go straight to it instead of re-probing every time.

    If every candidate fails, that's also remembered (negative cache) for
    the life of this instance: retrying all N models on every single chunk
    of every document — this host clearly has no embedding model at all —
    would make ingestion visibly slow for no benefit, so subsequent calls
    fail fast and let ChainEmbeddingProvider move on to the next step
    immediately instead of re-paying that cost.
    """

    name = "ollama"
    _FALLBACK_MODELS = ("nomic-embed-text", "mxbai-embed-large", "all-minilm", "bge-m3", "snowflake-arctic-embed")

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._working_model: str | None = None
        self._no_model_available = False

    async def _embed_with_model(self, client: httpx.AsyncClient, headers: dict, model: str, text: str) -> list[float]:
        resp = await client.post(
            f"{self.base_url}/api/embed",
            headers=headers,
            json={"model": model, "input": text},
        )
        resp.raise_for_status()
        embeddings = resp.json().get("embeddings")
        if not embeddings or not embeddings[0]:
            raise RuntimeError(f"Ollama returned no embedding values for model {model!r}")
        return embeddings[0]

    async def embed(self, text: str) -> EmbeddingResult:
        if self._no_model_available:
            raise RuntimeError(f"No working embedding model found on {self.base_url} (cached from an earlier attempt)")

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        candidates = [self._working_model] if self._working_model else []
        candidates += [m for m in (self.model, *self._FALLBACK_MODELS) if m and m not in candidates]

        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=30) as client:
            for model in candidates:
                try:
                    vector = await self._embed_with_model(client, headers, model, text)
                except Exception as exc:  # noqa: BLE001 - try the next candidate model
                    last_error = exc
                    continue
                if model != self._working_model:
                    logger.info("Ollama embedding model %r resolved to %r", self.model, model)
                self._working_model = model
                return EmbeddingResult(vector=vector, provider=self.name, model=model)
        self._no_model_available = True
        raise last_error or RuntimeError("Ollama embedding failed: no candidate models available")


class ChainEmbeddingProvider(EmbeddingProvider):
    """Tries each embedding provider in `steps`, in order; the first success
    wins. Falls back to the offline hash embedder only if every step fails
    (or none are configured), so ingestion/retrieval is never broken by a
    missing model or a provider outage."""

    name = "chain"

    def __init__(self, steps: list[EmbeddingProvider], hash_provider: HashEmbeddingProvider):
        self.steps = steps
        self.hash_provider = hash_provider

    async def embed(self, text: str) -> EmbeddingResult:
        last_error: Exception | None = None
        for step in self.steps:
            try:
                result = await step.embed(text)
            except Exception as exc:  # noqa: BLE001 - any step failure must try the next, not crash
                logger.warning("Embedding provider %s failed, trying next: %s", step.name, exc)
                last_error = exc
                continue
            result.used_fallback = step is not self.steps[0]
            return result
        result = await self.hash_provider.embed(text)
        result.used_fallback = True
        result.error = type(last_error).__name__ if last_error else "not_configured"
        return result


def build_embedding_provider() -> EmbeddingProvider:
    """Factory: mock mode returns the hash embedder directly. Configured mode
    builds a chain following MODEL_PROVIDER: Ollama first (itself retrying a
    few other embedding models on that host if the configured one fails —
    see OllamaEmbeddingProvider), then Gemini as a cross-provider safety net
    if it's also configured, then the offline hash embedder as the final,
    always-available step."""
    hash_provider = HashEmbeddingProvider()
    if settings.ai_mode != "configured":
        return hash_provider

    steps: list[EmbeddingProvider] = []
    if settings.model_provider == "ollama" and settings.ollama_embedding_model:
        steps.append(OllamaEmbeddingProvider(
            settings.ollama_base_url, settings.ollama_embedding_model, settings.ollama_api_key,
        ))
        if settings.gemini_api_key:
            steps.append(GeminiEmbeddingProvider(settings.gemini_api_key, settings.gemini_embedding_model))
    elif settings.model_provider == "gemini" and settings.gemini_api_key:
        steps.append(GeminiEmbeddingProvider(settings.gemini_api_key, settings.gemini_embedding_model))

    return ChainEmbeddingProvider(steps, hash_provider)


def describe_embedding_config() -> dict:
    """Static description of the *configured* embedding chain (from settings,
    no network call) — "what embedding model are we set up to use", for the
    RAG status UI. Distinct from a chunk's recorded `embedding_provider`/
    `embedding_model` (app/services.py:RAGStore), which is "what actually
    embedded this specific chunk" and can differ if a provider fell back at
    ingestion time."""
    if settings.ai_mode != "configured":
        return {"provider": "hash", "model": None, "fallback_chain": ["hash"]}

    chain: list[dict] = []
    if settings.model_provider == "ollama" and settings.ollama_embedding_model:
        chain.append({"provider": "ollama", "model": settings.ollama_embedding_model})
        if settings.gemini_api_key:
            chain.append({"provider": "gemini", "model": settings.gemini_embedding_model})
    elif settings.model_provider == "gemini" and settings.gemini_api_key:
        chain.append({"provider": "gemini", "model": settings.gemini_embedding_model})
    chain.append({"provider": "hash", "model": None})

    primary = chain[0]
    return {
        "provider": primary["provider"], "model": primary["model"],
        "fallback_chain": [f"{c['provider']}" + (f"/{c['model']}" if c["model"] else "") for c in chain],
    }
