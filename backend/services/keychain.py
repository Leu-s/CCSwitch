"""
Minimal keychain/config helpers.

With the isolated-config-dir approach, each account has its own
CLAUDE_CONFIG_DIR.  This module is intentionally thin — credential
management lives in account_service.py.
"""

import os
import json


def get_active_email(config_dir: str) -> str | None:
    """Return the email of the account currently active in config_dir."""
    path = os.path.join(os.path.expanduser(config_dir), ".claude.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("oauthAccount", {}).get("emailAddress")
    except Exception:
        return None
