"""migrate_to_vault_and_drop_config_dir

Revision ID: a7e16baf4a03
Revises: e2d620dcfbec
Create Date: 2026-04-15 14:00:00.000000

One-shot migration to the vault-swap architecture.  Reads legacy
per-config-dir credentials (hashed Keychain entries + isolated
``~/.ccswitch-accounts/`` directories) and promotes them into the new
``ccswitch-vault`` Keychain namespace keyed by email.  Then removes
the legacy filesystem structures and drops the ``config_dir`` column
from the ``accounts`` table.

Destructive on purpose.  A JSON backup is written to
``~/.ccswitch-backup-2026-04-15.json`` before any mutation, containing
every DB row (with config_dir), the hashed Keychain contents base64
encoded, the pointer file, and ``~/.claude/.claude.json``.  A user who
needs to downgrade restores this backup by hand — ``downgrade()``
below raises NotImplementedError.

See ``docs/superpowers/specs/2026-04-15-vault-swap-architecture.md``
section 4 for the full rationale and step-by-step description.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import json
import logging
import os
import shutil
import subprocess
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7e16baf4a03"
down_revision: Union[str, Sequence[str], None] = "e2d620dcfbec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


logger = logging.getLogger("alembic.migrate_to_vault")


# ── Keychain primitives (inlined so the migration has no runtime deps) ────


_STANDARD_SERVICE = "Claude Code-credentials"
_VAULT_SERVICE = "ccswitch-vault"
_KEYCHAIN_TIMEOUT = 5


def _read_keychain(service: str, account: str) -> dict | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_TIMEOUT,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _write_keychain(service: str, account: str, payload: dict, comment: str | None = None) -> bool:
    cred_json = json.dumps(payload)
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", service, "-a", account],
            capture_output=True,
            timeout=_KEYCHAIN_TIMEOUT,
        )
        cmd = [
            "security", "add-generic-password",
            "-s", service, "-a", account, "-w", cred_json,
        ]
        if comment:
            cmd.extend(["-j", comment])
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_KEYCHAIN_TIMEOUT,
        )
        return result.returncode == 0
    except Exception:
        return False


def _delete_keychain(service: str, account: str) -> None:
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", service, "-a", account],
            capture_output=True,
            timeout=_KEYCHAIN_TIMEOUT,
        )
    except Exception:
        pass


def _list_services_with_prefix(prefix: str) -> list[str]:
    try:
        result = subprocess.run(
            ["security", "dump-keychain"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return []
    found: set[str] = set()
    marker = '"svce"<blob>="'
    for line in result.stdout.splitlines():
        if marker in line:
            start = line.index(marker) + len(marker)
            end = line.rfind('"')
            if end > start:
                svc = line[start:end]
                if svc.startswith(prefix):
                    found.add(svc)
    return sorted(found)


def _legacy_hashed_service(config_dir: str) -> str:
    h = hashlib.sha256(config_dir.encode()).hexdigest()[:8]
    return f"{_STANDARD_SERVICE}-{h}"


def _load_json_safe(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _atomic_write_json(path: str, data: dict, mode: int = 0o600) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp = f"{path}.migrate.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ── Actual migration ───────────────────────────────────────────────────────


_HOME = os.path.expanduser("~")
_POINTER_PATH = os.path.join(_HOME, ".ccswitch", "active")
_CCSWITCH_DIR = os.path.join(_HOME, ".ccswitch")
_ACCOUNTS_DIR = os.path.join(_HOME, ".ccswitch-accounts")
# Claude Code CLI's identity file lives at HOME ROOT (not inside ~/.claude/).
_CLAUDE_JSON = os.path.join(_HOME, ".claude.json")
_CREDENTIALS_JSON = os.path.join(_HOME, ".claude", ".credentials.json")
_BACKUP_PATH = os.path.join(_HOME, ".ccswitch-backup-2026-04-15.json")
_VAULT_COMMENT = (
    "CCSwitch subscription vault — do not delete. "
    "Managed by the CCSwitch dashboard at http://127.0.0.1:41924."
)


def _read_legacy_credentials(config_dir: str) -> dict | None:
    """Read credentials for a legacy account: hashed Keychain first,
    file fallback second."""
    kc = _read_keychain(_legacy_hashed_service(config_dir), getpass.getuser())
    if kc:
        return kc
    for filename in [".credentials.json", "credentials.json", ".claude.json"]:
        path = os.path.join(config_dir, filename)
        data = _load_json_safe(path)
        if not data:
            continue
        if "claudeAiOauth" in data:
            return data
        if "accessToken" in data:
            return {"claudeAiOauth": {
                k: data[k]
                for k in ("accessToken", "refreshToken", "expiresAt", "subscriptionType")
                if k in data
            }}
    return None


def _build_vault_blob(
    creds: dict | None,
    config_dir: str,
) -> dict | None:
    """Build the canonical vault blob shape from legacy credentials +
    the config_dir's ``.claude.json`` identity metadata."""
    if not creds:
        return None
    tokens = creds.get("claudeAiOauth")
    if not isinstance(tokens, dict):
        tokens = {
            k: creds[k]
            for k in ("accessToken", "refreshToken", "expiresAt", "subscriptionType")
            if k in creds
        }
    if not tokens.get("refreshToken"):
        return None
    blob: dict = {"claudeAiOauth": tokens}

    identity = _load_json_safe(os.path.join(config_dir, ".claude.json"))
    oauth_account = identity.get("oauthAccount")
    if isinstance(oauth_account, dict):
        blob["oauthAccount"] = oauth_account
    user_id = identity.get("userID")
    if isinstance(user_id, str):
        blob["userID"] = user_id
    return blob


def _write_backup(rows: list[tuple], legacy_services: list[str]) -> None:
    backup: dict = {
        "schema": "ccswitch-vault-migration/1",
        "accounts": [
            {"id": r[0], "email": r[1], "config_dir": r[2]}
            for r in rows
        ],
        "hashed_keychain_entries": {},
        "pointer_file": None,
        "claude_json": None,
    }
    for svc in legacy_services:
        entry = _read_keychain(svc, getpass.getuser())
        if entry:
            backup["hashed_keychain_entries"][svc] = base64.b64encode(
                json.dumps(entry).encode()
            ).decode()
    try:
        with open(_POINTER_PATH) as f:
            backup["pointer_file"] = f.read()
    except Exception:
        pass
    try:
        with open(_CLAUDE_JSON) as f:
            backup["claude_json"] = f.read()
    except Exception:
        pass

    try:
        fd = os.open(_BACKUP_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(backup, f, indent=2)
        logger.warning(
            "Vault migration backup written to %s — preserve this file "
            "until you have verified the new architecture is working",
            _BACKUP_PATH,
        )
    except Exception as e:
        logger.error("Failed to write migration backup: %s", e)


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # If config_dir does not exist yet the DB is already migrated.  Treat
    # the whole body as idempotent — still attempt to clean up any legacy
    # filesystem remnants but skip DB reads and the final drop.
    columns = {c["name"] for c in inspector.get_columns("accounts")}
    has_config_dir = "config_dir" in columns

    rows: list[tuple] = []
    if has_config_dir:
        result = conn.execute(
            sa.text("SELECT id, email, config_dir FROM accounts ORDER BY id")
        )
        rows = [tuple(r) for r in result.fetchall()]

    # Collect every legacy hashed Keychain service.  Only write a backup when
    # there is actual legacy state to back up — otherwise a hypothetical
    # re-run of upgrade() on an already-migrated DB would clobber the
    # original backup with a near-empty one.
    legacy_services = _list_services_with_prefix(_STANDARD_SERVICE + "-")
    if has_config_dir:
        _write_backup(rows, legacy_services)

    # ── 1. Move credentials into the vault per-account ─────────────────────
    stale_ids: list[int] = []
    for acct_id, email, config_dir in rows:
        existing_vault = _read_keychain(_VAULT_SERVICE, email)
        if existing_vault and (existing_vault.get("claudeAiOauth") or existing_vault.get("refreshToken")):
            logger.info("Skipping %s — vault entry already present", email)
            continue

        creds = _read_legacy_credentials(config_dir) if config_dir else None
        blob = _build_vault_blob(creds, config_dir or "") if creds else None

        if not blob:
            logger.warning(
                "Could not recover credentials for %s (config_dir=%s) — "
                "marking account stale for user re-login",
                email, config_dir,
            )
            stale_ids.append(acct_id)
            continue

        if _write_keychain(_VAULT_SERVICE, email, blob, comment=_VAULT_COMMENT):
            logger.info("Migrated %s into vault", email)
            if config_dir:
                _delete_keychain(_legacy_hashed_service(config_dir), getpass.getuser())
        else:
            logger.error("Failed to write vault entry for %s", email)
            stale_ids.append(acct_id)

    # ── 2. Determine the active account ────────────────────────────────────
    active_email: str | None = None
    try:
        with open(_POINTER_PATH) as f:
            pointer = f.read().strip()
        for _, email, config_dir in rows:
            if config_dir and os.path.realpath(config_dir) == os.path.realpath(pointer):
                active_email = email
                break
    except Exception:
        pass
    if not active_email:
        claude_data = _load_json_safe(_CLAUDE_JSON)
        oauth_account = claude_data.get("oauthAccount") or {}
        maybe_email = oauth_account.get("emailAddress")
        if maybe_email and any(r[1] == maybe_email for r in rows):
            active_email = maybe_email

    # ── 3. Promote active account's vault entry into the standard entry ───
    if active_email:
        active_blob = _read_keychain(_VAULT_SERVICE, active_email)
        if active_blob:
            _write_keychain(_STANDARD_SERVICE, getpass.getuser(), active_blob)
            oauth_account = active_blob.get("oauthAccount")
            if isinstance(oauth_account, dict):
                claude_data = _load_json_safe(_CLAUDE_JSON)
                claude_data["oauthAccount"] = oauth_account
                if isinstance(active_blob.get("userID"), str):
                    claude_data["userID"] = active_blob["userID"]
                try:
                    _atomic_write_json(_CLAUDE_JSON, claude_data)
                except Exception as e:
                    logger.warning("Failed to update %s: %s", _CLAUDE_JSON, e)
            try:
                _atomic_write_json(_CREDENTIALS_JSON, active_blob)
            except Exception as e:
                logger.warning("Failed to write %s: %s", _CREDENTIALS_JSON, e)
            logger.info("Active account set to %s", active_email)

    # ── 4. Orphan hashed Keychain sweep ────────────────────────────────────
    # Any ``Claude Code-credentials-<hash>`` entry remaining after the
    # per-account migration is either a stale leftover from a deleted
    # account or belonged to a config_dir we could not find.  Delete all
    # of them — the standard (unhashed) entry and vault entries are the
    # only ones the new architecture uses.
    for svc in legacy_services:
        if svc == _STANDARD_SERVICE:
            continue
        _delete_keychain(svc, getpass.getuser())
        logger.info("Removed orphan hashed Keychain entry: %s", svc)

    # ── 5. Remove legacy filesystem structures ─────────────────────────────
    if os.path.isdir(_ACCOUNTS_DIR):
        try:
            shutil.rmtree(_ACCOUNTS_DIR)
            logger.info("Removed %s", _ACCOUNTS_DIR)
        except Exception as e:
            logger.warning("Failed to rmtree %s: %s", _ACCOUNTS_DIR, e)
    if os.path.isfile(_POINTER_PATH):
        try:
            os.unlink(_POINTER_PATH)
        except Exception as e:
            logger.debug("Failed to remove %s: %s", _POINTER_PATH, e)
    if os.path.isdir(_CCSWITCH_DIR):
        try:
            os.rmdir(_CCSWITCH_DIR)
        except OSError:
            pass

    # ── 6. Flag un-migratable accounts as stale ────────────────────────────
    for acct_id in stale_ids:
        conn.execute(
            sa.text(
                "UPDATE accounts SET stale_reason = :reason WHERE id = :id"
            ),
            {
                "reason": "No access token in vault — re-login required",
                "id": acct_id,
            },
        )

    # ── 7. Drop the config_dir column ──────────────────────────────────────
    if has_config_dir:
        with op.batch_alter_table("accounts", schema=None) as batch_op:
            batch_op.drop_column("config_dir")


def downgrade() -> None:
    """Downgrade is not supported.

    This migration removes the legacy filesystem + hashed Keychain
    structures that the pre-vault code required.  A user who needs to
    revert restores ``~/.ccswitch-backup-2026-04-15.json`` by hand and
    runs an older CCSwitch build against an older DB.
    """
    raise NotImplementedError(
        "Vault migration is one-way. Restore from "
        "~/.ccswitch-backup-2026-04-15.json manually to revert."
    )
