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
    mock_proc.communicate = AsyncMock(return_value=(b"main:0.0 claude\nwork:1.0 bash\n", b""))
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        panes = await list_panes()
    assert len(panes) == 2
    assert panes[0]["target"] == "main:0.0"
    assert panes[0]["command"] == "claude"


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
    "Claude AI usage limit reached",
    "Claude usage limit reached. Try again at 18:00.",
    "  rate limit exceeded — please wait",
    "RATE_LIMIT_ERROR returned by API",
    "Approaching usage limit (95%)",
    "API Error: Overloaded — try again later",
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


# ── wake_stalled_sessions ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wake_nudges_only_panes_with_rate_limit_text():
    """Of three panes, only the one whose capture matches a rate-limit
    pattern should receive the message."""
    from backend.services import tmux_service as ts

    panes = [
        {"target": "a:0.0", "command": "claude"},
        {"target": "b:0.0", "command": "vim"},
        {"target": "c:0.0", "command": "claude"},
    ]
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

    with patch.object(ts, "list_panes", new=AsyncMock(return_value=[{"target": "x:0.0"}])), \
         patch.object(ts, "capture_pane", new=AsyncMock(return_value="usage limit reached")), \
         patch.object(ts, "send_keys", new=AsyncMock()) as mock_send:
        summary = await ts.wake_stalled_sessions("")

    mock_send.assert_not_called()
    assert summary["nudged"] == []


@pytest.mark.asyncio
async def test_wake_records_per_pane_errors_without_aborting():
    """If one pane's capture / send fails, the others are still processed."""
    from backend.services import tmux_service as ts

    panes = [
        {"target": "good:0.0", "command": "claude"},
        {"target": "bad:0.0", "command": "claude"},
    ]
    captures = {
        "good:0.0": "rate limit reached",
        "bad:0.0": "rate limit reached",
    }

    async def fake_capture(target, lines=None):
        return captures[target]

    async def fake_send(target, text, press_enter=True):
        if target == "bad:0.0":
            raise RuntimeError("send failed")

    with patch.object(ts, "list_panes", new=AsyncMock(return_value=panes)), \
         patch.object(ts, "capture_pane", new=fake_capture), \
         patch.object(ts, "send_keys", new=fake_send):
        summary = await ts.wake_stalled_sessions("continue")

    assert "good:0.0" in summary["nudged"]
    assert "bad:0.0" not in summary["nudged"]
    assert any(e["target"] == "bad:0.0" for e in summary["errors"])


@pytest.mark.asyncio
async def test_wake_returns_zero_when_no_panes():
    from backend.services import tmux_service as ts

    with patch.object(ts, "list_panes", new=AsyncMock(return_value=[])):
        summary = await ts.wake_stalled_sessions("continue")
    assert summary == {"scanned": 0, "nudged": [], "errors": []}
