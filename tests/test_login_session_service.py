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


def test_verify_login_session_leaves_session_in_registry_on_success(
    fake_tmux, tmpdir_home, monkeypatch
):
    """verify_login_session must NOT pop the session on success.

    Regression guard for a bug where popping made the subsequent
    ``cleanup_login_session`` call a silent no-op — which meant the
    tmux window, scratch dir, and hashed Keychain entry all leaked
    on every successful login.
    """
    info = ls.start_login_session()
    sid = info["session_id"]
    scratch = ls._active_login_sessions[sid]["scratch_dir"]
    with open(os.path.join(scratch, ".claude.json"), "w") as f:
        json.dump({
            "oauthAccount": {"emailAddress": "alice@example.com"},
            "userID": "uid-alice",
        }, f)
    monkeypatch.setattr(
        cp, "read_login_scratch",
        lambda path: {"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt"}},
    )

    result = ls.verify_login_session(sid)
    assert result["success"] is True

    # The session must still be in the registry so cleanup_login_session
    # can find it and run the teardown (kill-window, rmtree, keychain).
    assert sid in ls._active_login_sessions

    # Now run cleanup and verify the session is gone + teardown happened.
    delete_calls: list[str] = []
    monkeypatch.setattr(
        cp, "delete_login_scratch",
        lambda path: delete_calls.append(path),
    )
    ls.cleanup_login_session(sid)
    assert sid not in ls._active_login_sessions
    assert delete_calls == [scratch]
    assert not os.path.isdir(scratch)
    kill_window_calls = [
        c for c in fake_tmux if len(c) >= 2 and c[1] == "kill-window"
    ]
    assert kill_window_calls, "tmux kill-window was not invoked by cleanup"


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
    # Error must specifically explain that login wasn't detected yet.
    assert "Login not detected" in (result.get("error") or "")


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
    assert "Credentials not found" in (result.get("error") or "")


# ── start_relogin_session ─────────────────────────────────────────────────


def test_start_relogin_retry_cleans_up_orphan_session(fake_tmux, tmpdir_home):
    """Clicking Re-login a second time for the same email (after the first
    attempt was orphaned by a closed tab / browser crash) must tear down
    the stale session — including the tmux window — and start fresh, not
    reject with "already active"."""
    first = ls.start_relogin_session("alice@example.com")
    first_sid = first["session_id"]
    assert first_sid in ls._active_login_sessions

    # Second call for the same email — previously raised ValueError;
    # now reclaims the orphan.
    second = ls.start_relogin_session("alice@example.com")
    second_sid = second["session_id"]

    assert second_sid != first_sid, "new session id expected"
    assert first_sid not in ls._active_login_sessions, "orphan not cleaned up"
    assert second_sid in ls._active_login_sessions

    # The orphan cleanup fired tmux kill-window on the first session's pane.
    kill_calls = [c for c in fake_tmux if len(c) >= 2 and c[1] == "kill-window"]
    assert kill_calls, "tmux kill-window was not invoked for orphan session"


def test_start_relogin_for_different_email_does_not_reclaim(fake_tmux, tmpdir_home):
    """An existing re-login session for a DIFFERENT email must stay
    untouched when a new re-login for another email starts."""
    first = ls.start_relogin_session("alice@example.com")
    second = ls.start_relogin_session("bob@example.com")

    assert first["session_id"] in ls._active_login_sessions
    assert second["session_id"] in ls._active_login_sessions
    assert first["session_id"] != second["session_id"]


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


# ── cleanup_orphan_login_artifacts ────────────────────────────────────────


def test_cleanup_orphan_login_artifacts_kills_windows_and_wipes_scratch(
    tmpdir_home, monkeypatch
):
    """On startup, orphan tmux windows named 'add-acct' inside the ccswitch
    session must be killed and orphan scratch dirs under
    $TMPDIR/ccswitch-login/session-* must be deleted — even though the
    in-memory session registry is empty (prior-process artifacts)."""
    # Pre-seed two orphan scratch dirs on disk.
    base = os.path.join(str(tmpdir_home), "ccswitch-login")
    os.makedirs(base, mode=0o700, exist_ok=True)
    orphan_a = os.path.join(base, "session-abc123")
    orphan_b = os.path.join(base, "session-def456")
    os.makedirs(orphan_a, mode=0o700)
    os.makedirs(orphan_b, mode=0o700)
    # A non-matching sibling must NOT be touched.
    untouched = os.path.join(base, "keep-me")
    os.makedirs(untouched, mode=0o700)

    # Mock tmux: list-windows returns two login windows + one unrelated.
    tmux_calls: list[list[str]] = []

    class _Fake:
        def __init__(self, rc=0, stdout=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, *args, **kwargs):
        tmux_calls.append(list(cmd))
        if len(cmd) >= 2 and cmd[1] == "list-windows":
            return _Fake(rc=0, stdout=(
                "@42|add-acct\n"
                "@43|status-bar\n"
                "@44|add-acct\n"
            ))
        return _Fake()

    monkeypatch.setattr(ls.subprocess, "run", fake_run)

    delete_scratch_calls: list[str] = []
    monkeypatch.setattr(
        cp, "delete_login_scratch",
        lambda p: delete_scratch_calls.append(p),
    )

    ls.cleanup_orphan_login_artifacts()

    # Killed both login windows — not the status-bar one.
    kill_calls = [c for c in tmux_calls if len(c) >= 2 and c[1] == "kill-window"]
    killed_ids = [c[3] for c in kill_calls if len(c) >= 4]
    assert "@42" in killed_ids
    assert "@44" in killed_ids
    assert "@43" not in killed_ids

    # Both scratch dirs wiped, sibling preserved.
    assert not os.path.isdir(orphan_a)
    assert not os.path.isdir(orphan_b)
    assert os.path.isdir(untouched)

    # Keychain hashed-entry delete attempted for each scratch dir.
    assert orphan_a in delete_scratch_calls
    assert orphan_b in delete_scratch_calls


def test_cleanup_orphan_login_artifacts_no_base_dir_is_noop(tmpdir_home, monkeypatch):
    """Fresh install with no scratch dir yet → function is a harmless no-op
    after the tmux sweep."""
    monkeypatch.setattr(ls.subprocess, "run", lambda *a, **kw: type("R", (), {
        "returncode": 1, "stdout": "", "stderr": ""
    })())
    # No base dir yet under tmpdir_home.
    assert not os.path.isdir(os.path.join(str(tmpdir_home), "ccswitch-login"))
    ls.cleanup_orphan_login_artifacts()  # must not raise


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
