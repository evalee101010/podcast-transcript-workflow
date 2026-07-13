#!/usr/bin/env bash
set -euo pipefail

HOST="${PODCAST_TRACKER_HOST:-127.0.0.1}"
PORT="${PODCAST_TRACKER_PORT:-8765}"

python3 - "$HOST" "$PORT" <<'PY'
import json
import sys
import urllib.request

host = sys.argv[1]
port = sys.argv[2]
url = f"http://{host}:{port}/api/health"
try:
    with urllib.request.urlopen(url, timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8"))
except Exception as exc:
    print(f"unhealthy: {exc}", file=sys.stderr)
    raise SystemExit(1)

if not payload.get("ok"):
    print(f"unhealthy payload: {payload}", file=sys.stderr)
    raise SystemExit(1)

counts = payload.get("counts", {})
print(
    "healthy: "
    f"subscriptions={counts.get('subscriptions', 0)} "
    f"episodes={counts.get('episodes', 0)}"
)
PY
