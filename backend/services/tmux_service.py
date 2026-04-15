"""
tmux helpers.

Two responsibilities:

1. Thin wrappers around ``tmux list-panes`` / ``capture-pane`` / ``send-keys``
   used by the background switch flow.
2. ``wake_stalled_sessions(message)`` — after an account switch, scan every
   tmux pane on the box, and for any pane whose recent output matches a
   rate-limit/usage-limit message send a nudge so that already-running Claude
   Code sessions pick up the freshly-mirrored credentials and continue.

There are no user-managed monitor rows or LLM-based evaluators anymore.  One
toggle (``tmux_nudge_enabled``) and one message string (``tmux_nudge_message``)
control the whole feature.
"""

import asyncio
import logging
import os
import re

from ..database import AsyncSessionLocal
from . import settings_service as ss

logger = logging.getLogger(__name__)


# Patterns that mean "this Claude Code pane is stalled on a rate-limit screen
# and needs a nudge to continue".  Matched case-insensitively against the last
# few hundred lines of the pane.  Kept conservative to avoid false positives
# on benign output that happens to contain the word "limit".
#
# Confirmed against real messages surfaced by Claude Code in its terminal UI
# (Anthropic GitHub issues #2087, #5977, #6457, #6488, #9046, #9236, #35487,
# #35704, #35785 + Claude Help Center "Troubleshoot Claude error messages"):
#
#   "Claude AI usage limit reached|1749924000"
#   "Claude usage limit reached. Your limit will reset at 2pm (America/New_York)"
#   "⎿ 5-hour limit reached ∙ resets 18:00"
#   "5-hour limit resets 17:00 - continuing with extra usage"
#   "Approaching usage limit (95%)"
#   "rate_limit_error"  /  "This request would exceed your account's rate limit"
#   "Anthropic API Error: Overloaded Error (529)"
#   "HTTP 529 Service Overloaded"  /  "overloaded_error"
_STALL_PATTERNS = re.compile(
    r"("
    r"usage limit reached"
    r"|approaching usage limit"
    r"|claude usage limit"
    r"|claude.+limit reached"
    r"|\d+-hour limit (reached|resets)"
    r"|rate limit(ed| exceeded| reached)?"
    r"|rate_limit_error"
    r"|overloaded_error"
    r"|api error.*overloaded"
    r"|service overloaded"
    r"|try again later"
    r")",
    re.IGNORECASE,
)

# Number of pane lines we capture per scan.  Big enough to catch a recent
# rate-limit notice that has scrolled past the visible region, small enough
# that capturing every pane is cheap.
_CAPTURE_LINES = 200


# Tab-separated so user-option values that contain spaces or shell metacharacters
# survive intact.  A ``#{@user-option}`` that is unset expands to an empty string;
# the ``maxsplit=3`` below preserves that as the literal ``""``.
_PANE_FORMAT = (
    "#{session_name}:#{window_index}.#{pane_index}"
    "\t#{pane_pid}"
    "\t#{pane_current_command}"
    "\t#{@ccswitch-nudge}"
)


def _opt_in_value(raw: str) -> bool:
    """True when a tmux user option's raw value reads as 'on' (case-insensitive).

    Strict equality is intentional.  A value with embedded whitespace or
    trailing junk (e.g. ``"on\textra"`` — possible if the user shell-quoted
    a tab into the option value) reads as opt-OUT.  False-negative is the
    safer failure mode for this flag — we prefer skipping a legit opt-in
    than nudging a pane whose opt-in value was mangled.
    """
    return raw.strip().lower() == "on"


async def list_panes() -> list[dict]:
    """Return one dict per tmux pane: ``{target, pid, command, opt_in}``.

    ``pid`` is the shell PID (``#{pane_pid}``), not the foreground process —
    exactly the root for the descendant-walk in ``_pane_has_claude_descendant``.
    ``opt_in`` is ``True`` only when the pane's ``@ccswitch-nudge`` user option
    is set to ``on``; an unset option parses as an empty string and maps to
    ``False``.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-panes", "-a", "-F", _PANE_FORMAT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("tmux list-panes timed out — killing subprocess")
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return []
        panes = []
        for line in stdout.decode().strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 3)
            # Pad missing fields (very old tmux that ignores a format token
            # leaves the slot absent rather than empty).
            while len(parts) < 4:
                parts.append("")
            target, pid_raw, command, opt_in_raw = parts
            try:
                pid = int(pid_raw) if pid_raw else None
            except ValueError:
                pid = None
            panes.append({
                "target": target,
                "pid": pid,
                "command": command,
                "opt_in": _opt_in_value(opt_in_raw),
            })
        return panes
    except FileNotFoundError:
        return []


async def send_keys(target: str, text: str, press_enter: bool = True) -> None:
    # 1. Send literal text (use -l so key-name tokens are not interpreted)
    proc = await asyncio.create_subprocess_exec(
        "tmux", "send-keys", "-t", target, "-l", text,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        logger.warning("tmux send-keys (literal) timed out — killing subprocess")
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return

    # 2. Send Enter as a separate call (key-name, so -l must be absent)
    if press_enter:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", target, "Enter",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("tmux send-keys (Enter) timed out — killing subprocess")
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass


async def capture_pane(target: str, lines: int = _CAPTURE_LINES) -> str:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        logger.warning("tmux capture-pane timed out — killing subprocess")
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return ""
    return stdout.decode(errors="replace")


def looks_stalled(capture: str) -> bool:
    """True if the pane capture contains text that looks like a Claude Code
    rate-limit / usage-limit notice waiting for the user to continue."""
    if not capture:
        return False
    return bool(_STALL_PATTERNS.search(capture))


# Claude Code's native (post-2.1.100) installer ships the CLI with
# argv[0] set to the bare version string — tmux then reports
# ``pane_current_command = "2.1.108"`` instead of ``claude``.  Match that
# shape so the nudge finds native-install panes, not just the legacy
# ``npm i -g @anthropic-ai/claude-code`` ones.
_SEMVER_COMMAND_RE = re.compile(r"^\d+\.\d+\.\d+([.\-+].*)?$")


def _looks_like_claude_pane(command: str) -> bool:
    """True if ``command`` (from ``pane_current_command``) looks like a Claude
    Code process.

    Matches three real-world shapes:

    * ``claude`` basename — legacy npm-global install
      (``/usr/local/bin/claude`` or plain ``claude``).
    * ``python -m claude`` — wrapper invocations.
    * Bare semver like ``2.1.108`` — the post-2.1.100 native installer
      ships the binary with argv[0] set to the version string, and
      that's what tmux's ``pane_current_command`` reports.

    Used by ``wake_stalled_sessions`` so a stray rate-limit substring in
    a shell pane's scrollback does not cause us to type the nudge message
    into that shell (which would execute it as a command — self-footgun).
    """
    if not command:
        return False
    cmd = command.strip().lower()
    basename = os.path.basename(cmd.split()[0]) if cmd else ""
    if basename.startswith("claude"):
        return True
    # Handle "python -m claude" style wrappers.
    tokens = cmd.split()
    if len(tokens) >= 3 and tokens[0].startswith("python") and tokens[1] == "-m":
        return tokens[2].startswith("claude")
    # Native installer (2.1.100+): pane_current_command == "2.1.108" etc.
    if _SEMVER_COMMAND_RE.match(basename):
        return True
    return False


async def wake_stalled_sessions(message: str) -> dict:
    """Scan every tmux pane on the box and send ``message`` to each one whose
    recent output matches a rate-limit notice AND is running ``claude``.

    Returns a summary dict suitable for logging:

        {
          "scanned": int,    # total panes inspected
          "nudged":  [target, ...],
          "errors":  [{"target": ..., "error": ...}],
        }

    Used by the background switch flow as a "kick stalled sessions" step
    after every successful ``perform_switch``.  Safe to call when no panes
    match — does nothing in that case.
    """
    summary = {"scanned": 0, "nudged": [], "errors": []}
    if not message:
        return summary

    # Defensive length cap — the router already validates ALLOWED_KEYS, but
    # defense in depth is cheap and prevents us from blasting an arbitrarily
    # long string into every matching pane.
    if len(message) > 256:
        logger.warning(
            "tmux nudge message too long (%d chars) — truncating to 256",
            len(message),
        )
        message = message[:256]

    panes = await list_panes()
    summary["scanned"] = len(panes)

    for pane in panes:
        target = pane.get("target")
        if not target:
            continue
        command = pane.get("command") or ""
        if not _looks_like_claude_pane(command):
            logger.debug(
                "tmux nudge: skipping %s — not a claude pane (command=%r)",
                target, command,
            )
            continue
        try:
            capture = await capture_pane(target)
            if not looks_stalled(capture):
                continue
            await send_keys(target, message, press_enter=True)
            summary["nudged"].append(target)
            logger.info("tmux nudge sent to %s (%s)", target, command)
        except Exception as e:
            summary["errors"].append({"target": target, "error": str(e)})
            logger.warning("tmux nudge failed for %s: %s", target, e)
    return summary


# ── Post-switch nudge orchestration ───────────────────────────────────────────
# Called fire-and-forget by switcher.perform_switch so a slow tmux scan cannot
# stall the poll loop.  Opens its own DB session because the caller's session
# is released as soon as perform_switch returns — using it from a background
# task would race with session pool lifecycle.


async def _nudge_if_enabled() -> None:
    """Read the two nudge settings in a fresh DB session and, if enabled,
    scan every pane for a rate-limit notice and send the configured message."""
    async with AsyncSessionLocal() as db:
        enabled = await ss.get_bool("tmux_nudge_enabled", False, db)
        if not enabled:
            return
        message = await ss.get_setting("tmux_nudge_message", "continue", db)
    try:
        summary = await wake_stalled_sessions(message)
        if summary["nudged"]:
            logger.info(
                "tmux nudge: scanned %d pane(s), nudged %d (%s)",
                summary["scanned"], len(summary["nudged"]),
                ", ".join(summary["nudged"]),
            )
        elif summary["scanned"]:
            logger.debug(
                "tmux nudge: scanned %d pane(s), no rate-limit notices found",
                summary["scanned"],
            )
    except Exception as e:
        logger.warning("tmux nudge failed: %s", e)


def fire_nudge() -> None:
    """Schedule ``_nudge_if_enabled`` as a background task.  Safe to call from
    any async context; returns immediately so the caller is never blocked by a
    slow tmux scan."""
    async def _run():
        try:
            await _nudge_if_enabled()
        except Exception as e:
            logger.warning("fire_nudge task failed: %s", e)
    asyncio.create_task(_run())
