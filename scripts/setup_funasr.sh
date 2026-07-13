#!/usr/bin/env bash
# One-shot setup for the FunASR local backend (default).
# Usage: bash scripts/setup_funasr.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. Install it first (macOS: brew install ffmpeg)." >&2
  exit 1
fi

PY="${PYTHON:-python3}"

if [ ! -d ".venv" ]; then
  echo "Creating venv at .venv ..."
  "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements-funasr.txt

echo
echo "FunASR backend ready."
echo "Smoke test (no transcription, just env check):"
echo "  python -m podcast_tracker transcribe-auto <episode_id> --dry-run"
echo
echo "First real run downloads ~1-2 GB of models, then runs fully offline:"
echo "  python -m podcast_tracker transcribe-auto <episode_id>"
