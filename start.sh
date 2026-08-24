#!/usr/bin/env bash
# Full stack bring-up for Enterprise Copilot: venv + deps + .env, a reachability
# check of every configured external dependency (Gemini / Ollama / Qdrant /
# Tavily / Langfuse), then the app server itself.
#
# Every one of those dependencies is optional and remote (a cloud API, not a
# service this repo can start locally) — the app already degrades gracefully
# when one is unset or unreachable (see app/config.py). This script's checks
# are diagnostic only: a failed/skipped check is reported and the app still
# starts, so a misconfigured integration is visible up front instead of
# surfacing later as a confusing mid-chat failure.
#
# Usage:
#   ./start.sh                    # create venv if missing, install deps, run with --reload
#   HOST=0.0.0.0 PORT=8080 ./start.sh
#   SKIP_CHECKS=1 ./start.sh      # skip the dependency reachability checks
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

VENV_DIR=".venv"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
SKIP_CHECKS="${SKIP_CHECKS:-0}"

# ---------------------------------------------------------------------------
# 1. Python environment
# ---------------------------------------------------------------------------
if [ ! -f "$VENV_DIR/Scripts/python.exe" ]; then
    echo "Creating virtual environment in $VENV_DIR ..."
    python -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/Scripts/activate"

REQ_FILE="requirements-dev.txt"
[ -f "$REQ_FILE" ] || REQ_FILE="requirements.txt"

echo "Installing dependencies from $REQ_FILE ..."
pip install -q -r "$REQ_FILE"

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    echo "No .env found, copying .env.example -> .env"
    cp .env.example .env
fi

# ---------------------------------------------------------------------------
# 2. Dependency reachability checks (diagnostic only — never blocks startup)
# ---------------------------------------------------------------------------
if [ "$SKIP_CHECKS" != "1" ]; then
    echo ""
    echo "Checking configured external dependencies..."
    python - <<'PYEOF'
import sys
import urllib.request
import urllib.error

sys.path.insert(0, ".")
from app.config import settings

def check(name: str, configured: bool, url: str | None, headers: dict | None = None) -> None:
    if not configured:
        print(f"  [skip]  {name} — not configured (optional)")
        return
    if url is None:
        print(f"  [ok]    {name} — configured")
        return
    try:
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        urllib.request.urlopen(req, timeout=5)
        print(f"  [ok]    {name} — reachable ({url})")
    except urllib.error.HTTPError as exc:
        # Any HTTP response (even 401/404) means the host answered — that's
        # a reachability check, not an auth check.
        print(f"  [ok]    {name} — reachable ({url}, HTTP {exc.code})")
    except Exception as exc:  # noqa: BLE001 - diagnostic only, never fatal
        print(f"  [warn]  {name} — unreachable ({url}): {exc}")

check("Gemini API", bool(settings.gemini_api_key), None)
check(
    f"Ollama ({settings.ollama_base_url})",
    settings.model_provider == "ollama" or bool(settings.ollama_base_url),
    f"{settings.ollama_base_url.rstrip('/')}/api/tags",
)
check(
    "Qdrant Cloud",
    bool(settings.qdrant_url and settings.qdrant_api_key),
    settings.qdrant_url,
    {"api-key": settings.qdrant_api_key} if settings.qdrant_api_key else None,
)
check("Tavily web search", bool(settings.tavily_api_key), None)
check(
    "Langfuse observability",
    bool(settings.langfuse_public_key and settings.langfuse_secret_key),
    settings.langfuse_base_url,
)
print(f"  [info]  MCP stdio tools: {'enabled' if settings.mcp_stdio_enabled else 'disabled'}"
      f" ({settings.mcp_stdio_args})" if settings.mcp_stdio_enabled else "")
print(f"  [info]  MCP remote server: {'configured' if settings.mcp_remote_url else 'not configured'}")
PYEOF
    echo ""
fi

# ---------------------------------------------------------------------------
# 3. Application server
# ---------------------------------------------------------------------------
echo "Starting Enterprise Copilot on http://${HOST}:${PORT}"
exec uvicorn app.main:app --reload --host "$HOST" --port "$PORT"
