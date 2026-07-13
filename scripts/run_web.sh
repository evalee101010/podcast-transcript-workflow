#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "Python venv not found at $PYTHON_BIN. Run scripts/setup_funasr.sh first." >&2
  exit 1
fi

if [ -z "${PODCAST_TRACKER_CODEX_BIN:-}" ] && command -v codex >/dev/null 2>&1; then
  export PODCAST_TRACKER_CODEX_BIN="$(command -v codex)"
fi

if [ -z "${PODCAST_TRACKER_LARK_CLI_BIN:-}" ] && command -v lark-cli >/dev/null 2>&1; then
  export PODCAST_TRACKER_LARK_CLI_BIN="$(command -v lark-cli)"
fi

if [ -n "${PODCAST_TRACKER_MODEL_CACHE_DIR:-}" ]; then
  mkdir -p "$PODCAST_TRACKER_MODEL_CACHE_DIR/modelscope" \
    "$PODCAST_TRACKER_MODEL_CACHE_DIR/huggingface" \
    "$PODCAST_TRACKER_MODEL_CACHE_DIR/torch"
  export MODELSCOPE_CACHE="$PODCAST_TRACKER_MODEL_CACHE_DIR/modelscope"
  export MODELSCOPE_HOME="$PODCAST_TRACKER_MODEL_CACHE_DIR/modelscope"
  export HF_HOME="$PODCAST_TRACKER_MODEL_CACHE_DIR/huggingface"
  export HUGGINGFACE_HUB_CACHE="$PODCAST_TRACKER_MODEL_CACHE_DIR/huggingface/hub"
  export TORCH_HOME="$PODCAST_TRACKER_MODEL_CACHE_DIR/torch"
fi

export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

HOST="${PODCAST_TRACKER_HOST:-127.0.0.1}"
PORT="${PODCAST_TRACKER_PORT:-8765}"

exec "$PYTHON_BIN" -m podcast_tracker web --host "$HOST" --port "$PORT"
