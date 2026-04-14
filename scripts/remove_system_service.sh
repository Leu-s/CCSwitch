#!/usr/bin/env bash
set -euo pipefail

PLIST_PATH="$HOME/Library/LaunchAgents/com.ccswitch.manager.plist"
LOG_DIR="$HOME/Library/Logs/ccswitch"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/ccswitch"
PURGE_LOGS=false

for arg in "$@"; do
    case "$arg" in
        --purge-logs) PURGE_LOGS=true ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# Unload service
if [[ -f "$PLIST_PATH" ]]; then
    echo "Stopping and unloading service..."
    launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || \
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
else
    echo "No LaunchAgent plist found — service not installed."
fi

# Kill any lingering process (graceful → forceful)
PIDS=$(pgrep -f "uvicorn backend.main:app" 2>/dev/null || true)
if [[ -n "$PIDS" ]]; then
    echo "Sending SIGTERM to server processes: $PIDS"
    kill -TERM $PIDS 2>/dev/null || true
    sleep 5
    REMAINING=$(pgrep -f "uvicorn backend.main:app" 2>/dev/null || true)
    if [[ -n "$REMAINING" ]]; then
        echo "Sending SIGKILL to remaining processes: $REMAINING"
        kill -KILL $REMAINING 2>/dev/null || true
    fi
fi

# Remove PID file
rm -f "$STATE_DIR/server.pid"

# Remove plist
if [[ -f "$PLIST_PATH" ]]; then
    rm -f "$PLIST_PATH"
    echo "Removed plist: $PLIST_PATH"
fi

if $PURGE_LOGS; then
    rm -rf "$LOG_DIR"
    rm -f "$STATE_DIR/server.log"
    echo "Purged logs."
fi

echo "Done."
