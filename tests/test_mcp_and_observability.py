"""app/mcp_tools.py and app/observability.py — both optional, graceful-
degrade-to-nothing subsystems (see their module docstrings). These tests
cover the config-driven describe_*()/enabled surfaces without depending on
this environment's real .env credentials or actually spawning the bundled
MCP server (that's covered live in docs/tools.md's manual smoke check —
these tests only need to prove the graceful-degrade contract itself)."""
import pytest

import app.mcp_tools as mcp_tools
from app.config import settings
from app.observability import Tracer


@pytest.fixture(autouse=True)
def _reset_mcp_cache(monkeypatch):
    # Every test gets its own fresh "not loaded yet" cache — the module-level
    # cache is deliberately process-wide in production (see get_mcp_tools's
    # docstring), which would leak state between tests otherwise.
    monkeypatch.setattr(mcp_tools, "_cache", None)
    monkeypatch.setattr(mcp_tools, "_web_search_cache", None)
    monkeypatch.setattr(mcp_tools, "_last_status", {"stdio": None, "remote": None, "web_search": None})
    monkeypatch.setattr(settings, "tavily_api_key", "")
    yield


def test_describe_mcp_config_before_any_load_is_static_only():
    desc = mcp_tools.describe_mcp_config()
    assert desc["last_loaded"] is None
    assert desc["tool_count"] is None
    assert desc["stdio"]["enabled"] == settings.mcp_stdio_enabled
    assert desc["remote"]["configured"] == bool(settings.mcp_remote_url)
    assert desc["web_search"]["configured"] == bool(settings.tavily_api_key)


@pytest.mark.asyncio
async def test_get_mcp_tools_returns_empty_when_both_servers_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "mcp_stdio_enabled", False)
    monkeypatch.setattr(settings, "mcp_remote_url", "")
    tools = await mcp_tools.get_mcp_tools()
    assert tools == []
    desc = mcp_tools.describe_mcp_config()
    assert desc["last_loaded"] == {"stdio": {"configured": False}, "remote": {"configured": False}, "web_search": None}
    assert desc["tool_count"] == 0


# --- web search (Tavily) — separate cache, gated per-turn by the `web_search` param ---

@pytest.mark.asyncio
async def test_get_mcp_tools_skips_web_search_when_not_requested(monkeypatch):
    monkeypatch.setattr(settings, "mcp_stdio_enabled", False)
    monkeypatch.setattr(settings, "mcp_remote_url", "")
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-fake-key")
    tools = await mcp_tools.get_mcp_tools(web_search=False)
    assert tools == []
    # Not even attempted — no load outcome recorded.
    assert mcp_tools._last_status["web_search"] is None


@pytest.mark.asyncio
async def test_get_web_search_tools_returns_empty_when_unconfigured():
    tools = await mcp_tools.get_web_search_tools()
    assert tools == []
    assert mcp_tools._last_status["web_search"] == {"configured": False}


@pytest.mark.asyncio
async def test_get_mcp_tools_web_search_degrades_when_server_fails_to_load(monkeypatch):
    monkeypatch.setattr(settings, "mcp_stdio_enabled", False)
    monkeypatch.setattr(settings, "mcp_remote_url", "")
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-fake-key")
    monkeypatch.setattr(settings, "mcp_stdio_command", "this-binary-does-not-exist-anywhere")
    tools = await mcp_tools.get_mcp_tools(web_search=True)
    assert tools == []
    assert mcp_tools._last_status["web_search"]["configured"] is True
    assert mcp_tools._last_status["web_search"]["ok"] is False


@pytest.mark.asyncio
async def test_get_mcp_tools_degrades_when_stdio_server_fails_to_load(monkeypatch):
    # A broken/unreachable stdio command must not raise — it just contributes
    # no tools, same graceful-degrade posture as every AIProvider.
    monkeypatch.setattr(settings, "mcp_stdio_enabled", True)
    monkeypatch.setattr(settings, "mcp_stdio_command", "this-binary-does-not-exist-anywhere")
    monkeypatch.setattr(settings, "mcp_stdio_args", "")
    monkeypatch.setattr(settings, "mcp_remote_url", "")
    tools = await mcp_tools.get_mcp_tools()
    assert tools == []
    desc = mcp_tools.describe_mcp_config()
    assert desc["last_loaded"]["stdio"]["configured"] is True
    assert desc["last_loaded"]["stdio"]["ok"] is False


def test_parse_headers_parses_key_value_lines():
    # A line with no ":" (no key) is skipped, not turned into a bogus header.
    headers = mcp_tools._parse_headers("Authorization: Bearer abc123\nX-Custom: value\nnot-a-header-line")
    assert headers == {"Authorization": "Bearer abc123", "X-Custom": "value"}


def test_parse_headers_empty_input_returns_none():
    assert mcp_tools._parse_headers("") is None
    assert mcp_tools._parse_headers("   \n  ") is None


# --- observability: Tracer's no-op contract when unconfigured ---------------

def test_tracer_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    monkeypatch.setattr(settings, "langfuse_secret_key", "")
    tracer = Tracer()
    assert tracer.enabled is False


@pytest.mark.asyncio
async def test_tracer_turn_is_a_safe_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    monkeypatch.setattr(settings, "langfuse_secret_key", "")
    tracer = Tracer()
    async with tracer.turn("chat_turn", input="hello", metadata={"agent_mode": True}) as turn:
        # None of these may raise, and none may require a real Langfuse client.
        turn.event("thinking", label="Thinking…")
        turn.generation("model_call", model="m", provider="p", input="hi", output="hello")
        turn.set_output("hello", metadata={"provider": "p"})
