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

# Tail window the stall regex is allowed to match against.  Much narrower
# than ``_CAPTURE_LINES`` so a rate-limit banner the user already resolved
# 5 minutes ago and scrolled past does NOT keep matching on every swap.
# A fresh banner always renders at the bottom of the buffer; restricting
# the match to the last 20 lines keeps recall for real stalls and drops
# the "stale scrollback A3 attack" surface from 200 lines to 20.
_STALL_TAIL_LINES = 20

# ANSI escape stripping before regex match.  Tmux's ``capture-pane -p``
# already strips most SGR sequences, but colourised banners (24-bit RGB,
# OSC sequences, mouse-tracking CSIs) can slip through on certain
# terminals.  Matching the stall patterns against raw capture then failing
# because of an embedded ``\x1b[31m`` would be a silent recall miss.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;:?]*[ -/]*[@-~]"    # CSI (SGR inc. colon-form 24-bit, cursor)
    r"|\x1b\][^\x07]*(?:\x07|\x1b\\)"  # OSC (hyperlinks, title)
)


def _strip_ansi(text: str) -> str:
    # Fast-path: most captures have zero escapes; skip the regex scan.
    if "\x1b" not in text:
        return text
    return _ANSI_RE.sub("", text)


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
    """True if the pane capture shows a Claude Code rate-limit notice
    **in its most recent output**.

    The match is restricted to the last ``_STALL_TAIL_LINES`` lines
    after ANSI escape stripping, so:

    * A banner the user already resolved but that still lives in
      scrollback above the prompt does NOT re-trigger a nudge on the
      next swap.
    * A colourised banner (``\\x1b[31m...\\x1b[0m``) still matches.
    """
    if not capture:
        return False
    tail = "\n".join(capture.splitlines()[-_STALL_TAIL_LINES:])
    return bool(_STALL_PATTERNS.search(_strip_ansi(tail)))


async def _process_snapshot() -> dict[int, tuple[int, str]]:
    """Return ``{pid: (ppid, comm)}`` for every process visible to the
    current user.

    Single ``ps`` invocation per ``wake_stalled_sessions`` call — cheaper
    than per-pane ``pgrep -P``.  ``comm`` is POSIX — on macOS it is the
    basename of argv[0]; on Linux it is the kernel's task name (from
    ``/proc/<pid>/comm``).  Both platforms return forms that contain
    the substring ``"claude"`` somewhere for a Claude Code process,
    whether invoked as bare ``claude`` or as a full path like
    ``/Users/.../versions/2.1.109`` — verified empirically against the
    maintainer's 22-pane workstation.

    Failure modes fall back to an empty dict, which makes every pane
    look "no descendants" — the orchestrator then depends on the
    opt-in flag alone.  Safer than crashing a whole poll cycle.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-A", "-o", "pid=,ppid=,comm=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.debug("ps not found — ancestry detection disabled this cycle")
        return {}
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        logger.warning("ps -A timed out — killing subprocess")
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return {}
    if proc.returncode != 0:
        logger.debug("ps -A returncode=%s — snapshot empty", proc.returncode)
        return {}
    snapshot: dict[int, tuple[int, str]] = {}
    for line in stdout.decode(errors="replace").splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        snapshot[pid] = (ppid, parts[2])
    return snapshot


# Depth cap for ``_pane_has_claude_descendant``'s BFS — defends against a
# malformed snapshot with ppid cycles (can happen if ps races an exec).
_ANCESTRY_MAX_DEPTH = 50


def _comm_looks_like_claude(comm: str) -> bool:
    """True when ``comm`` (from ``ps -o comm=``) names a Claude Code process.

    Matches by substring, not prefix, because macOS ps returns two
    shapes for native-installer panes:

    * ``claude`` — short form when invoked via PATH.
    * ``/Users/.../local/share/claude/versions/2.1.109`` — full path
      when argv[0] is the absolute path.

    Both contain ``"claude"``.  A non-claude process would have to be
    deliberately named to match; the ancestry walk only inspects
    descendants of tmux pane shells, which narrows the attack surface
    to processes the user themselves spawned under that shell.
    """
    return "claude" in comm.lower()


def _pane_has_claude_descendant(
    pane_pid: int | None,
    snapshot: dict[int, tuple[int, str]],
) -> bool:
    """BFS from ``pane_pid`` through ``snapshot`` to find a Claude Code
    descendant.  Returns ``False`` on missing pid, empty snapshot, or
    cycle detection."""
    if pane_pid is None or not snapshot:
        return False
    children_of: dict[int, list[int]] = {}
    for pid, (ppid, _comm) in snapshot.items():
        children_of.setdefault(ppid, []).append(pid)
    frontier = [pane_pid]
    visited: set[int] = set()
    for _ in range(_ANCESTRY_MAX_DEPTH):
        next_frontier: list[int] = []
        for pid in frontier:
            if pid in visited:
                continue
            visited.add(pid)
            for child in children_of.get(pid, ()):
                if child in visited:
                    continue
                comm = snapshot.get(child, (0, ""))[1]
                if _comm_looks_like_claude(comm):
                    return True
                next_frontier.append(child)
        if not next_frontier:
            return False
        frontier = next_frontier
    return False


async def wake_stalled_sessions(message: str) -> dict:
    """Scan every tmux pane and send ``message`` to each one that is a
    Claude Code session AND shows a rate-limit notice in its recent
    output.

    Detection is two-tiered:

    1. **Opt-in** — pane has ``@ccswitch-nudge`` user option set to
       ``on``.  Bypasses the ancestry walk entirely; precision 100%
       by user declaration.
    2. **Ancestry** — the pane's shell (``pane_pid``) has a descendant
       whose ``comm`` contains ``"claude"``.  Replaces the pre-M2
       ``pane_current_command`` shape-match so the native installer's
       ``argv[0]=2.1.108`` pattern is caught by real process-tree
       membership instead of a fragile string regex.

    Panes with neither signal are skipped WITHOUT calling
    ``capture_pane`` — saves a subprocess and avoids reading the
    scrollback of panes we never had permission to touch.

    Returns a summary dict::

        {
          "scanned": int,
          "nudged":  [target, ...],
          "errors":  [{"target": ..., "error": ...}],
        }
    """
    summary = {"scanned": 0, "nudged": [], "errors": []}
    if not message:
        return summary

    # Defensive length cap — the router validates ALLOWED_KEYS, but
    # defence in depth prevents blasting an arbitrarily long string
    # into every matching pane.
    if len(message) > 256:
        logger.warning(
            "tmux nudge message too long (%d chars) — truncating to 256",
            len(message),
        )
        message = message[:256]

    panes = await list_panes()
    summary["scanned"] = len(panes)

    # Single process snapshot for the whole scan — cheaper than one
    # ``pgrep -P`` per pane and avoids torn views between panes.
    snapshot = await _process_snapshot()

    for pane in panes:
        target = pane.get("target")
        if not target:
            continue

        opt_in = pane.get("opt_in", False)
        has_claude_descendant = _pane_has_claude_descendant(
            pane.get("pid"), snapshot
        )

        if not (opt_in or has_claude_descendant):
            logger.debug(
                "tmux nudge: skipping %s — no claude descendant, no opt-in "
                "(cmd=%r pid=%r)",
                target, pane.get("command"), pane.get("pid"),
            )
            continue

        try:
            capture = await capture_pane(target)
            if not looks_stalled(capture):
                continue
            await send_keys(target, message, press_enter=True)
            summary["nudged"].append(target)
            reason = "opt-in" if opt_in else "ancestry"
            logger.info("tmux nudge sent to %s (%s)", target, reason)
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
