#!/usr/bin/env bash
# Open the local web version in your default browser WITHOUT printing the single-use
# launch nonce (read from a 0600 file and handed straight to `open`). If the nonce was
# already used, ask the running server (SIGUSR1) to issue a fresh one.
set -euo pipefail
d="${TAL_WEB_DATA_DIR:-$HOME/Library/Application Support/Trading Agents Lab Web}"
f="$d/launch-url.txt"
if [ ! -f "$f" ]; then
  [ -f "$d/web.pid" ] || { echo "web server not started (run tools/run-web.sh)" >&2; exit 1; }
  kill -USR1 "$(cat "$d/web.pid")" 2>/dev/null || { echo "web server not running" >&2; exit 1; }
  for _ in 1 2 3 4 5 6 7 8 9 10; do [ -f "$f" ] && break; sleep 0.3; done
fi
[ -f "$f" ] || { echo "no launch URL available" >&2; exit 1; }
exec open "$(cat "$f")"
