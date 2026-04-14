"""
Integration-ish tests for account_service.activate_account_config under the
credential-targets model.

The function now takes an explicit list of enabled target paths.  With an
empty list, nothing outside the isolated account dir is touched.  With the
HOME ``.claude.json`` included, the legacy Keychain and ``~/.claude/``
plaintext fallback are also updated — matching what a fresh ``claude`` run
reads when CLAUDE_CONFIG_DIR is unset.

All Keychain operations are monkey-patched so tests run hermetically on Linux
and CI.
"""
import json
import os
from pathlib import Path

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
    store: dict[str, str] = {}
    deletions: list[tuple[str, str]] = []

    def fake_read(config_dir: str) -> dict:
        from backend.services.credential_provider import _keychain_service_name
        raw = store.get(_keychain_service_name(config_dir))
        return json.loads(raw) if raw else {}

    def fake_write(credentials: dict, service: str) -> bool:
        store[service] = json.dumps(credentials)
        return True

    import subprocess
    real_run = subprocess.run

    def fake_run(argv, *a, **kw):
        if isinstance(argv, list) and argv[:2] == ["security", "delete-generic-password"]:
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


def _home_canonical(fake_home: Path) -> str:
    """Canonical path of $HOME/.claude.json used as a credential target."""
    return os.path.realpath(str(fake_home / ".claude.json"))


# ── Tests: HOME target enabled (most common configuration) ─────────────────


def test_activate_with_home_target_merges_oauth_into_home(fake_home, fake_keychain):
    """With HOME .claude.json in enabled_targets, activate mirrors the
    target's oauthAccount into HOME while preserving unrelated keys."""
    from backend.services import account_service as ac

    home_json = fake_home / ".claude.json"
    home_json.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "old@x.com"},
        "projects": {"/some/project": {"history": ["hi"]}},
        "autoUpdates": True,
        "someOtherKey": "stays",
    }))

    acct_dir = make_account_dir(fake_home, "a", "new@x.com", "tok-a", fake_keychain)

    summary = ac.activate_account_config(str(acct_dir), [_home_canonical(fake_home)])

    home = json.loads(home_json.read_text())
    assert home["oauthAccount"]["emailAddress"] == "new@x.com"
    assert home["userID"] == "userid-a"
    assert home["projects"] == {"/some/project": {"history": ["hi"]}}
    assert home["autoUpdates"] is True
    assert home["someOtherKey"] == "stays"

    assert ac.get_active_email() == "new@x.com"

    legacy = json.loads(fake_keychain["store"]["Claude Code-credentials"])
    assert legacy["claudeAiOauth"]["accessToken"] == "tok-a"

    assert summary["system_default_enabled"] is True
    assert summary["keychain_written"] is True
    assert _home_canonical(fake_home) in summary["mirror"]["written"]


def test_switch_between_two_accounts_with_home_target(fake_home, fake_keychain):
    """Switching A → B → A updates email and token each time when HOME is a target."""
    from backend.services import account_service as ac

    a = make_account_dir(fake_home, "a", "a@x.com", "tok-a", fake_keychain)
    b = make_account_dir(fake_home, "b", "b@x.com", "tok-b", fake_keychain)
    targets = [_home_canonical(fake_home)]

    ac.activate_account_config(str(a), targets)
    assert ac.get_active_email() == "a@x.com"
    assert json.loads(fake_keychain["store"]["Claude Code-credentials"])["claudeAiOauth"]["accessToken"] == "tok-a"

    ac.activate_account_config(str(b), targets)
    assert ac.get_active_email() == "b@x.com"
    assert json.loads(fake_keychain["store"]["Claude Code-credentials"])["claudeAiOauth"]["accessToken"] == "tok-b"

    ac.activate_account_config(str(a), targets)
    assert ac.get_active_email() == "a@x.com"
    assert json.loads(fake_keychain["store"]["Claude Code-credentials"])["claudeAiOauth"]["accessToken"] == "tok-a"


def test_activate_missing_oauth_returns_error_in_summary(fake_home, fake_keychain):
    """If the target has no oauthAccount, the mirror summary reports an error
    but activation still advances the pointer (the isolated dir is still the
    account — it's the target file that cannot receive a merge)."""
    from backend.services import account_service as ac

    home_json = fake_home / ".claude.json"
    home_json.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "existing@x.com"},
        "userID": "existing-uid",
    }))

    bad = fake_home / "account-bad"
    bad.mkdir()
    (bad / ".claude.json").write_text(json.dumps({"someUnrelatedKey": 1}))

    summary = ac.activate_account_config(str(bad), [_home_canonical(fake_home)])

    # Mirror summary surfaces the error.
    assert summary["mirror"]["errors"], "missing oauthAccount should surface an error"
    assert any("oauthAccount" in e for e in summary["mirror"]["errors"])

    # HOME was NOT touched because the source had no oauthAccount — mirror_oauth_into_targets
    # refuses to write anything when the source is unusable.
    home = json.loads(home_json.read_text())
    assert home["oauthAccount"]["emailAddress"] == "existing@x.com"


def test_stale_keychain_entries_cleaned_when_home_target_enabled(fake_home, fake_keychain):
    """Stale-cleanup runs only when a system-default target is enabled."""
    from backend.services import account_service as ac

    a = make_account_dir(fake_home, "a", "a@x.com", "tok-a", fake_keychain)
    ac.activate_account_config(str(a), [_home_canonical(fake_home)])

    deletions = fake_keychain["deletions"]
    assert any(svc == "Claude Code-credentials" and acct == "claude-code"
               for svc, acct in deletions)
    assert any(svc == "Claude Code-credentials" and acct == "claude-code-user"
               for svc, acct in deletions)


def test_keychain_write_failure_does_not_advance_pointer(fake_home, fake_keychain, monkeypatch):
    """If the plaintext .credentials.json copy raises during activation,
    the pointer must NOT advance to the new target."""
    from backend.services import account_service as ac

    prev = make_account_dir(fake_home, "prev", "prev@x.com", "tok-prev", fake_keychain)
    new = make_account_dir(fake_home, "new", "new@x.com", "tok-new", fake_keychain)
    # Plant a .credentials.json in the new dir so the copy step runs.
    (new / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-new"}}))

    targets = [_home_canonical(fake_home)]
    ac.activate_account_config(str(prev), targets)
    ptr_path = fake_home / ".ccswitch" / "active"
    assert ptr_path.read_text().strip() == str(prev)

    # Force shutil.copy2 to raise during the plaintext copy step.
    def failing_copy(src, dst, *a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("backend.services.account_service.shutil.copy2", failing_copy)

    with pytest.raises(RuntimeError, match="disk full"):
        ac.activate_account_config(str(new), targets)

    # Pointer must still be the previous account — step 3 is never reached.
    assert ptr_path.read_text().strip() == str(prev)


def test_copy_failure_leaves_mirror_identity_untouched(fake_home, fake_keychain, monkeypatch):
    """Fix C2 invariant: when the ``.credentials.json`` copy step raises,
    HOME ``.claude.json`` must still hold the PREVIOUS account's identity —
    the reorder establishes that the mirror write only runs after the
    failure-prone copy succeeds, so a disk-full error cannot leave the dash
    in a half-switched split-brain state."""
    from backend.services import account_service as ac

    prev = make_account_dir(fake_home, "prev", "prev@x.com", "tok-prev", fake_keychain)
    new = make_account_dir(fake_home, "new", "new@x.com", "tok-new", fake_keychain)
    (new / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok-new"}})
    )

    targets = [_home_canonical(fake_home)]
    # Activate A so HOME .claude.json + pointer + legacy Keychain hold A.
    ac.activate_account_config(str(prev), targets)
    ptr_path = fake_home / ".ccswitch" / "active"
    home_json = fake_home / ".claude.json"
    assert ptr_path.read_text().strip() == str(prev)
    assert json.loads(home_json.read_text())["oauthAccount"]["emailAddress"] == "prev@x.com"

    # Force shutil.copy2 to raise — the atomic copy step runs before mirror.
    def failing_copy(src, dst, *a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("backend.services.account_service.shutil.copy2", failing_copy)

    with pytest.raises(OSError, match="disk full"):
        ac.activate_account_config(str(new), targets)

    # Pointer still points at the previous account (existing invariant).
    assert ptr_path.read_text().strip() == str(prev)
    # NEW invariant: HOME .claude.json still has the previous identity — the
    # mirror step was never reached because the copy raised first.
    assert json.loads(home_json.read_text())["oauthAccount"]["emailAddress"] == "prev@x.com"


# ── Tests: no targets enabled (default — nothing outside isolated dir) ─────


def test_activate_with_no_targets_leaves_home_untouched(fake_home, fake_keychain):
    """With an empty enabled_targets list, HOME .claude.json is not modified."""
    from backend.services import account_service as ac

    home_json = fake_home / ".claude.json"
    home_json.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "existing@x.com"},
        "userID": "existing-uid",
    }))

    acct_dir = make_account_dir(fake_home, "a", "new@x.com", "tok-a", fake_keychain)
    summary = ac.activate_account_config(str(acct_dir), [])

    home = json.loads(home_json.read_text())
    assert home["oauthAccount"]["emailAddress"] == "existing@x.com"
    assert home["userID"] == "existing-uid"

    # No legacy Keychain write either.
    assert "Claude Code-credentials" not in fake_keychain["store"]

    assert summary["system_default_enabled"] is False
    assert summary["keychain_written"] is False
    # The mirror step records a "no targets enabled" skip message.
    assert summary["mirror"]["skipped"], summary


def test_activate_with_no_targets_still_updates_pointer(fake_home, fake_keychain):
    """The active pointer is always written — it reflects dashboard intent,
    independent of external mirroring."""
    from backend.services import account_service as ac

    a = make_account_dir(fake_home, "a", "a@x.com", "tok-a", fake_keychain)
    ac.activate_account_config(str(a), [])

    ptr = fake_home / ".ccswitch" / "active"
    assert ptr.exists()
    assert ptr.read_text().strip() == str(a)


def test_get_active_email_reads_via_pointer(fake_home, fake_keychain):
    """After a no-target activation, HOME is untouched but get_active_email
    still returns the target's email — it reads via the pointer file."""
    from backend.services import account_service as ac

    # Pre-existing HOME with a stale oauthAccount the service must ignore.
    home_json = fake_home / ".claude.json"
    home_json.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "stale@x.com"},
    }))

    a = make_account_dir(fake_home, "a", "a@x.com", "tok-a", fake_keychain)
    ac.activate_account_config(str(a), [])

    # Pointer was updated → get_active_email reports the target's email.
    assert ac.get_active_email() == "a@x.com"


def test_get_active_email_falls_back_to_home_without_pointer(fake_home, fake_keychain):
    """Cold-start: no pointer yet, HOME has an oauthAccount — that is the
    email the service reports until the first switch runs."""
    from backend.services import account_service as ac

    home_json = fake_home / ".claude.json"
    home_json.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "cold@x.com"},
    }))

    # Pointer path should not exist yet.
    ptr = fake_home / ".ccswitch" / "active"
    if ptr.exists():
        ptr.unlink()

    assert ac.get_active_email() == "cold@x.com"
