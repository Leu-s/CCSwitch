"""
Credential-target discovery and mirroring.

Different tools that integrate with Claude Code (vanilla CLI, the VSCode
extension, Gas Town, this service, ad-hoc shell setups, etc.) read or write
``oauthAccount`` from different ``.claude.json`` locations on the same machine.
Without coordination, switching accounts in this dashboard updates only one of
them, which is exactly the "UI shows one, reality is another" failure.

This module:

* Walks a set of well-known patterns and reports every ``.claude.json`` it
  finds, deduplicated by canonical (symlink-resolved) path.
* Keeps the user's "which of those should the dashboard mirror to on every
  switch" decision in the ``credential_targets`` settings row as a JSON map
  ``{canonical_path: bool}``.
* Defaults every target to *disabled*.  The user must explicitly opt-in via the
  UI before the service touches any system file beyond the isolated account
  dirs we own under ``~/.claude-multi-accounts/``.
* Exposes the list of currently-enabled targets so ``activate_account_config``
  can fan out the ``oauthAccount``/``userID`` merge across all of them.
"""

import asyncio
import glob
import json
import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession

from . import settings_service as ss

logger = logging.getLogger(__name__)

_SETTING_KEY = "credential_targets"

# Keys in `.claude.json` that carry the active OAuth identity. Mirroring writes
# only these — never workspace state — so unrelated fields in each target file
# (projects, mcpServers, hooks, etc.) are preserved across switches.
_IDENTITY_KEYS = ("oauthAccount", "userID")


def _home() -> str:
    return os.path.expanduser("~")


def _safe_realpath(path: str) -> str:
    """Resolve symlinks but do not crash on missing parents."""
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.abspath(path)


def _read_email(path: str) -> str | None:
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    return ((data.get("oauthAccount") or {}).get("emailAddress")) or None


def _label(display_path: str, canonical_path: str) -> str:
    """Human-friendly label, with `→ realpath` suffix when symlinked."""
    home = _home()
    short = display_path.replace(home, "~", 1)
    if canonical_path != display_path:
        short_real = canonical_path.replace(home, "~", 1)
        return f"{short}  →  {short_real}"
    return short


# ── Discovery ─────────────────────────────────────────────────────────────────


def _discovery_patterns() -> list[str]:
    """Glob patterns scanned on every discovery run.

    Add new patterns here when a new tool with its own ``.claude.json``
    convention shows up in the wild.
    """
    home = _home()
    return [
        os.path.join(home, ".claude.json"),
        os.path.join(home, ".claude", ".claude.json"),
        os.path.join(home, ".claude-accounts", "*", ".claude.json"),
        os.path.join(home, ".config", "claude", "*", ".claude.json"),
    ]


def discover_targets() -> list[dict]:
    """Scan the filesystem for known ``.claude.json`` locations.

    Returns a list of dicts ordered by display path:

        {
          "path":          "/abs/display/path/.claude.json",
          "canonical":     "/abs/symlink-resolved/path/.claude.json",
          "label":         "human-friendly label",
          "exists":        bool,
          "current_email": "..." | None,
        }

    Deduped by *canonical* path so a symlink to the same target file is
    reported only once (under the first display path that resolved to it).
    """
    seen_canonical: set[str] = set()
    out: list[dict] = []
    for pattern in _discovery_patterns():
        for display in sorted(glob.glob(pattern)):
            canonical = _safe_realpath(display)
            if canonical in seen_canonical:
                continue
            seen_canonical.add(canonical)
            exists = os.path.isfile(display)
            out.append({
                "path": display,
                "canonical": canonical,
                "label": _label(display, canonical),
                "exists": exists,
                "current_email": _read_email(display) if exists else None,
            })
    return out


# ── Persistent enabled-state ──────────────────────────────────────────────────


async def _load_state(db: AsyncSession) -> dict[str, bool]:
    """Return ``{canonical_path: enabled}`` map from the DB."""
    raw = await ss.get_json(_SETTING_KEY, {}, db)
    if not isinstance(raw, dict):
        return {}
    return {str(k): bool(v) for k, v in raw.items()}


async def _save_state(state: dict[str, bool], db: AsyncSession) -> None:
    await ss.set_json(_SETTING_KEY, state, db)


async def list_targets(db: AsyncSession) -> list[dict]:
    """Discovery + enabled state, joined for the UI.

    Each discovered target gets an ``enabled`` boolean from the DB
    (``False`` if not previously stored).  Discovered targets that no longer
    exist on disk are still returned so the user can see *why* they cannot be
    enabled.  Stale DB entries (target deleted from disk and never re-found)
    are also included with ``exists=False`` so the user can clean them up.
    """
    state = await _load_state(db)
    discovered = await asyncio.to_thread(discover_targets)

    by_canonical = {t["canonical"]: t for t in discovered}
    # Surface DB entries that are no longer discoverable, so the user can see
    # them in the UI as "missing" instead of silently dropping them.
    for canonical, enabled in state.items():
        if canonical in by_canonical:
            continue
        by_canonical[canonical] = {
            "path": canonical,
            "canonical": canonical,
            "label": canonical.replace(_home(), "~", 1) + "  (missing)",
            "exists": False,
            "current_email": None,
        }

    out = []
    for canonical, entry in sorted(by_canonical.items()):
        out.append({**entry, "enabled": bool(state.get(canonical, False))})
    return out


async def set_target_enabled(
    canonical_path: str, enabled: bool, db: AsyncSession
) -> None:
    """Flip the ``enabled`` flag for a single canonical target.

    When enabling, the path MUST appear in the current ``discover_targets()``
    result set — otherwise a caller with local API access (or a typoing user)
    could steer the mirror fan-out at arbitrary files (``~/.zshrc``,
    ``~/.ssh/authorized_keys``, …).  Disabling is always allowed so stale DB
    entries surfaced by ``list_targets`` as "missing" can be cleared.
    """
    if enabled:
        discovered = {t["canonical"] for t in discover_targets()}
        if canonical_path not in discovered:
            raise ValueError(
                f"canonical path not in discovered targets: {canonical_path}"
            )
    state = await _load_state(db)
    if enabled:
        state[canonical_path] = True
    else:
        state.pop(canonical_path, None)
    await _save_state(state, db)


def enabled_canonical_paths_sync(state: dict[str, bool]) -> list[str]:
    """Filter helper for sync code paths that already have the state map."""
    return [p for p, v in state.items() if v]


async def enabled_canonical_paths(db: AsyncSession) -> list[str]:
    state = await _load_state(db)
    return enabled_canonical_paths_sync(state)


# ── Mirroring (the actual write step on every switch) ─────────────────────────


def _atomic_write_json(path: str, data: dict, mode: int = 0o600) -> None:
    import threading
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
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


def mirror_oauth_into_targets(
    source_config_dir: str, target_paths: list[str]
) -> dict:
    """Write ``oauthAccount`` + ``userID`` from ``source_config_dir/.claude.json``
    into every path in ``target_paths`` (canonical paths; symlinks are followed
    naturally by the OS).

    Only the identity keys are written.  Every other field already in each
    target file is preserved untouched, so the user's projects/MCP/onboarding
    state in their main Claude Code config dir is never disturbed.

    Returns a summary dict ``{written: [...], skipped: [...], errors: [...]}``
    suitable for logging and surfacing back through the WS broadcast.
    """
    source_file = os.path.join(source_config_dir, ".claude.json")
    summary = {"written": [], "skipped": [], "errors": []}

    if not os.path.isfile(source_file):
        summary["errors"].append(
            f"source .claude.json missing: {source_file}"
        )
        logger.warning("mirror: source missing: %s", source_file)
        return summary

    try:
        with open(source_file) as f:
            source = json.load(f)
    except Exception as e:
        summary["errors"].append(f"source unreadable ({source_file}): {e}")
        return summary

    if not source.get("oauthAccount"):
        summary["errors"].append(
            f"source has no oauthAccount: {source_file}"
        )
        return summary

    source_email = (source.get("oauthAccount") or {}).get("emailAddress", "?")

    if not target_paths:
        summary["skipped"].append(
            "no targets enabled — open the dashboard, pick which "
            "system files to mirror under 'Credential targets'"
        )
        logger.info(
            "mirror: no targets enabled, source=%s skipped",
            source_email,
        )
        return summary

    for path in target_paths:
        try:
            existing = {}
            if os.path.isfile(path):
                try:
                    with open(path) as f:
                        existing = json.load(f)
                except Exception:
                    # Treat unreadable target file as empty so the write still
                    # repairs corruption rather than crashing the whole switch.
                    existing = {}
            for k in _IDENTITY_KEYS:
                if k in source:
                    existing[k] = source[k]
            _atomic_write_json(path, existing)
            summary["written"].append(path)
            logger.info("mirror: wrote %s → %s", source_email, path)
        except Exception as e:
            summary["errors"].append(f"{path}: {e}")
            logger.warning("mirror: failed for %s: %s", path, e)

    return summary
