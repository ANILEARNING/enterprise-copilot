#!/usr/bin/env bash
# Start the Enterprise Copilot app (FastAPI + Uvicorn) using git bash on Windows.
#
# Usage:
#   ./run.sh                # create venv if missing, install deps, run with --reload
#   HOST=0.0.0.0 PORT=8080 ./run.sh
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

VENV_DIR=".venv"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

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

echo "Starting Enterprise Copilot on http://${HOST}:${PORT}"
exec uvicorn app.main:app --reload --host "$HOST" --port "$PORT"
