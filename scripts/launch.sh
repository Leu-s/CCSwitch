#!/usr/bin/env bash
set -euo pipefail

# Resolve repo root from script location (CRITICAL: never rely on caller CWD)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="$REPO_ROOT/.venv/bin/python"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/claude-multi"
PID_FILE="$STATE_DIR/server.pid"
LOG_FILE="$STATE_DIR/server.log"

# Verify prerequisites
if ! command -v tmux &>/dev/null; then
    echo "ERROR: tmux not found on PATH. Install with: brew install tmux" >&2
    exit 1
fi
if ! command -v security &>/dev/null; then
    echo "ERROR: security command not found — this requires macOS." >&2
    exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: Python venv not found at $PYTHON. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

mkdir -p "$STATE_DIR"

# Stale PID detection with process identity check
if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        # Verify it's actually our server (PID reuse defense)
        PROC_CMD=$(ps -p "$OLD_PID" -o comm= 2>/dev/null || true)
        if echo "$PROC_CMD" | grep -qE "python|uvicorn"; then
            echo "Server is already running (PID $OLD_PID). Use 'cc-acc server stop' to stop it." >&2
            exit 1
        fi
    fi
    rm -f "$PID_FILE"
fi

# Write PID before exec; cleanup trap for pre-exec failures
cleanup() { rm -f "$PID_FILE"; }
trap cleanup EXIT

echo $$ > "$PID_FILE"

echo "Starting Claude Multi-Account Manager on http://127.0.0.1:8765"
echo "Logs: $LOG_FILE"
echo "Press Ctrl+C to stop."

# Disarm trap — exec replaces process; PID file must survive while server runs
trap - EXIT

exec "$PYTHON" -m uvicorn backend.main:app \
    --host 127.0.0.1 \
    --port 8765 \
    --log-level warning
