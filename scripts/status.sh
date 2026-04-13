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
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/claude-multi"
PID_FILE="$STATE_DIR/server.pid"
# Unified log path: both launch.sh and plist write here
LOG_FILE="$STATE_DIR/server.log"
LAUNCHAGENT_PLIST="$HOME/Library/LaunchAgents/com.claudemulti.manager.plist"
API="http://localhost:8765"

echo "=== Claude Multi-Account Manager Status ==="
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
    if launchctl print "gui/$(id -u)/com.claudemulti.manager" &>/dev/null 2>&1; then
        echo -e "${GREEN}✓ LaunchAgent loaded${NC} (auto-start enabled)"
    else
        echo -e "${YELLOW}⚠ LaunchAgent plist exists but not loaded${NC}"
    fi
else
    echo -e "${YELLOW}  LaunchAgent not installed${NC} (run: cc-acc service install)"
fi

# 4. Active account
ACTIVE_FILE="$HOME/.claude-multi/active"
if [[ -f "$ACTIVE_FILE" ]]; then
    ACTIVE_DIR=$(cat "$ACTIVE_FILE" | tr -d '[:space:]')
    if [[ -n "$ACTIVE_DIR" && -d "$ACTIVE_DIR" ]]; then
        # Parse email from .claude.json
        CLAUDE_JSON="$ACTIVE_DIR/.claude.json"
        if [[ -f "$CLAUDE_JSON" ]]; then
            EMAIL=$(CLAUDE_JSON="$CLAUDE_JSON" python3 -c "import json,os; d=json.load(open(os.environ['CLAUDE_JSON'])); print(d.get('oauthAccount',{}).get('emailAddress','unknown'))" 2>/dev/null || echo "unknown")
            echo -e "${GREEN}✓ Active account: $EMAIL${NC}"
        else
            echo -e "${YELLOW}⚠ Active dir set but .claude.json missing${NC}"
        fi
    else
        echo -e "${RED}✗ Active config dir not found${NC}"
    fi
else
    echo -e "${YELLOW}⚠ No active account set${NC}"
fi

# 5. Shell configured
SHELL_OK=false
for rc in ".zshrc" ".bashrc" ".zprofile" ".bash_profile"; do
    rc_path="$HOME/$rc"
    if [[ -f "$rc_path" ]] && grep -q "claude-multi/active" "$rc_path" 2>/dev/null; then
        echo -e "${GREEN}✓ Shell configured${NC} ($rc_path)"
        SHELL_OK=true
        break
    fi
done
if ! $SHELL_OK; then
    echo -e "${YELLOW}⚠ Shell not configured${NC} — run: cc-acc shell setup"
fi

# 6. Log tail if server down
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
echo "UI: http://localhost:8765"
