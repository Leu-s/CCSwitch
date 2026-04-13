import subprocess
import json
import os
import re

ACTIVE_SERVICE = "Claude Code-credentials"
SUFFIX_PREFIX = "Claude Code-credentials-"

def read_credentials(suffix: str) -> dict:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", f"{SUFFIX_PREFIX}{suffix}", "-w"],
        capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout.strip())

def read_active_credentials() -> dict:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", ACTIVE_SERVICE,
         "-a", os.environ.get("USER", "user"), "-w"],
        capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout.strip())

def write_active_credentials(creds: dict) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U",
         "-s", ACTIVE_SERVICE,
         "-a", os.environ.get("USER", "user"),
         "-w", json.dumps(creds)],
        capture_output=True, text=True, check=True
    )

def scan_keychain() -> list[str]:
    """Return list of unique suffix strings for Claude Code credential entries."""
    result = subprocess.run(
        ["security", "dump-keychain"],
        capture_output=True, text=True
    )
    suffixes = re.findall(r'Claude Code-credentials-([a-f0-9]{8})', result.stdout)
    return list(set(suffixes))

def get_claude_config(config_dir: str) -> dict:
    path = os.path.join(os.path.expanduser(config_dir), ".claude.json")
    with open(path) as f:
        return json.load(f)

def update_oauth_account(config_dir: str, oauth_account: dict) -> None:
    path = os.path.join(os.path.expanduser(config_dir), ".claude.json")
    with open(path) as f:
        config = json.load(f)
    config["oauthAccount"] = oauth_account
    with open(path, "w") as f:
        json.dump(config, f, indent=2)

def get_active_email(config_dir: str) -> str | None:
    try:
        config = get_claude_config(config_dir)
        return config.get("oauthAccount", {}).get("emailAddress")
    except Exception:
        return None
