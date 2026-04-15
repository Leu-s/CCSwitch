"""
Tests for backend.services.login_session_service.

Covers the start / verify / cleanup lifecycle for both the add-account
flow and the re-login flow.  Nothing in these tests touches real tmux or
real Keychain — ``subprocess.run`` for tmux is monkeypatched, and
``cp.read_login_scratch`` / ``cp.delete_login_scratch`` are monkeypatched
to avoid Keychain dependencies.
"""
import json
import os

import pytest

from backend.services import credential_provider as cp
from backend.services import login_session_service as ls


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_sessions():
    """Reset the module-level session registry between tests."""
    ls._active_login_sessions.clear()
    yield
    ls._active_login_sessions.clear()


@pytest.fixture
def fake_tmux(monkeypatch):
    """Stub out tmux subprocess calls so start_login_session does not
    require a running tmux server.  Records the commands invoked so
    individual tests can assert on them."""
    calls: list[list[str]] = []

    class _Fake:
        def __init__(self, rc=0, stdout=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if len(cmd) >= 2 and cmd[0] == "tmux" and cmd[1] == "list-sessions":
            return _Fake(rc=1, stdout="")
        if len(cmd) >= 2 and cmd[0] == "tmux" and cmd[1] == "new-window":
            return _Fake(rc=0, stdout="ccswitch:99.0")
        return _Fake()

    monkeypatch.setattr(ls.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def tmpdir_home(monkeypatch, tmp_path):
    """Redirect tempfile.gettempdir() to a clean per-test path so scratch
    dirs don't collide with other tests.  We patch the function directly
    because ``tempfile.gettempdir`` caches its result and will not re-read
    $TMPDIR on a simple env-var change."""
    monkeypatch.setattr(ls.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


# ── start_login_session ───────────────────────────────────────────────────


def test_start_login_session_creates_scratch_dir(fake_tmux, tmpdir_home):
    info = ls.start_login_session()
    assert "session_id" in info
    assert "instructions" in info
    assert "config_dir" not in info  # vault-swap architecture drops this field

    # Session is registered internally.
    sid = info["session_id"]
    assert sid in ls._active_login_sessions
    data = ls._active_login_sessions[sid]
    scratch = data["scratch_dir"]
    assert os.path.isdir(scratch)
    assert scratch.startswith(str(tmpdir_home))

    # tmux new-window was invoked with CLAUDE_CONFIG_DIR set.
    new_window_calls = [
        c for c in fake_tmux if len(c) >= 2 and c[1] == "new-window"
    ]
    assert new_window_calls, "tmux new-window was not invoked"
    send_keys_calls = [
        c for c in fake_tmux if len(c) >= 2 and c[1] == "send-keys"
    ]
    # The send-keys payload contains CLAUDE_CONFIG_DIR=<scratch>.
    assert any(
        any(f"CLAUDE_CONFIG_DIR={scratch}" in arg for arg in c)
        for c in send_keys_calls
    )


# ── verify_login_session ──────────────────────────────────────────────────


def test_verify_login_session_success(fake_tmux, tmpdir_home, monkeypatch):
    info = ls.start_login_session()
    sid = info["session_id"]
    scratch = ls._active_login_sessions[sid]["scratch_dir"]

    # Simulate what Claude Code would write on successful login.
    with open(os.path.join(scratch, ".claude.json"), "w") as f:
        json.dump({
            "oauthAccount": {"emailAddress": "alice@example.com"},
            "userID": "uid-alice",
        }, f)

    def fake_read_scratch(path):
        assert path == scratch
        return {"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt"}}

    monkeypatch.setattr(cp, "read_login_scratch", fake_read_scratch)

    result = ls.verify_login_session(sid)
    assert result["success"] is True
    assert result["email"] == "alice@example.com"
    assert result["user_id"] == "uid-alice"
    assert result["oauth_account"] == {"emailAddress": "alice@example.com"}
    assert result["oauth_tokens"]["refreshToken"] == "rt"
    assert result["kind"] == "add"


def test_verify_login_session_missing_email(fake_tmux, tmpdir_home, monkeypatch):
    info = ls.start_login_session()
    sid = info["session_id"]
    scratch = ls._active_login_sessions[sid]["scratch_dir"]

    # Write .claude.json with no oauthAccount — login has not completed yet.
    with open(os.path.join(scratch, ".claude.json"), "w") as f:
        json.dump({"projects": []}, f)

    monkeypatch.setattr(
        cp, "read_login_scratch",
        lambda path: {"claudeAiOauth": {"refreshToken": "rt"}},
    )

    result = ls.verify_login_session(sid)
    assert result["success"] is False
    assert "email" in (result.get("error") or "").lower() or \
        "not found" in (result.get("error") or "").lower() or \
        ".claude.json" in (result.get("error") or "")


def test_verify_login_session_no_refresh_token(fake_tmux, tmpdir_home, monkeypatch):
    info = ls.start_login_session()
    sid = info["session_id"]
    scratch = ls._active_login_sessions[sid]["scratch_dir"]

    with open(os.path.join(scratch, ".claude.json"), "w") as f:
        json.dump({
            "oauthAccount": {"emailAddress": "alice@example.com"},
            "userID": "uid-alice",
        }, f)

    # No refreshToken in the Keychain scratch entry.
    monkeypatch.setattr(
        cp, "read_login_scratch",
        lambda path: {"claudeAiOauth": {"accessToken": "at"}},
    )

    result = ls.verify_login_session(sid)
    assert result["success"] is False
    assert "credentials" in (result.get("error") or "").lower() or \
        "refresh" in (result.get("error") or "").lower() or \
        "login" in (result.get("error") or "").lower()


# ── start_relogin_session ─────────────────────────────────────────────────


def test_start_relogin_rejects_duplicate_for_same_email(fake_tmux, tmpdir_home):
    ls.start_relogin_session("alice@example.com")
    with pytest.raises(ValueError) as excinfo:
        ls.start_relogin_session("alice@example.com")
    assert "already active" in str(excinfo.value).lower() or \
        "re-login" in str(excinfo.value).lower()


# ── cleanup_login_session ────────────────────────────────────────────────


def test_cleanup_login_session_deletes_scratch_and_keychain(
    fake_tmux, tmpdir_home, monkeypatch
):
    info = ls.start_login_session()
    sid = info["session_id"]
    scratch = ls._active_login_sessions[sid]["scratch_dir"]
    assert os.path.isdir(scratch)

    delete_calls: list[str] = []

    def fake_delete(path):
        delete_calls.append(path)

    monkeypatch.setattr(cp, "delete_login_scratch", fake_delete)

    ls.cleanup_login_session(sid)

    # Keychain delete invoked with the scratch path.
    assert delete_calls == [scratch]
    # Scratch dir removed from disk.
    assert not os.path.isdir(scratch)
    # Session registry entry popped.
    assert sid not in ls._active_login_sessions
    # tmux kill-window called with the pane target (fake_tmux recorded it).
    kill_window_calls = [
        c for c in fake_tmux if len(c) >= 2 and c[1] == "kill-window"
    ]
    assert kill_window_calls, "tmux kill-window was not invoked"
    assert any("ccswitch:99.0" in c for c in kill_window_calls)


def test_open_claude_tmux_window_targets_ccswitch_session(fake_tmux, tmpdir_home):
    """The login window must be created inside the ccswitch session, not
    whatever session the user is currently attached to."""
    ls.start_login_session()
    new_window_calls = [
        c for c in fake_tmux if len(c) >= 2 and c[1] == "new-window"
    ]
    assert new_window_calls, "tmux new-window was not invoked"
    # Every new-window call must carry ``-t <session_name>``.
    for call in new_window_calls:
        assert "-t" in call, f"missing -t in {call}"
        idx = call.index("-t")
        assert call[idx + 1] == "ccswitch", f"-t target was {call[idx + 1]!r}"
