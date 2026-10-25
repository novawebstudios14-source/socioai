#!/usr/bin/env bash
set -euo pipefail
node /provider/server/build/main.js --host 127.0.0.1 > /dev/null 2>&1 &
provider_pid=$!
xvfb-run -a --server-args="-screen 0 1280x720x24 -nolisten tcp" python server.py &
api_pid=$!
trap 'kill "$provider_pid" "$api_pid" 2>/dev/null || true' EXIT INT TERM
wait -n "$provider_pid" "$api_pid"
