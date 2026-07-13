#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

exec "$PYTHON_BIN" -m podcast_tracker check "$@"
