#!/usr/bin/env python3
"""ccswitch — CLI for the CCSwitch dashboard."""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: .venv/bin/pip install httpx", file=sys.stderr)
    sys.exit(1)

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
_DEFAULT_HOST = os.environ.get("CCSWITCH_SERVER_HOST", "127.0.0.1")
_DEFAULT_PORT = os.environ.get("CCSWITCH_SERVER_PORT", "41924")
BASE_URL = os.environ.get("CCSWITCH_URL", f"http://{_DEFAULT_HOST}:{_DEFAULT_PORT}").rstrip("/")
TIMEOUT = 5.0
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "ccswitch"


def api(method: str, path: str, **kwargs):
    """Make an API call; exit with a clear message if server is unreachable."""
    url = f"{BASE_URL}{path}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = getattr(client, method)(url, **kwargs)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        print(f"ERROR: Cannot connect to server at {BASE_URL}.", file=sys.stderr)
        print("Start it with: ccswitch server start  OR  ccswitch service install", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"ERROR: API returned {e.response.status_code}: {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except httpx.TimeoutException:
        print(f"ERROR: Request to {url} timed out after {TIMEOUT}s.", file=sys.stderr)
        sys.exit(1)


def find_account_by_email(email: str) -> dict:
    accounts = api("get", "/api/accounts")
    matches = [a for a in accounts if a["email"] == email]
    if not matches:
        emails = [a["email"] for a in accounts]
        print(f"ERROR: No account found with email '{email}'.", file=sys.stderr)
        print(f"Known accounts: {', '.join(emails) or '(none)'}", file=sys.stderr)
        sys.exit(1)
    return matches[0]


def cmd_list(args):
    accounts = api("get", "/api/accounts")
    if not accounts:
        print("No accounts registered.")
        return
    print(f"{'#':<3} {'Email':<40} {'Status':<10} {'Enabled'}")
    print("-" * 65)
    for i, a in enumerate(accounts, 1):
        active = " ← active" if a.get("is_active") else ""
        enabled = "yes" if a.get("enabled", True) else "no"
        print(f"{i:<3} {a['email']:<40} {enabled:<10}{active}")


def cmd_switch(args):
    acc = find_account_by_email(args.email)
    print(f"Switching to {args.email}...")
    api("post", f"/api/accounts/{acc['id']}/switch")
    print("Done.")


def cmd_enable(args):
    acc = find_account_by_email(args.email)
    api("patch", f"/api/accounts/{acc['id']}", json={"enabled": True})
    print(f"Enabled {args.email}.")


def cmd_disable(args):
    acc = find_account_by_email(args.email)
    api("patch", f"/api/accounts/{acc['id']}", json={"enabled": False})
    print(f"Disabled {args.email}.")


def cmd_status(args):
    service = api("get", "/api/service")
    accounts = api("get", "/api/accounts")
    active_email = service.get("active_email")
    print(f"Auto-switch:        {'ON' if service.get('enabled') else 'OFF'}")
    print(f"Active account:     {active_email or '(none)'}")
    print(f"Total accounts:     {len(accounts)}")


def cmd_service_install(args):
    script = str(SCRIPTS_DIR / "create_system_service.sh")
    if not os.path.exists(script):
        print(f"ERROR: Script not found: {script}", file=sys.stderr)
        sys.exit(1)
    os.execv("/bin/bash", ["/bin/bash", script])


def cmd_service_remove(args):
    script = str(SCRIPTS_DIR / "remove_system_service.sh")
    if not os.path.exists(script):
        print(f"ERROR: Script not found: {script}", file=sys.stderr)
        sys.exit(1)
    cmd = ["/bin/bash", script]
    if args.purge_logs:
        cmd.append("--purge-logs")
    os.execv("/bin/bash", cmd)


def cmd_log(args):
    # Prefer unified log file
    log_file = STATE_DIR / "server.log"
    if not log_file.exists():
        print(f"No log file found at {log_file}", file=sys.stderr)
        sys.exit(1)
    if args.follow:
        os.execv("/usr/bin/tail", ["/usr/bin/tail", "-f", str(log_file)])
    else:
        os.execv("/usr/bin/tail", ["/usr/bin/tail", f"-{args.lines}", str(log_file)])


def cmd_server_start(args):
    """Launch server in a new tmux window (non-blocking)."""
    script = str(SCRIPTS_DIR / "launch.sh")
    if not os.path.exists(script):
        print(f"ERROR: Script not found: {script}", file=sys.stderr)
        sys.exit(1)
    # Check tmux is available
    if not shutil.which("tmux"):
        print("ERROR: tmux not found. Install with: brew install tmux", file=sys.stderr)
        sys.exit(1)
    # Ensure a tmux session exists
    sess = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True
    )
    if sess.returncode != 0 or not sess.stdout.strip():
        subprocess.run(["tmux", "new-session", "-d", "-s", "ccswitch"], capture_output=True)
    # Open launch.sh in a new tmux window (non-blocking for caller)
    result = subprocess.run(
        ["tmux", "new-window", "-n", "claude-server", "-P", "-F", "#{session_name}:#{window_index}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("ERROR: Failed to create tmux window.", file=sys.stderr)
        sys.exit(1)
    pane = result.stdout.strip()
    subprocess.run(["tmux", "send-keys", "-t", pane, f"bash '{script}'", "Enter"])
    print(f"Server starting in tmux window: {pane}")
    print(f"Attach with: tmux attach -t {pane.split(':')[0]}")


def cmd_server_stop(args):
    """Stop the server. Unloads LaunchAgent first if loaded."""
    PLIST = Path.home() / "Library" / "LaunchAgents" / "com.ccswitch.manager.plist"
    uid = os.getuid()
    # Check if LaunchAgent is loaded
    if PLIST.exists():
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/com.ccswitch.manager"],
            capture_output=True
        )
        if result.returncode == 0:
            print("Unloading LaunchAgent...")
            unload = subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}", str(PLIST)],
                capture_output=True
            )
            if unload.returncode != 0:
                # Fall back to legacy unload
                subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)

    # Now kill the process
    pid_file = STATE_DIR / "server.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to PID {pid}.")
            pid_file.unlink(missing_ok=True)
        except (ValueError, ProcessLookupError, PermissionError) as e:
            print(f"PID file issue: {e}", file=sys.stderr)
    else:
        # Try pgrep fallback
        result = subprocess.run(
            ["pgrep", "-f", "uvicorn backend.main:app"],
            capture_output=True, text=True
        )
        if result.stdout.strip():
            for pid_str in result.stdout.strip().splitlines():
                try:
                    os.kill(int(pid_str), signal.SIGTERM)
                    print(f"Sent SIGTERM to PID {pid_str}.")
                except (ValueError, ProcessLookupError):
                    pass
        else:
            print("Server does not appear to be running.")


def build_parser():
    p = argparse.ArgumentParser(prog="ccswitch", description="CCSwitch CLI — auto-switch Claude.ai accounts")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all accounts").set_defaults(func=cmd_list)

    sw = sub.add_parser("switch", help="Switch to account by email")
    sw.add_argument("email")
    sw.set_defaults(func=cmd_switch)

    en = sub.add_parser("enable", help="Enable account")
    en.add_argument("email")
    en.set_defaults(func=cmd_enable)

    dis = sub.add_parser("disable", help="Disable account")
    dis.add_argument("email")
    dis.set_defaults(func=cmd_disable)

    sub.add_parser("status", help="Show server and account status").set_defaults(func=cmd_status)

    # service subgroup
    svc = sub.add_parser("service", help="Manage LaunchAgent service")
    svc_sub = svc.add_subparsers(dest="service_command", required=True)
    svc_sub.add_parser("install", help="Install and start LaunchAgent").set_defaults(func=cmd_service_install)
    rm = svc_sub.add_parser("remove", help="Remove LaunchAgent")
    rm.add_argument("--purge-logs", action="store_true", help="Also delete log files")
    rm.set_defaults(func=cmd_service_remove)

    # log
    log = sub.add_parser("log", help="Show server log")
    log.add_argument("-f", "--follow", action="store_true", help="Follow log (like tail -f)")
    log.add_argument("-n", "--lines", type=int, default=50, help="Number of lines (default: 50)")
    log.set_defaults(func=cmd_log)

    # server subgroup
    srv = sub.add_parser("server", help="Start/stop server")
    srv_sub = srv.add_subparsers(dest="server_command", required=True)
    srv_sub.add_parser("start", help="Start server in a new tmux window").set_defaults(func=cmd_server_start)
    srv_sub.add_parser("stop", help="Stop server (unloads LaunchAgent if needed)").set_defaults(func=cmd_server_stop)

    return p


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
