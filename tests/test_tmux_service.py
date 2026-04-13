import pytest
from unittest.mock import patch, MagicMock

def test_list_panes_parses_output():
    from backend.services.tmux_service import list_panes
    output = "main:0.0 claude\nwork:1.0 bash\n"
    with patch("subprocess.run", return_value=MagicMock(stdout=output, returncode=0)):
        panes = list_panes()
    assert len(panes) == 2
    assert panes[0]["target"] == "main:0.0"
    assert panes[0]["command"] == "claude"

def test_list_panes_returns_empty_on_no_tmux():
    from backend.services.tmux_service import list_panes
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        panes = list_panes()
    assert panes == []

def test_send_continue():
    from backend.services.tmux_service import send_continue
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        send_continue("main:0.0")
    args = mock_run.call_args[0][0]
    assert "send-keys" in args
    assert "main:0.0" in args
    assert "continue" in args

def test_capture_pane():
    from backend.services.tmux_service import capture_pane
    with patch("subprocess.run", return_value=MagicMock(stdout="some output\n", returncode=0)):
        result = capture_pane("main:0.0")
    assert result == "some output\n"

@pytest.mark.asyncio
async def test_evaluate_with_haiku_success():
    from backend.services.tmux_service import evaluate_with_haiku
    from unittest.mock import AsyncMock
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"SUCCESS The session continued normally.", b""))
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc), \
         patch("asyncio.wait_for", new_callable=AsyncMock, return_value=(b"SUCCESS The session continued normally.", b"")):
        result = await evaluate_with_haiku("some terminal output", "claude-haiku-4-5-20251001")
    assert result["status"] == "SUCCESS"

@pytest.mark.asyncio
async def test_evaluate_with_haiku_defaults_uncertain():
    from backend.services.tmux_service import evaluate_with_haiku
    from unittest.mock import AsyncMock
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"Something happened.", b""))
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc), \
         patch("asyncio.wait_for", new_callable=AsyncMock, return_value=(b"Something happened.", b"")):
        result = await evaluate_with_haiku("output", "model")
    assert result["status"] == "UNCERTAIN"
