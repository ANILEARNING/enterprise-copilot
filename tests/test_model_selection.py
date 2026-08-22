import httpx
import pytest

import app.providers as providers_module
from app.config import settings
from app.providers import (
    GeminiProvider, MockProvider, ModelUnavailableError, OllamaProvider,
    build_provider_for, describe_model_error, list_available_embedding_models,
    list_available_models,
)
from app.services import CopilotService


@pytest.fixture(autouse=True)
def _reset_ollama_embedding_cache(monkeypatch):
    # _discover_ollama_embedding_models (app/providers.py) caches process-
    # wide once scanned — every test gets a fresh "not scanned yet" cache.
    monkeypatch.setattr(providers_module, "_ollama_embedding_models_cache", None)
    yield


# --- describe_model_error: human-readable reasons -----------------------------

def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_describe_model_error_rate_limit():
    assert "Rate limited" in describe_model_error(_http_error(429))


def test_describe_model_error_auth():
    assert "Not authorized" in describe_model_error(_http_error(401))
    assert "Not authorized" in describe_model_error(_http_error(403))


def test_describe_model_error_not_found():
    assert "not found" in describe_model_error(_http_error(404)).lower()


def test_describe_model_error_server_unavailable():
    assert "unavailable" in describe_model_error(_http_error(503)).lower()


def test_describe_model_error_timeout():
    assert "timed out" in describe_model_error(httpx.ReadTimeout("x")).lower()


def test_describe_model_error_connect_error():
    assert "connect" in describe_model_error(httpx.ConnectError("x")).lower()


def test_describe_model_error_runtime_error_uses_message():
    assert describe_model_error(RuntimeError("no content returned")) == "no content returned"


def test_describe_model_error_empty_exception_never_blank():
    # a bare Exception() with no args has an empty str() -- must still say something
    msg = describe_model_error(Exception())
    assert msg and "Exception" in msg


# --- build_provider_for: explicit (provider, model) construction --------------

def test_build_provider_for_mock():
    provider = build_provider_for("mock", "mock")
    assert isinstance(provider, MockProvider)


def test_build_provider_for_gemini_requires_api_key(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    # Match on GEMINI_API_KEY (the actionable, stable part of the message)
    # rather than the surrounding prose, which has been reworded before.
    with pytest.raises(ModelUnavailableError, match="GEMINI_API_KEY"):
        build_provider_for("gemini", "gemini-flash-latest")


def test_build_provider_for_gemini_with_key(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "fake-key")
    provider = build_provider_for("gemini", "gemini-2.0-flash")
    assert isinstance(provider, GeminiProvider)
    assert provider.model == "gemini-2.0-flash"


def test_build_provider_for_ollama():
    provider = build_provider_for("ollama", "gpt-oss:20b")
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "gpt-oss:20b"


def test_build_provider_for_unknown_provider():
    with pytest.raises(ModelUnavailableError, match="Unknown provider"):
        build_provider_for("not-a-real-provider", "x")


# --- list_available_models: partial-failure-tolerant catalog ------------------

@pytest.mark.asyncio
async def test_list_available_models_always_includes_mock(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "ollama_base_url", "")
    result = await list_available_models()
    assert {"provider": "mock", "model": "mock", "label": "Mock (offline, deterministic)"} in result["models"]


@pytest.mark.asyncio
async def test_list_available_models_reports_reason_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "ollama_base_url", "")
    result = await list_available_models()
    error_providers = {e["provider"] for e in result["errors"]}
    assert "gemini" in error_providers
    assert "ollama" in error_providers
    assert all(e["message"] for e in result["errors"])  # never a blank reason


@pytest.mark.asyncio
async def test_list_available_models_gemini_success(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "fake-key")
    monkeypatch.setattr(settings, "ollama_base_url", "")

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"models": [
                {"name": "models/gemini-flash-latest", "displayName": "Gemini Flash",
                 "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/embedding-001", "displayName": "Embedding",
                 "supportedGenerationMethods": ["embedContent"]},  # filtered out: no generateContent
            ]}

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None): return FakeResponse()

    monkeypatch.setattr("app.providers.httpx.AsyncClient", FakeAsyncClient)
    result = await list_available_models()
    gemini_models = [m for m in result["models"] if m["provider"] == "gemini"]
    assert gemini_models == [{"provider": "gemini", "model": "gemini-flash-latest", "label": "Gemini Flash"}]


# --- list_available_embedding_models: same shape, one level down --------------

@pytest.mark.asyncio
async def test_list_available_embedding_models_reports_reason_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "ollama_base_url", "")
    result = await list_available_embedding_models()
    error_providers = {e["provider"] for e in result["errors"]}
    assert "gemini" in error_providers
    assert "ollama" in error_providers
    assert result["models"] == []


@pytest.mark.asyncio
async def test_list_available_embedding_models_gemini_filters_on_embed_content(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "fake-key")
    monkeypatch.setattr(settings, "ollama_base_url", "")

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"models": [
                {"name": "models/gemini-flash-latest", "displayName": "Gemini Flash",
                 "supportedGenerationMethods": ["generateContent"]},  # filtered out: no embedContent
                {"name": "models/gemini-embedding-001", "displayName": "Gemini Embedding 001",
                 "supportedGenerationMethods": ["embedContent"]},
            ]}

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None): return FakeResponse()

    monkeypatch.setattr("app.providers.httpx.AsyncClient", FakeAsyncClient)
    result = await list_available_embedding_models()
    gemini_models = [m for m in result["models"] if m["provider"] == "gemini"]
    assert gemini_models == [{"provider": "gemini", "model": "gemini-embedding-001", "label": "Gemini Embedding 001"}]


@pytest.mark.asyncio
async def test_list_available_embedding_models_ollama_scans_capabilities(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "ollama_base_url", "http://fake-ollama:11434")

    class FakeResponse:
        status_code = 200
        def __init__(self, payload): self._payload = payload
        def raise_for_status(self): pass
        def json(self): return self._payload

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            return FakeResponse({"models": [{"name": "nomic-embed-text"}, {"name": "gemma4:31b"}]})
        async def post(self, url, headers=None, json=None, timeout=None):
            name = json["model"]
            caps = ["embedding"] if name == "nomic-embed-text" else ["completion", "tools"]
            return FakeResponse({"capabilities": caps})

    monkeypatch.setattr("app.providers.httpx.AsyncClient", FakeAsyncClient)
    result = await list_available_embedding_models()
    ollama_models = [m["model"] for m in result["models"] if m["provider"] == "ollama"]
    assert ollama_models == ["nomic-embed-text"]  # gemma4:31b correctly excluded — no embedding capability
    assert not any(e["provider"] == "ollama" for e in result["errors"])


@pytest.mark.asyncio
async def test_list_available_embedding_models_ollama_reports_when_none_found(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "ollama_base_url", "http://fake-ollama:11434")

    class FakeResponse:
        status_code = 200
        def __init__(self, payload): self._payload = payload
        def raise_for_status(self): pass
        def json(self): return self._payload

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            return FakeResponse({"models": [{"name": "gemma4:31b"}]})
        async def post(self, url, headers=None, json=None, timeout=None):
            return FakeResponse({"capabilities": ["completion", "tools"]})  # no embedding models at all

    monkeypatch.setattr("app.providers.httpx.AsyncClient", FakeAsyncClient)
    result = await list_available_embedding_models()
    assert not any(m["provider"] == "ollama" for m in result["models"])
    ollama_error = next(e["message"] for e in result["errors"] if e["provider"] == "ollama")
    assert "gemma4:31b" not in ollama_error  # doesn't leak the model name, just says none qualified
    assert "1 pulled model" in ollama_error


@pytest.mark.asyncio
async def test_ollama_embedding_scan_is_cached_across_calls(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "ollama_base_url", "http://fake-ollama:11434")
    call_count = {"show": 0}

    class FakeResponse:
        status_code = 200
        def __init__(self, payload): self._payload = payload
        def raise_for_status(self): pass
        def json(self): return self._payload

    class FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            return FakeResponse({"models": [{"name": "nomic-embed-text"}]})
        async def post(self, url, headers=None, json=None, timeout=None):
            call_count["show"] += 1
            return FakeResponse({"capabilities": ["embedding"]})

    monkeypatch.setattr("app.providers.httpx.AsyncClient", FakeAsyncClient)
    await list_available_embedding_models()
    await list_available_embedding_models()
    assert call_count["show"] == 1  # second call reused the cache, no re-scan


# --- CopilotService._parse_model_choice ---------------------------------------

def test_parse_model_choice_auto_and_none():
    assert CopilotService._parse_model_choice(None) == (None, None)
    assert CopilotService._parse_model_choice("auto") == (None, None)


def test_parse_model_choice_provider_and_model():
    assert CopilotService._parse_model_choice("gemini/gemini-2.0-flash") == ("gemini", "gemini-2.0-flash")
    assert CopilotService._parse_model_choice("ollama/gpt-oss:20b") == ("ollama", "gpt-oss:20b")
