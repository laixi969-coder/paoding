#!/usr/bin/env bash
set -euo pipefail
if [ "$(uname -s)" = "Darwin" ]; then
  TASK_PLIST="$HOME/Library/LaunchAgents/local.paoding.pm2.plist"
  if [ -f "$TASK_PLIST" ]; then
    launchctl bootout "gui/$(id -u)" "$TASK_PLIST" || true
    rm "$TASK_PLIST"
  fi
else
  pm2 unstartup
fi
