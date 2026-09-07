"""Combined MCP tool server, Streamable HTTP transport — the deployable
counterpart to general_tools_server.py + tavily_search_server.py's stdio
servers (both still exist, unchanged, as the local-dev fallback; see
app/mcp_tools.py's _load_stdio_tools()). Deploy this as its own standalone
Render web service, then point Enterprise Copilot's MCP_REMOTE_URL at it —
the two are meant to run as separate deployments (this server has no
dependency on the app/ package, and the app has no dependency on this file)
so the app itself can be shared as a single link with no local MCP
subprocess to spawn on Render's side.

Tools: the same general-purpose set as general_tools_server.py
(current_datetime/calculator/word_count/generate_uuid — no filesystem/
network/subprocess access) plus tavily_search_server.py's `web_search`,
registered here only when TAVILY_API_KEY is present in THIS process's own
environment (the hosted server's Render env vars, not Enterprise Copilot's
own .env — see module docstring's secrets-boundary note). Enterprise
Copilot's own per-turn "Web Search" toggle (app/mcp_tools.py:
get_mcp_tools(web_search=...)) still decides whether a given chat turn's
tool list includes it; this only decides whether the tool exists to be
included at all.

Run locally for a quick manual check:
    MCP_HTTP_PORT=8765 python mcp_servers/hosted_tools_server.py
Then point a local Enterprise Copilot .env at it:
    MCP_REMOTE_URL=http://127.0.0.1:8765/mcp
    MCP_REMOTE_TRANSPORT=streamable-http

On Render: set the service's start command to
    python mcp_servers/hosted_tools_server.py
Render injects $PORT; this reads it the same way any Render web service
does (see MCP_HTTP_PORT below). No Dockerfile required — a plain Python
web service pointed at this repo with `pip install -r
mcp_servers/requirements.txt` as the build command works, since this file's
only import is `mcp` + optionally `httpx` (for web_search), not the whole
Enterprise Copilot app.
"""
from __future__ import annotations

import ast
import json
import operator
import os
import uuid
from datetime import datetime, timezone as _timezone

from mcp.server.fastmcp import FastMCP

# $PORT is Render's own convention (every Render web service is handed a
# port to bind via this env var — the platform's load balancer routes to
# whatever the service actually listens on, not a fixed port this code
# picks itself). MCP_HTTP_PORT is a same-shaped fallback for running this
# standalone outside Render (see module docstring's local-check command).
# 0.0.0.0, not 127.0.0.1 (FastMCP's own default) — Render's router connects
# from outside the container, which a loopback-only bind would refuse.
_PORT = int(os.environ.get("PORT") or os.environ.get("MCP_HTTP_PORT") or "8765")
_HOST = "0.0.0.0"

mcp = FastMCP(
    "enterprise-copilot-hosted-tools",
    instructions="General-purpose utility tools (current date/time, arithmetic, "
                 "text stats, UUIDs)" + (" plus real-time web search." if os.environ.get("TAVILY_API_KEY") else "."),
    host=_HOST,
    port=_PORT,
    # Stateless: Render can restart/redeploy this service's instance at any
    # time, and (on a paid plan) can run more than one instance behind its
    # load balancer — a stateful SSE session pinned to one specific process
    # would silently break on either. Every tool call here is already a
    # single self-contained request/response (no multi-step tool state), so
    # statelessness costs nothing real.
    stateless_http=True,
)


# --- General-purpose tools (mirrors general_tools_server.py exactly) ---------

@mcp.tool()
def current_datetime() -> str:
    """Current date and time in UTC, ISO 8601. UTC-only (no IANA timezone
    database bundled — see zoneinfo's platform note) so this works
    identically everywhere this server runs."""
    return datetime.now(_timezone.utc).isoformat()


# Safe arithmetic evaluator: a fixed, explicit whitelist of AST node types
# and operators — never Python's own eval()/exec(), which would let a
# calculator expression run arbitrary code. Deliberately numeric-only (no
# names, attributes, calls, subscripts, comprehensions).
_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"Unsupported expression: {ast.dump(node)}")


@mcp.tool()
def calculator(expression: str) -> str:
    """Evaluates a basic arithmetic expression (+ - * / // % ** and
    parentheses only — no variables, function calls, or other Python
    syntax). Returns the result as a string, or an error message if the
    expression can't be parsed/evaluated safely."""
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval(tree)
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError) as exc:
        return f"Error: {exc}"
    return str(result)


@mcp.tool()
def word_count(text: str) -> dict:
    """Basic text statistics: word/character/line counts."""
    return {
        "words": len(text.split()),
        "characters": len(text),
        "characters_no_spaces": len(text.replace(" ", "")),
        "lines": len(text.splitlines()) or (1 if text else 0),
    }


@mcp.tool()
def generate_uuid() -> str:
    """A fresh random UUID4 string."""
    return str(uuid.uuid4())


# --- Web search (mirrors tavily_search_server.py exactly, registered only
# when TAVILY_API_KEY is present in this process's own environment) ---------

if os.environ.get("TAVILY_API_KEY", "").strip():
    import httpx

    TAVILY_SEARCH_URL = "https://api.tavily.com/search"
    _TIMEOUT_SECONDS = 20
    _MAX_RESULTS = 5

    @mcp.tool()
    async def web_search(query: str) -> str:
        """Searches the live web via Tavily and returns the top results as a
        JSON string: {"query": "...", "results": [{"title", "url", "content"}, ...]}
        on success, or {"error": "..."} on failure (missing/invalid API key,
        timeout, Tavily error) — never raises; the calling agent sees the
        error and can decide how to proceed, same as any other tool result.
        ALWAYS call this for current-events/current-office-holder/price/
        schedule questions rather than answering from training data — cite
        the `url` of whichever result you actually used."""
        api_key = os.environ.get("TAVILY_API_KEY", "").strip()
        if not api_key:
            return json.dumps({"error": "web search is not configured (missing TAVILY_API_KEY)."})
        query = query.strip()
        if not query:
            return json.dumps({"error": "query must not be empty."})

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    TAVILY_SEARCH_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "query": query,
                        "search_depth": "basic",
                        "max_results": _MAX_RESULTS,
                        "include_answer": False,
                    },
                )
        except httpx.TimeoutException:
            return json.dumps({"error": "web search timed out."})
        except httpx.HTTPError as exc:
            return json.dumps({"error": f"web search request failed ({type(exc).__name__})."})

        if response.status_code == 401:
            return json.dumps({"error": "web search rejected the configured API key."})
        if response.status_code == 429:
            return json.dumps({"error": "web search rate limit exceeded — try again shortly."})
        if response.status_code != 200:
            # Never echo raw response bodies (may carry account-identifying
            # details) — just the status.
            return json.dumps({"error": f"web search failed (status {response.status_code})."})

        try:
            payload = response.json()
        except ValueError:
            return json.dumps({"error": "web search returned an unexpected response."})

        raw_results = payload.get("results") or []
        if not raw_results:
            return json.dumps({"query": query, "results": []})

        results = []
        for result in raw_results[:_MAX_RESULTS]:
            title = (result.get("title") or "Untitled").strip()
            url = (result.get("url") or "").strip()
            content = (result.get("content") or "").strip()
            if len(content) > 400:
                content = content[:400].rstrip() + "…"
            results.append({"title": title, "url": url, "content": content})
        return json.dumps({"query": query, "results": results})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
