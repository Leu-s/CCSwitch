"""
Tests for backend.services.tmux_service.

The user-managed monitor list was deleted; the only surviving "smart" path
is ``wake_stalled_sessions(message)`` which scans every pane and nudges the
ones whose recent output looks like a Claude Code rate-limit notice.
"""
import pytest
from unittest.mock import patch, AsyncMock


@pytest.mark.asyncio
async def test_list_panes_parses_output():
    from backend.services.tmux_service import list_panes
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(
        b"main:0.0\t12345\tclaude\ton\nwork:1.0\t67890\tbash\t\n", b"",
    ))
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        panes = await list_panes()
    assert len(panes) == 2
    assert panes[0] == {
        "target": "main:0.0", "pid": 12345, "command": "claude", "opt_in": True,
    }
    assert panes[1] == {
        "target": "work:1.0", "pid": 67890, "command": "bash", "opt_in": False,
    }


@pytest.mark.asyncio
async def test_list_panes_handles_missing_pid():
    """Very old tmux that doesn't populate pane_pid leaves the slot empty.
    Parser must yield ``pid=None`` instead of crashing."""
    from backend.services.tmux_service import list_panes
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"main:0.0\t\tclaude\t\n", b""))
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        panes = await list_panes()
    assert panes[0]["pid"] is None
    assert panes[0]["command"] == "claude"
    assert panes[0]["opt_in"] is False


@pytest.mark.asyncio
async def test_list_panes_opt_in_flag_parsing():
    """``@ccswitch-nudge`` is truthy ONLY when the raw value reads ``on``
    (case-insensitive, whitespace-trimmed).  Every other shape — empty,
    ``off``, ``true``, ``1`` — parses as opt-out."""
    from backend.services.tmux_service import list_panes
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(
        b"a:0.0\t1\tclaude\ton\n"
        b"b:0.0\t2\tclaude\t ON \n"     # whitespace + caps
        b"c:0.0\t3\tclaude\t\n"         # unset
        b"d:0.0\t4\tclaude\toff\n"
        b"e:0.0\t5\tclaude\ttrue\n"     # truthy-looking but not 'on'
        b"f:0.0\t6\tclaude\t1\n",
        b"",
    ))
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        panes = await list_panes()
    expected = [True, True, False, False, False, False]
    assert [p["opt_in"] for p in panes] == expected


@pytest.mark.asyncio
async def test_list_panes_returns_empty_on_no_tmux():
    from backend.services.tmux_service import list_panes
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
        panes = await list_panes()
    assert panes == []


@pytest.mark.asyncio
async def test_send_keys_sends_literal_then_enter():
    from backend.services.tmux_service import send_keys
    mock_proc = AsyncMock()
    mock_proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        await send_keys("main:0.0", "continue")
    # First call: literal text (-l), second call: Enter key.
    first_call_args = mock_exec.call_args_list[0][0]
    assert "send-keys" in first_call_args
    assert "main:0.0" in first_call_args
    assert "-l" in first_call_args
    assert "continue" in first_call_args
    second_call_args = mock_exec.call_args_list[1][0]
    assert "Enter" in second_call_args


@pytest.mark.asyncio
async def test_capture_pane():
    from backend.services.tmux_service import capture_pane
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"some output\n", b""))
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        result = await capture_pane("main:0.0")
    assert result == "some output\n"


# ── looks_stalled regex matrix ─────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    # Real Claude Code messages from Anthropic GitHub issues + Help Center.
    # Any change here should be backed by an actual observed message, not a guess.
    "Claude AI usage limit reached",                                 # issue #2087
    "Claude AI usage limit reached|1760000400",                      # issue #9046 (with epoch suffix)
    "Claude usage limit reached. Your limit will reset at 3pm",      # issue #9236
    "Claude usage limit reached. Your limit will reset at 2pm (America/New_York)",  # issue #5977
    "⎿ 5-hour limit reached ∙ resets 18:00",                         # issue #6488
    "5-hour limit reached",                                          # issue #6457
    "5-hour limit resets 17:00 - continuing with extra usage",       # extra-usage variant
    "Approaching usage limit (95%)",                                 # Pro tier warning
    "  rate limit exceeded — please wait",
    "RATE_LIMIT_ERROR returned by API",                              # 429 error code
    "This request would exceed your account's rate limit",           # Anthropic API raw
    "Anthropic API Error: Overloaded Error (529)",                   # issue #35487
    "API Error: 529 Overloaded — try again later",                   # issue #35704
    "HTTP 529 Service Overloaded",                                   # issue #35785
    "overloaded_error",                                              # bare API code
])
def test_looks_stalled_matches_known_messages(text):
    from backend.services.tmux_service import looks_stalled
    assert looks_stalled(text)


@pytest.mark.parametrize("text", [
    "",
    "all good here",
    "$ echo hello",
    "build succeeded in 4.2s",
    "Some quote: 'I have no limits' — funny line",  # "limit" but no rate-limit verb
])
def test_looks_stalled_ignores_benign_text(text):
    from backend.services.tmux_service import looks_stalled
    assert not looks_stalled(text)


# ── looks_stalled tail-window + ANSI stripping ─────────────────────────────


def test_looks_stalled_ignores_stale_scrollback_outside_tail_20():
    """A3 regression guard: a rate-limit banner the user already resolved
    and scrolled 30 lines up MUST NOT re-trigger a nudge."""
    from backend.services.tmux_service import looks_stalled
    capture = (
        "Claude AI usage limit reached\n"           # old banner — line 1
        + "\n".join([f"user command {i}" for i in range(30)])  # 30 benign lines
        + "\n"
        + "\n".join(["idle"] * 5)                    # fresh tail
    )
    assert not looks_stalled(capture)


def test_looks_stalled_boundary_exactly_20_lines_matches():
    """Banner on the FIRST line of a 20-line capture (line 20 from the
    tail end) still matches — inclusive boundary."""
    from backend.services.tmux_service import looks_stalled
    capture = "Claude AI usage limit reached\n" + "\n".join(["l"] * 19)
    assert looks_stalled(capture)


def test_looks_stalled_boundary_banner_on_line_21_does_not_match():
    """Banner on line 21 (one line outside the tail-20 window) is
    ignored — exclusive boundary."""
    from backend.services.tmux_service import looks_stalled
    capture = "Claude AI usage limit reached\n" + "\n".join(["l"] * 20)
    assert not looks_stalled(capture)


def test_looks_stalled_matches_on_last_line():
    from backend.services.tmux_service import looks_stalled
    capture = "\n".join(["benign"] * 100) + "\nrate_limit_error"
    assert looks_stalled(capture)


def test_looks_stalled_strips_ansi_csi_before_match():
    """Colourised banner with CSI (SGR) escapes still matches."""
    from backend.services.tmux_service import looks_stalled
    assert looks_stalled("\x1b[31;1mClaude AI usage limit reached\x1b[0m")
    assert looks_stalled("\x1b[38;2;255;0;0m5-hour limit reached\x1b[0m")


def test_looks_stalled_strips_ansi_osc_before_match():
    """OSC sequences (hyperlinks, cwd-notify) do not block the match."""
    from backend.services.tmux_service import looks_stalled
    # OSC-8 hyperlink + BEL terminator
    capture = "\x1b]8;;https://x\x07rate limit exceeded\x1b]8;;\x07"
    assert looks_stalled(capture)


def test_strip_ansi_preserves_plain_text():
    from backend.services.tmux_service import _strip_ansi
    assert _strip_ansi("hello world") == "hello world"
    assert _strip_ansi("\x1b[0m\x1b[31mhello\x1b[0m") == "hello"
    assert _strip_ansi("") == ""


# ── wake_stalled_sessions ──────────────────────────────────────────────────


def _make_panes(*shape):
    """Build a list of pane dicts from 4-tuples (target, pid, command, opt_in)."""
    return [
        {"target": t, "pid": p, "command": c, "opt_in": o}
        for (t, p, c, o) in shape
    ]


@pytest.mark.asyncio
async def test_wake_nudges_only_panes_with_rate_limit_text():
    """Three panes, one has a stall pattern and a claude descendant →
    only that one gets nudged."""
    from backend.services import tmux_service as ts

    panes = _make_panes(
        ("a:0.0", 100, "zsh", False),   # claude descendant at pid 200
        ("b:0.0", 300, "vim", False),   # no claude descendant
        ("c:0.0", 400, "2.1.108", False),  # claude descendant at pid 500
    )
    snapshot = {
        100: (1, "zsh"),
        200: (100, "claude"),
        300: (1, "vim"),
        400: (1, "zsh"),
        500: (400, "/Users/nazarii/.local/share/claude/versions/2.1.108"),
    }
    captures = {
        "a:0.0": "Welcome to Claude Code\n> ",
        "b:0.0": "vim: editing file.py",
        "c:0.0": "Claude AI usage limit reached. Try again at 18:00.",
    }

    nudged: list[tuple[str, str]] = []

    async def fake_capture(target, lines=None):
        return captures.get(target, "")

    async def fake_send(target, text, press_enter=True):
        nudged.append((target, text))

    with patch.object(ts, "list_panes", new=AsyncMock(return_value=panes)), \
         patch.object(ts, "_process_snapshot", new=AsyncMock(return_value=snapshot)), \
         patch.object(ts, "capture_pane", new=fake_capture), \
         patch.object(ts, "send_keys", new=fake_send):
        summary = await ts.wake_stalled_sessions("continue")

    assert summary["scanned"] == 3
    assert summary["nudged"] == ["c:0.0"]
    assert summary["errors"] == []
    assert nudged == [("c:0.0", "continue")]


@pytest.mark.asyncio
async def test_wake_with_empty_message_is_noop():
    from backend.services import tmux_service as ts

    with patch.object(ts, "list_panes", new=AsyncMock(return_value=[{"target": "x:0.0", "pid": 1, "command": "claude", "opt_in": True}])), \
         patch.object(ts, "_process_snapshot", new=AsyncMock(return_value={})), \
         patch.object(ts, "capture_pane", new=AsyncMock(return_value="usage limit reached")), \
         patch.object(ts, "send_keys", new=AsyncMock()) as mock_send:
        summary = await ts.wake_stalled_sessions("")

    mock_send.assert_not_called()
    assert summary["nudged"] == []


@pytest.mark.asyncio
async def test_wake_records_per_pane_errors_without_aborting():
    """If one pane's send fails, the others are still processed."""
    from backend.services import tmux_service as ts

    panes = _make_panes(
        ("good:0.0", 10, "zsh", False),
        ("bad:0.0", 20, "zsh", False),
    )
    snapshot = {
        10: (1, "zsh"), 11: (10, "claude"),
        20: (1, "zsh"), 21: (20, "claude"),
    }
    captures = {"good:0.0": "rate limit reached", "bad:0.0": "rate limit reached"}

    async def fake_capture(target, lines=None):
        return captures[target]

    async def fake_send(target, text, press_enter=True):
        if target == "bad:0.0":
            raise RuntimeError("send failed")

    with patch.object(ts, "list_panes", new=AsyncMock(return_value=panes)), \
         patch.object(ts, "_process_snapshot", new=AsyncMock(return_value=snapshot)), \
         patch.object(ts, "capture_pane", new=fake_capture), \
         patch.object(ts, "send_keys", new=fake_send):
        summary = await ts.wake_stalled_sessions("continue")

    assert "good:0.0" in summary["nudged"]
    assert "bad:0.0" not in summary["nudged"]
    assert any(e["target"] == "bad:0.0" for e in summary["errors"])


@pytest.mark.asyncio
async def test_wake_returns_zero_when_no_panes():
    from backend.services import tmux_service as ts

    with patch.object(ts, "list_panes", new=AsyncMock(return_value=[])), \
         patch.object(ts, "_process_snapshot", new=AsyncMock(return_value={})):
        summary = await ts.wake_stalled_sessions("continue")
    assert summary == {"scanned": 0, "nudged": [], "errors": []}


# ── _comm_looks_like_claude: substring match semantics ────────────────────


def test_comm_looks_like_claude_shapes():
    """Both macOS shapes return True: bare ``claude`` and full-path
    ``/Users/.../versions/2.1.108``.  Non-claude processes return False."""
    from backend.services import tmux_service as ts
    assert ts._comm_looks_like_claude("claude")
    assert ts._comm_looks_like_claude("Claude")  # case-insensitive
    assert ts._comm_looks_like_claude("/opt/homebrew/bin/claude")
    assert ts._comm_looks_like_claude("/Users/x/.local/share/claude/versions/2.1.109")
    assert ts._comm_looks_like_claude("/Applications/Claude Code.app/claude")
    assert not ts._comm_looks_like_claude("2.1.108")
    assert not ts._comm_looks_like_claude("zsh")
    assert not ts._comm_looks_like_claude("bash")
    assert not ts._comm_looks_like_claude("node")
    assert not ts._comm_looks_like_claude("")


# ── _pane_has_claude_descendant: BFS through process snapshot ─────────────


def test_ancestry_immediate_child_is_claude():
    from backend.services import tmux_service as ts
    snap = {100: (1, "zsh"), 200: (100, "claude")}
    assert ts._pane_has_claude_descendant(100, snap)


def test_ancestry_grandchild_through_bash_tool():
    """Claude Code runs a Bash tool → pane_current_command transiently
    shows ``bash``, but claude is still an ancestor of that bash.  The
    walk descends from the shell (pane_pid) — NOT from the foreground
    process — so claude appears as a direct child of the shell."""
    from backend.services import tmux_service as ts
    snap = {
        100: (1, "zsh"),
        200: (100, "claude"),
        300: (200, "bash"),  # Bash tool invocation
    }
    assert ts._pane_has_claude_descendant(100, snap)


def test_ancestry_wrapper_script_zsh_dash_c():
    """User aliased ``cc='zsh -c "claude"'`` — shell forks another shell,
    which execs claude.  Walk must find claude as a grandchild."""
    from backend.services import tmux_service as ts
    snap = {
        100: (1, "zsh"),
        200: (100, "zsh"),      # zsh -c wrapper
        300: (200, "claude"),
    }
    assert ts._pane_has_claude_descendant(100, snap)


def test_ancestry_native_installer_full_path_comm():
    """Native installer ships claude with comm set to the absolute path
    of the versioned binary.  ``comm`` contains the substring ``claude``
    twice — the walk must match it."""
    from backend.services import tmux_service as ts
    snap = {
        100: (1, "zsh"),
        200: (100, "/Users/x/.local/share/claude/versions/2.1.109"),
    }
    assert ts._pane_has_claude_descendant(100, snap)


def test_ancestry_no_claude_descendant_returns_false():
    from backend.services import tmux_service as ts
    snap = {
        100: (1, "zsh"),
        200: (100, "vim"),
        300: (200, "git"),
    }
    assert not ts._pane_has_claude_descendant(100, snap)


def test_ancestry_missing_pid_returns_false():
    from backend.services import tmux_service as ts
    assert not ts._pane_has_claude_descendant(None, {1: (0, "claude")})


def test_ancestry_empty_snapshot_returns_false():
    from backend.services import tmux_service as ts
    assert not ts._pane_has_claude_descendant(100, {})


def test_ancestry_cycle_guard_terminates():
    """A malformed snapshot with ppid cycles (ps racing an exec) must
    not infinite-loop.  visited-set + depth cap both limit traversal."""
    from backend.services import tmux_service as ts
    snap = {
        100: (200, "zsh"),    # 100's parent is 200...
        200: (100, "bash"),   # ... and 200's parent is 100 — cycle
    }
    # Must terminate without claude being found.
    assert not ts._pane_has_claude_descendant(100, snap)


# ── wake integration: opt-in + ancestry gating ────────────────────────────


@pytest.mark.asyncio
async def test_wake_opt_in_bypasses_ancestry_when_stall_present():
    """Opt-in pane with no claude descendant but a stall pattern in the
    capture gets nudged — the user explicitly asked us to treat this
    pane as claude-owned."""
    from backend.services import tmux_service as ts
    panes = _make_panes(("a:0.0", 100, "bash", True))
    snapshot = {100: (1, "bash")}  # no claude anywhere

    nudged: list[str] = []

    async def fake_send(target, text, press_enter=True):
        nudged.append(target)

    with patch.object(ts, "list_panes", new=AsyncMock(return_value=panes)), \
         patch.object(ts, "_process_snapshot", new=AsyncMock(return_value=snapshot)), \
         patch.object(ts, "capture_pane",
                     new=AsyncMock(return_value="usage limit reached")), \
         patch.object(ts, "send_keys", new=fake_send):
        summary = await ts.wake_stalled_sessions("continue")

    assert nudged == ["a:0.0"]
    assert summary["nudged"] == ["a:0.0"]


@pytest.mark.asyncio
async def test_wake_opt_in_without_stall_does_not_nudge():
    """Opt-in bypasses the ancestry gate but NOT the stall gate.  A pane
    that opted in but has no rate-limit text stays quiet."""
    from backend.services import tmux_service as ts
    panes = _make_panes(("a:0.0", 100, "bash", True))

    with patch.object(ts, "list_panes", new=AsyncMock(return_value=panes)), \
         patch.object(ts, "_process_snapshot", new=AsyncMock(return_value={})), \
         patch.object(ts, "capture_pane", new=AsyncMock(return_value="$ echo hi")), \
         patch.object(ts, "send_keys", new=AsyncMock()) as mock_send:
        summary = await ts.wake_stalled_sessions("continue")

    mock_send.assert_not_called()
    assert summary["nudged"] == []


@pytest.mark.asyncio
async def test_wake_skips_non_claude_pane_without_capturing():
    """Invariant 4: a pane with no claude descendant and no opt-in is
    skipped WITHOUT calling capture_pane — saves the subprocess and
    avoids reading scrollback of panes we never had permission for."""
    from backend.services import tmux_service as ts
    panes = _make_panes(("a:0.0", 100, "zsh", False))
    snapshot = {100: (1, "zsh"), 200: (100, "vim")}

    mock_capture = AsyncMock()
    mock_send = AsyncMock()

    with patch.object(ts, "list_panes", new=AsyncMock(return_value=panes)), \
         patch.object(ts, "_process_snapshot", new=AsyncMock(return_value=snapshot)), \
         patch.object(ts, "capture_pane", new=mock_capture), \
         patch.object(ts, "send_keys", new=mock_send):
        await ts.wake_stalled_sessions("continue")

    mock_capture.assert_not_called()
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_process_snapshot_non_zero_exit_returns_empty():
    """Explicit coverage for the non-zero-exit branch in _process_snapshot
    (ps runs but fails).  Without this the graceful-fallback path is only
    exercised via the higher-level monkeypatch in the opt-in fallback test."""
    from backend.services import tmux_service as ts

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"ps: Operation not permitted\n"))
    mock_proc.returncode = 1

    with patch("asyncio.create_subprocess_exec",
               new_callable=AsyncMock, return_value=mock_proc):
        snapshot = await ts._process_snapshot()

    assert snapshot == {}


@pytest.mark.asyncio
async def test_wake_ps_missing_falls_back_to_opt_in_only():
    """If ``ps`` cannot be invoked, ``_process_snapshot`` returns an
    empty dict.  Every ancestry check then returns False — only
    explicitly opted-in panes still get considered."""
    from backend.services import tmux_service as ts
    panes = _make_panes(
        ("a:0.0", 100, "claude", False),  # ancestry-only; skipped when ps=∅
        ("b:0.0", 200, "claude", True),   # opt-in; still nudged
    )

    async def fake_capture(target, lines=None):
        return "rate limit reached"

    nudged: list[str] = []

    async def fake_send(target, text, press_enter=True):
        nudged.append(target)

    with patch.object(ts, "list_panes", new=AsyncMock(return_value=panes)), \
         patch.object(ts, "_process_snapshot", new=AsyncMock(return_value={})), \
         patch.object(ts, "capture_pane", new=fake_capture), \
         patch.object(ts, "send_keys", new=fake_send):
        await ts.wake_stalled_sessions("continue")

    assert nudged == ["b:0.0"]
