#!/usr/bin/env bash
# Open the local web version in your default browser WITHOUT printing the one-time
# launch nonce (it is read from a 0600 file and handed straight to `open`).
set -euo pipefail
f="${TAL_WEB_DATA_DIR:-$HOME/Library/Application Support/Trading Agents Lab Web}/launch-url.txt"
[ -f "$f" ] || { echo "web server not started (run tools/run-web.sh)" >&2; exit 1; }
exec open "$(cat "$f")"
