"""Tavily web search MCP tool server (stdio transport).

A single `web_search` tool backed by the Tavily Search API
(https://docs.tavily.com/documentation/api-reference/endpoint/search) — real
network access, unlike mcp_servers/general_tools_server.py's sandboxed
utility tools. Only ever loaded when TAVILY_API_KEY is configured (see
app/config.py) AND the turn's "Web Search" UI toggle is on (see
app/mcp_tools.py get_mcp_tools(web_search=...)); otherwise it never spawns.

The API key is read from this process's own environment (passed down by
app/mcp_tools.py's StdioServerParams `env=`, not the app's own .env parsing)
and is never echoed back in a tool result or error message.

The tool returns a JSON string (not free text): {"query", "results": [...]}
on success, {"error": "..."} on failure. The model reads JSON text fine, and
this lets AutoGenOrchestrator._run_with_tools (app/agents.py) parse the exact
same string out of ToolCallExecutionEvent to hand real, clickable sources to
the UI (OrchestrationResult.web_sources) — the same title/url/content the
model itself saw, not a re-derived guess.

Run standalone for a quick manual check (requires TAVILY_API_KEY in the
environment): `python mcp_servers/tavily_search_server.py` (reads/writes MCP
JSON-RPC frames over stdio — not meant to be run interactively, but it will
sit and wait for a client instead of erroring).
"""
from __future__ import annotations

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "enterprise-copilot-web-search",
    instructions="Real-time web search via Tavily. ALWAYS call this before "
                 "answering any question about current events, people "
                 "currently holding a position/office, prices, versions, "
                 "schedules, or any fact that can change over time — never "
                 "answer such questions from memory alone, even if you "
                 "believe you know the answer; your training data has a "
                 "cutoff and may be outdated. Also use it for anything the "
                 "knowledge base doesn't cover. Always cite the source URL "
                 "for anything you learned from a search result.",
)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_TIMEOUT_SECONDS = 20
_MAX_RESULTS = 5


@mcp.tool()
async def web_search(query: str) -> str:
    """Searches the live web via Tavily and returns the top results as a
    JSON string: {"query": "...", "results": [{"title", "url", "content"}, ...]}
    on success, or {"error": "..."} on failure (missing/invalid API key,
    timeout, Tavily error) — never raises; the calling agent sees the error
    and can decide how to proceed, same as any other tool result. ALWAYS
    call this for current-events/current-office-holder/price/schedule
    questions rather than answering from training data — cite the `url` of
    whichever result you actually used."""
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
    mcp.run(transport="stdio")
