#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="${PODCAST_TRACKER_LAUNCHD_LABEL:-io.github.podcast-transcript-workflow.web}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PRIVATE_DIR="${PODCAST_TRACKER_PRIVATE_DIR:-$HOME/.podcast_tracker_private}"
LOG_DIR="$PRIVATE_DIR/logs"
MODEL_CACHE_DIR="${PODCAST_TRACKER_MODEL_CACHE_DIR:-}"
CODEX_BIN="${PODCAST_TRACKER_CODEX_BIN:-}"
if [ -z "$CODEX_BIN" ] && command -v codex >/dev/null 2>&1; then
  CODEX_BIN="$(command -v codex)"
fi
LARK_CLI_BIN="${PODCAST_TRACKER_LARK_CLI_BIN:-}"
if [ -z "$LARK_CLI_BIN" ] && command -v lark-cli >/dev/null 2>&1; then
  LARK_CLI_BIN="$(command -v lark-cli)"
fi

mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$PROJECT_ROOT/scripts/run_web.sh</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$PROJECT_ROOT</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PODCAST_TRACKER_HOST</key>
    <string>127.0.0.1</string>
    <key>PODCAST_TRACKER_PORT</key>
    <string>8765</string>
    <key>PODCAST_TRACKER_PRIVATE_DIR</key>
    <string>$PRIVATE_DIR</string>
    <key>PODCAST_TRACKER_MODEL_CACHE_DIR</key>
    <string>$MODEL_CACHE_DIR</string>
    <key>PODCAST_TRACKER_CODEX_BIN</key>
    <string>$CODEX_BIN</string>
    <key>PODCAST_TRACKER_LARK_CLI_BIN</key>
    <string>$LARK_CLI_BIN</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>$LOG_DIR/web.out.log</string>

  <key>StandardErrorPath</key>
  <string>$LOG_DIR/web.err.log</string>
</dict>
</plist>
PLIST

chmod 644 "$PLIST"

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Installed launchd service: $LABEL"
echo "URL: http://127.0.0.1:8765/"
echo "Logs: $LOG_DIR/web.out.log and $LOG_DIR/web.err.log"
echo "Health check: $PROJECT_ROOT/scripts/healthcheck_web.sh"
