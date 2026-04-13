"""
Integration-ish tests for account_service.activate_account_config.

These tests exercise the path that was previously broken: when a user switches
accounts in the UI, new `claude` invocations (no CLAUDE_CONFIG_DIR set) must
read the new account's oauthAccount from $HOME/.claude.json.

All Keychain operations are monkey-patched so tests run hermetically on Linux
and CI.
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point $HOME at a temp dir and clear CLAUDE_CONFIG_DIR so active_config_file()
    resolves to <tmp>/.claude.json."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


@pytest.fixture
def fake_keychain(monkeypatch):
    """Replace the keychain-touching subprocess calls with an in-memory dict."""
    # service_name -> credentials_json_str
    store: dict[str, str] = {}
    # Calls made against `security delete-generic-password -s X -a Y`.
    deletions: list[tuple[str, str]] = []

    def fake_read(config_dir: str) -> dict:
        from backend.services.credential_provider import _keychain_service_name
        raw = store.get(_keychain_service_name(config_dir))
        return json.loads(raw) if raw else {}

    def fake_write(credentials: dict, service: str) -> bool:
        store[service] = json.dumps(credentials)
        return True

    # Stale-entry cleanup calls subprocess.run([...delete-generic-password...])
    # directly; intercept with a wrapper that records but doesn't touch the real
    # keychain. Any OTHER subprocess call still goes through (we're not in tmux
    # tests here, so none expected).
    import subprocess
    real_run = subprocess.run

    def fake_run(argv, *a, **kw):
        if isinstance(argv, list) and argv[:2] == ["security", "delete-generic-password"]:
            # argv is like ["security","delete-generic-password","-s",svc,"-a",acct]
            svc = argv[argv.index("-s") + 1] if "-s" in argv else ""
            acct = argv[argv.index("-a") + 1] if "-a" in argv else ""
            deletions.append((svc, acct))
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        return real_run(argv, *a, **kw)

    monkeypatch.setattr("backend.services.account_service._read_keychain_credentials", fake_read)
    monkeypatch.setattr("backend.services.account_service._write_keychain_credentials", fake_write)
    monkeypatch.setattr("backend.services.credential_provider._read_keychain_credentials", fake_read)
    monkeypatch.setattr("backend.services.credential_provider._write_keychain_credentials", fake_write)
    monkeypatch.setattr("subprocess.run", fake_run)
    return {"store": store, "deletions": deletions}


def make_account_dir(
    base: Path,
    name: str,
    email: str,
    access_token: str,
    fake_keychain: dict,
    extra_keys: dict | None = None,
) -> Path:
    """Create a fake account config dir with .claude.json + primed keychain."""
    d = base / f"account-{name}"
    d.mkdir(parents=True, exist_ok=True)
    claude_json = {
        "oauthAccount": {
            "emailAddress": email,
            "accountUuid": f"uuid-{name}",
            "organizationUuid": f"org-{name}",
            "organizationName": f"{email}'s Organization",
        },
        "userID": f"userid-{name}",
    }
    if extra_keys:
        claude_json.update(extra_keys)
    (d / ".claude.json").write_text(json.dumps(claude_json))
    # Prime the fake keychain with this dir's credentials
    from backend.services.credential_provider import _keychain_service_name
    fake_keychain["store"][_keychain_service_name(str(d))] = json.dumps({
        "claudeAiOauth": {
            "accessToken": access_token,
            "refreshToken": f"refresh-{name}",
            "expiresAt": 9999999999999,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
        }
    })
    return d


# ── Tests ───────────────────────────────────────────────────────────────────


def test_activate_merges_oauth_into_home(fake_home, fake_keychain):
    """After activate_account_config, $HOME/.claude.json has the target's
    oauthAccount merged in, preserving any pre-existing home keys."""
    from backend.services import account_service as ac

    # Pre-populate $HOME/.claude.json with unrelated state we must preserve.
    home_json = fake_home / ".claude.json"
    home_json.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "old@x.com"},
        "projects": {"/some/project": {"history": ["hi"]}},
        "autoUpdates": True,
        "someOtherKey": "stays",
    }))

    acct_dir = make_account_dir(fake_home, "a", "new@x.com", "tok-a", fake_keychain)

    ac.activate_account_config(str(acct_dir))

    # Home file was updated with the new oauthAccount…
    home = json.loads(home_json.read_text())
    assert home["oauthAccount"]["emailAddress"] == "new@x.com"
    assert home["userID"] == "userid-a"
    # …and unrelated keys were preserved.
    assert home["projects"] == {"/some/project": {"history": ["hi"]}}
    assert home["autoUpdates"] is True
    assert home["someOtherKey"] == "stays"

    # get_active_email now reflects the new account
    assert ac.get_active_email() == "new@x.com"

    # Legacy keychain entry was updated with the target's token
    legacy = json.loads(fake_keychain["store"]["Claude Code-credentials"])
    assert legacy["claudeAiOauth"]["accessToken"] == "tok-a"


def test_switch_between_two_accounts_roundtrip(fake_home, fake_keychain):
    """Switching A → B → A updates email and token each time."""
    from backend.services import account_service as ac

    a = make_account_dir(fake_home, "a", "a@x.com", "tok-a", fake_keychain)
    b = make_account_dir(fake_home, "b", "b@x.com", "tok-b", fake_keychain)

    ac.activate_account_config(str(a))
    assert ac.get_active_email() == "a@x.com"
    assert json.loads(fake_keychain["store"]["Claude Code-credentials"])["claudeAiOauth"]["accessToken"] == "tok-a"

    ac.activate_account_config(str(b))
    assert ac.get_active_email() == "b@x.com"
    assert json.loads(fake_keychain["store"]["Claude Code-credentials"])["claudeAiOauth"]["accessToken"] == "tok-b"

    ac.activate_account_config(str(a))
    assert ac.get_active_email() == "a@x.com"
    assert json.loads(fake_keychain["store"]["Claude Code-credentials"])["claudeAiOauth"]["accessToken"] == "tok-a"


def test_switch_writes_back_home_state_to_previous_account(fake_home, fake_keychain):
    """
    User runs `claude` without CLAUDE_CONFIG_DIR while account A is active.
    Claude writes new project state to $HOME/.claude.json. When we switch to
    B, that new state should be written back to A's isolated dir so it isn't
    lost next time A is activated.
    """
    from backend.services import account_service as ac

    a = make_account_dir(fake_home, "a", "a@x.com", "tok-a", fake_keychain)
    b = make_account_dir(fake_home, "b", "b@x.com", "tok-b", fake_keychain)

    ac.activate_account_config(str(a))

    # Simulate Claude Code appending a project while A is active.
    home_json = fake_home / ".claude.json"
    home = json.loads(home_json.read_text())
    home["projects"] = {"/work/proj": {"opened": True}}
    home_json.write_text(json.dumps(home))

    ac.activate_account_config(str(b))

    # A's dir should now contain the project entry (writeback)
    a_json = json.loads((a / ".claude.json").read_text())
    assert a_json["projects"] == {"/work/proj": {"opened": True}}

    # And B's oauthAccount is now in home
    assert ac.get_active_email() == "b@x.com"


def test_activate_refuses_target_without_oauth(fake_home, fake_keychain, caplog):
    """If the target account dir has no oauthAccount, activation raises
    ValueError (rather than silently wiping home state)."""
    from backend.services import account_service as ac

    # Pre-existing home state
    home_json = fake_home / ".claude.json"
    home_json.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "existing@x.com"},
        "userID": "existing-uid",
    }))

    bad = fake_home / "account-bad"
    bad.mkdir()
    (bad / ".claude.json").write_text(json.dumps({"someUnrelatedKey": 1}))

    with pytest.raises(ValueError, match="no oauthAccount"):
        ac.activate_account_config(str(bad))

    # Home unchanged
    assert ac.get_active_email() == "existing@x.com"


def test_stale_claude_code_keychain_entries_are_cleaned(fake_home, fake_keychain):
    """After activate_account_config, any legacy 'Claude Code-credentials' entries
    for accounts other than $USER are scheduled for deletion."""
    from backend.services import account_service as ac

    a = make_account_dir(fake_home, "a", "a@x.com", "tok-a", fake_keychain)
    ac.activate_account_config(str(a))

    # The stale-cleanup call should have attempted to delete the known-bad
    # legacy account strings against 'Claude Code-credentials'.
    deletions = fake_keychain["deletions"]
    assert any(svc == "Claude Code-credentials" and acct == "claude-code"
               for svc, acct in deletions)
    assert any(svc == "Claude Code-credentials" and acct == "claude-code-user"
               for svc, acct in deletions)


def test_active_dir_pointer_updated(fake_home, fake_keychain):
    """activate_account_config writes the target dir to ~/.claude-multi/active
    so the shell-profile snippet picks it up in new terminals."""
    from backend.services import account_service as ac

    a = make_account_dir(fake_home, "a", "a@x.com", "tok-a", fake_keychain)
    ac.activate_account_config(str(a))

    ptr = fake_home / ".claude-multi" / "active"
    assert ptr.exists()
    assert ptr.read_text().strip() == str(a)


def test_get_active_email_falls_back_to_legacy_location(fake_home, fake_keychain):
    """If $HOME/.claude.json is missing but $HOME/.claude/.claude.json is
    present from an older install, get_active_email still works."""
    from backend.services import account_service as ac

    # No $HOME/.claude.json, but legacy dir file exists
    legacy_dir = fake_home / ".claude"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "legacy@x.com"}
    }))

    # Redirect settings.active_claude_dir to the fake dir
    with patch("backend.services.account_service.active_claude_dir",
               return_value=str(legacy_dir)):
        assert ac.get_active_email() == "legacy@x.com"


def test_activate_keychain_write_failure_does_not_advance_pointer(fake_home, fake_keychain, monkeypatch):
    """If the Keychain write raises during activate_account_config, the
    ~/.claude-multi/active pointer must NOT be updated to the target account.

    Steps 1–2 (writeback + oauthAccount merge) complete, but step 3
    (_write_keychain_credentials) raises RuntimeError.  The active-dir pointer
    (step 6) must stay pointing at the *previous* account — or be absent —
    so the system is not left advertising a partially-installed account.
    """
    from backend.services import account_service as ac

    # Create two account dirs: 'prev' is currently active, 'new' is the target.
    prev = make_account_dir(fake_home, "prev", "prev@x.com", "tok-prev", fake_keychain)
    new  = make_account_dir(fake_home, "new",  "new@x.com",  "tok-new",  fake_keychain)

    # Activate the 'prev' account so the pointer file exists and points at it.
    ac.activate_account_config(str(prev))
    ptr_path = fake_home / ".claude-multi" / "active"
    assert ptr_path.read_text().strip() == str(prev)

    # Now make the Keychain write fail for ANY write.
    def failing_write(credentials: dict, service: str) -> bool:
        raise RuntimeError("keychain write failed")

    monkeypatch.setattr(
        "backend.services.account_service._write_keychain_credentials",
        failing_write,
    )

    # activate_account_config raises (because _write_keychain_credentials raises).
    # The exact propagation path: step 3 raises → no try/except wraps it →
    # the exception escapes activate_account_config before step 6 runs.
    # If the implementation swallows the error (logs a warning instead of
    # raising), the pointer is still not supposed to advance because step 6
    # is only reached when credential operations succeed.
    try:
        ac.activate_account_config(str(new))
    except Exception:
        pass  # An exception is acceptable (and expected in the raise path)

    # Regardless of whether an exception was raised or swallowed, the active
    # pointer must NOT have been advanced to the new account.
    current_ptr = ptr_path.read_text().strip() if ptr_path.exists() else None
    assert current_ptr != str(new), (
        "active pointer was advanced to the new account despite a Keychain write failure"
    )
