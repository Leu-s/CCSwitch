#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$REPO_ROOT/.venv/bin/python"
PLIST_PATH="$HOME/Library/LaunchAgents/com.claudemulti.manager.plist"
LOG_DIR="$HOME/Library/Logs/claude-multi"
# Unified log — same location as manual launch.sh
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/claude-multi"
LOG_FILE="$STATE_DIR/server.log"

if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: Python venv not found at $PYTHON" >&2
    exit 1
fi

# Build PATH dynamically at install time — include user's current PATH
# Filter to include known tool locations not always on system PATH
INSTALL_PATH="$PATH"
for extra in /opt/homebrew/bin /opt/homebrew/sbin /usr/local/bin "$HOME/.local/bin"; do
    if [[ ":$INSTALL_PATH:" != *":$extra:"* ]]; then
        INSTALL_PATH="$extra:$INSTALL_PATH"
    fi
done

# Create directories
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$LOG_DIR"
mkdir -p "$STATE_DIR"

# Unload existing service if loaded (idempotent install)
# Use bootout (modern) with fallback to unload
if launchctl print "gui/$(id -u)/com.claudemulti.manager" &>/dev/null 2>&1; then
    echo "Unloading existing service..."
    launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || \
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
    sleep 1
fi

cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claudemulti.manager</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>backend.main:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8765</string>
        <string>--log-level</string>
        <string>warning</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$REPO_ROOT</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$INSTALL_PATH</string>
    </dict>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>$LOG_FILE</string>

    <key>StandardErrorPath</key>
    <string>$LOG_FILE</string>

    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF

chmod 644 "$PLIST_PATH"

# Load with modern API, fall back to legacy
echo "Loading service..."
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || \
    launchctl load "$PLIST_PATH"

echo "Service installed and started."
echo "Logs: $LOG_FILE"
echo "Status: launchctl print gui/$(id -u)/com.claudemulti.manager"
