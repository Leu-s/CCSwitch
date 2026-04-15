#!/usr/bin/env bash
set -euo pipefail

# Color support only for TTY
if [[ -t 1 ]]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; NC='\033[0m'
else
    RED=''; YELLOW=''; GREEN=''; NC=''
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/ccswitch"
PID_FILE="$STATE_DIR/server.pid"
# Unified log path: both launch.sh and plist write here
LOG_FILE="$STATE_DIR/server.log"
LAUNCHAGENT_PLIST="$HOME/Library/LaunchAgents/com.ccswitch.manager.plist"
_HOST="${CCSWITCH_SERVER_HOST:-127.0.0.1}"
_PORT="${CCSWITCH_SERVER_PORT:-41924}"
API="http://${_HOST}:${_PORT}"

echo "=== CCSwitch Status ==="
echo ""

# 1. Server process
SERVER_RUNNING=false
if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo -e "${GREEN}✓ Server running${NC} (PID $PID)"
        SERVER_RUNNING=true
    else
        echo -e "${RED}✗ Server not running${NC} (stale PID file)"
    fi
else
    # Check if running via LaunchAgent (may not have our PID file)
    if pgrep -f "uvicorn backend.main:app" &>/dev/null; then
        echo -e "${GREEN}✓ Server running${NC} (via LaunchAgent)"
        SERVER_RUNNING=true
    else
        echo -e "${RED}✗ Server not running${NC}"
    fi
fi

# 2. HTTP health
if $SERVER_RUNNING; then
    if curl -sf "$API/health" &>/dev/null; then
        echo -e "${GREEN}✓ HTTP health check OK${NC}"
    else
        echo -e "${RED}✗ HTTP health check FAILED${NC}"
    fi
fi

# 3. LaunchAgent status
if [[ -f "$LAUNCHAGENT_PLIST" ]]; then
    if launchctl print "gui/$(id -u)/com.ccswitch.manager" &>/dev/null 2>&1; then
        echo -e "${GREEN}✓ LaunchAgent loaded${NC} (auto-start enabled)"
    else
        echo -e "${YELLOW}⚠ LaunchAgent plist exists but not loaded${NC}"
    fi
else
    echo -e "${YELLOW}  LaunchAgent not installed${NC} (run: ccswitch service install)"
fi

# 4. Active account (read from ~/.claude.json oauthAccount — HOME root,
#    not ~/.claude/.claude.json; that's where Claude Code CLI reads it from)
CLAUDE_JSON="$HOME/.claude.json"
if [[ -f "$CLAUDE_JSON" ]]; then
    EMAIL=$(CLAUDE_JSON="$CLAUDE_JSON" python3 -c "import json,os; d=json.load(open(os.environ['CLAUDE_JSON'])); print(d.get('oauthAccount',{}).get('emailAddress','unknown'))" 2>/dev/null || echo "unknown")
    if [[ "$EMAIL" != "unknown" && -n "$EMAIL" ]]; then
        echo -e "${GREEN}✓ Active account: $EMAIL${NC}"
    else
        echo -e "${YELLOW}⚠ No active account (oauthAccount missing from .claude.json)${NC}"
    fi
else
    echo -e "${YELLOW}⚠ No active account (~/.claude/.claude.json missing)${NC}"
fi

# 5. Log tail if server down
if ! $SERVER_RUNNING; then
    echo ""
    echo "Last 10 log lines:"
    if [[ -f "$LOG_FILE" ]]; then
        tail -10 "$LOG_FILE" | sed 's/^/  /'
    else
        echo "  (no log file found at $LOG_FILE)"
    fi
fi

echo ""
echo "UI: http://${_HOST}:${_PORT}"
