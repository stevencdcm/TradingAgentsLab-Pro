#!/usr/bin/env bash
# Local web version (127.0.0.1 only). Builds the renderer if needed, then starts
# the engine + web bridge and prints a one-time launch URL.
#   tools/run-web.sh [--port 8765]
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PY="${TAL_PYTHON:-engine/.venv/bin/python}"
[ -x "$PY" ] || { echo "Python venv not found at $PY (set TAL_PYTHON)" >&2; exit 1; }
[ -f desktop/dist/index.html ] || (cd desktop && npm run build)
exec "$PY" -m engine.web "$@"
