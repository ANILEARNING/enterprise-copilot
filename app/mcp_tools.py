"""MCP (Model Context Protocol) tool loading — optional. AutoGenOrchestrator
(app/agents.py) gets these tools available for real tool-calling on
agent-mode turns, in addition to its built-in knowledge-base/code-queue
steps. A local stdio server and a remote (Streamable HTTP/SSE) server can
be configured independently (see app/config.py); either/both missing just
means no extra tools, never a startup/request error — the same
graceful-degrade posture as every AIProvider in app/providers.py.

This is the only file that imports autogen_ext.tools.mcp — mirrors the
AutoGen boundary discipline in .claude/rules/autogen-maf.md (framework-
specific calls stay in one designated place), one level removed:
AutoGenOrchestrator only ever sees the plain BaseTool objects get_mcp_tools()
returns, never an MCP-specific type.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
TAVILY_SERVER_SCRIPT = "mcp_servers/tavily_search_server.py"

_lock = asyncio.Lock()
_cache: list | None = None  # None = not loaded yet; [] = loaded, nothing available
# Per-server outcome of the most recent load attempt — what the Settings tab
# shows (see describe_mcp_config()). None until get_mcp_tools() has run once.
_last_status: dict = {"stdio": None, "remote": None, "web_search": None}

# The web-search tool is loaded separately from _cache: unlike the two
# servers above (always offered once loaded), it's only ever included in a
# given turn's tools when that turn's "Web Search" UI toggle is on (see
# get_mcp_tools(web_search=...)) — a process-wide cache keyed only on
# "loaded or not" still works because the *server process* itself doesn't
# depend on the toggle, only which turns get handed its tool.
_web_search_lock = asyncio.Lock()
_web_search_cache: list | None = None


def _parse_headers(raw: str) -> dict[str, str] | None:
    """One "Key: Value" pair per line -> dict (e.g. an Authorization bearer
    token for the remote server). None if `raw` has no parseable pairs."""
    headers: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key:
            headers[key] = value
    return headers or None


async def _load_stdio_tools() -> list:
    if not settings.mcp_stdio_enabled or not settings.mcp_stdio_command:
        _last_status["stdio"] = {"configured": False}
        return []
    from autogen_ext.tools.mcp import StdioServerParams, mcp_server_tools

    params = StdioServerParams(
        command=settings.mcp_stdio_command,
        args=settings.mcp_stdio_args.split() if settings.mcp_stdio_args else [],
        # Relative args (the bundled default) must resolve against the repo
        # root regardless of whatever directory the app process itself was
        # launched from.
        cwd=str(REPO_ROOT),
        read_timeout_seconds=15,
    )
    try:
        tools = await mcp_server_tools(params)
    except Exception as exc:  # noqa: BLE001 - one server's failure must not break the other / the app
        logger.warning("MCP stdio server failed to load tools: %s", exc)
        _last_status["stdio"] = {"configured": True, "ok": False, "error": str(exc)}
        return []
    _last_status["stdio"] = {"configured": True, "ok": True, "tool_count": len(tools),
                              "tools": [t.name for t in tools]}
    return tools


async def _load_remote_tools() -> list:
    if not settings.mcp_remote_url:
        _last_status["remote"] = {"configured": False}
        return []
    from autogen_ext.tools.mcp import SseServerParams, StreamableHttpServerParams, mcp_server_tools

    headers = _parse_headers(settings.mcp_remote_headers)
    params = (
        SseServerParams(url=settings.mcp_remote_url, headers=headers)
        if settings.mcp_remote_transport == "sse"
        else StreamableHttpServerParams(url=settings.mcp_remote_url, headers=headers)
    )
    try:
        tools = await mcp_server_tools(params)
    except Exception as exc:  # noqa: BLE001 - one server's failure must not break the other / the app
        logger.warning("MCP remote server failed to load tools: %s", exc)
        _last_status["remote"] = {"configured": True, "ok": False, "error": str(exc)}
        return []
    _last_status["remote"] = {"configured": True, "ok": True, "tool_count": len(tools),
                               "tools": [t.name for t in tools]}
    return tools


async def _load_web_search_tools() -> list:
    if not settings.tavily_api_key:
        _last_status["web_search"] = {"configured": False}
        return []
    from autogen_ext.tools.mcp import StdioServerParams, mcp_server_tools

    params = StdioServerParams(
        command=settings.mcp_stdio_command,  # same interpreter as the bundled server (sys.executable)
        args=[TAVILY_SERVER_SCRIPT],
        cwd=str(REPO_ROOT),
        # The key reaches the child process's own environment only — never
        # logged, never part of the prompt/tool schema the model sees.
        env={"TAVILY_API_KEY": settings.tavily_api_key},
        read_timeout_seconds=15,
    )
    try:
        tools = await mcp_server_tools(params)
    except Exception as exc:  # noqa: BLE001 - one server's failure must not break the other / the app
        logger.warning("Tavily MCP server failed to load tools: %s", exc)
        _last_status["web_search"] = {"configured": True, "ok": False, "error": str(exc)}
        return []
    _last_status["web_search"] = {"configured": True, "ok": True, "tool_count": len(tools),
                                   "tools": [t.name for t in tools]}
    return tools


async def get_web_search_tools() -> list:
    """The Tavily `web_search` tool, loaded once and cached — [] if
    TAVILY_API_KEY isn't set or the server failed to load (see
    _last_status["web_search"]). Separate from get_mcp_tools()'s cache since
    inclusion is also gated per-turn by the UI toggle (see that function's
    `web_search` param), not just by configuration."""
    global _web_search_cache
    if _web_search_cache is not None:
        return _web_search_cache
    async with _web_search_lock:
        if _web_search_cache is not None:
            return _web_search_cache
        _web_search_cache = await _load_web_search_tools()
        return _web_search_cache


async def get_mcp_tools(web_search: bool = False) -> list:
    """Every available MCP tool for this turn: the always-on stdio + remote
    servers, plus the Tavily web-search tool when `web_search` is True (the
    turn's "Web Search" UI toggle) AND TAVILY_API_KEY is configured. The
    always-on pair is loaded once and cached for the life of the process —
    loading spawns a subprocess (stdio) / makes a network round-trip
    (remote), too slow to repeat on every chat turn. Never raises: a server
    that fails to load just contributes no tools (see _last_status, surfaced
    by describe_mcp_config() for the Settings tab, for why)."""
    global _cache
    if _cache is None:
        async with _lock:
            if _cache is None:  # someone else won the race while this waited on the lock
                stdio_tools, remote_tools = await asyncio.gather(_load_stdio_tools(), _load_remote_tools())
                _cache = [*stdio_tools, *remote_tools]
    tools = list(_cache)
    if web_search:
        tools.extend(await get_web_search_tools())
    return tools


def describe_mcp_config() -> dict:
    """Status for the UI (Settings tab): static configuration plus whatever
    the most recent load attempt found. `last_loaded` is None until
    get_mcp_tools() has actually run once (the first agent-mode chat turn
    in this process) — this function itself never loads anything, so
    polling it (e.g. on every Settings tab open) is always cheap."""
    any_loaded = _cache is not None or _web_search_cache is not None
    return {
        "stdio": {
            "enabled": settings.mcp_stdio_enabled, "command": settings.mcp_stdio_command,
            "args": settings.mcp_stdio_args,
        },
        "remote": {
            "configured": bool(settings.mcp_remote_url), "url": settings.mcp_remote_url,
            "transport": settings.mcp_remote_transport,
        },
        "web_search": {
            "configured": bool(settings.tavily_api_key),
        },
        "last_loaded": dict(_last_status) if any_loaded else None,
        "tool_count": (len(_cache) if _cache is not None else 0)
                      + (len(_web_search_cache) if _web_search_cache is not None else 0)
                      if any_loaded else None,
    }
