#!/usr/bin/env bash
set -euo pipefail

LABEL="${PODCAST_TRACKER_LAUNCHD_LABEL:-io.github.podcast-transcript-workflow.web}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"

echo "Uninstalled launchd service: $LABEL"
