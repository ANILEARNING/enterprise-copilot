# Hosted MCP tool server (`hosted_tools_server.py`)

The Streamable-HTTP counterpart to this app's local stdio MCP servers
(`general_tools_server.py`, `tavily_search_server.py`) — deploy this as its
**own separate Render web service**, then point the main Enterprise Copilot
app's `MCP_REMOTE_URL` at it. This lets the whole app be shared as a single
link with no local subprocess for Render to spawn on the app's side.

## Why a separate service

- No dependency on the `app/` package — `pip install -r
  mcp_servers/requirements.txt` alone is enough to run it, much lighter than
  the full app's stack (autogen, sqlalchemy, boto3, ...).
- Independently deployable/restartable/scalable from the main app.
- The Tavily API key lives in *this* service's own environment, not the main
  app's — a real secrets-boundary choice (see `hosted_tools_server.py`'s
  module docstring): if this server is ever shared/reused by something else,
  the main app's other secrets (DATABASE_URL, JWT_SECRET_KEY, ...) were never
  anywhere near it.

## Deploy to Render

1. **New Web Service** on Render, pointed at this repo.
2. **Root Directory**: `mcp_servers`
3. **Build Command**: `pip install -r requirements.txt`
4. **Start Command**: `python hosted_tools_server.py`
5. **Environment variables**:
   - `TAVILY_API_KEY` — optional; omit entirely to run general-purpose tools
     only (calculator, word_count, generate_uuid, current_datetime), no web
     search. Set it to also offer `web_search`.
   - `PORT` — Render sets this automatically; don't set it yourself.
6. Deploy. Render gives you a URL like `https://your-service.onrender.com`.

## Point Enterprise Copilot at it

In the main app's own environment (`.env` locally, or Render's env vars for
that service once it's deployed too):

```
MCP_REMOTE_URL=https://your-service.onrender.com/mcp
MCP_REMOTE_TRANSPORT=streamable-http
```

That's it — `app/mcp_tools.py`'s existing `_load_remote_tools()` path picks
this up with no code change; it's the same mechanism this app already
supported for any remote MCP server, just now pointed at one you deployed
yourself.

## Local dev / manual check

```bash
# from the repo root
MCP_HTTP_PORT=8765 python mcp_servers/hosted_tools_server.py
# or, with web search:
TAVILY_API_KEY=tvly-... MCP_HTTP_PORT=8765 python mcp_servers/hosted_tools_server.py
```

Then point a local `.env` at `http://127.0.0.1:8765/mcp` the same way as
above. The bundled stdio servers (`general_tools_server.py`,
`tavily_search_server.py`, driven by `MCP_STDIO_ENABLED`/`MCP_STDIO_COMMAND`)
still work unchanged and remain the zero-config default for local
development — you don't need this server running just to use the app
locally; it exists for the deployed/shared-link case.
