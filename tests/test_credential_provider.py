"""
Tests for backend.services.credential_provider.

The vault-swap architecture partitions the Keychain into two service
namespaces (``Claude Code-credentials`` and ``ccswitch-vault``) plus a
transient hashed namespace for login scratch dirs.  Every helper in the
provider module wraps a single ``security`` subprocess call; these tests
monkeypatch ``subprocess.run`` so the assertions never touch the real
Keychain.
"""
import hashlib
import json
import subprocess

import pytest

from backend.services import credential_provider as cp


# ── Fake subprocess helpers ────────────────────────────────────────────────


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_run(monkeypatch, responder):
    """Install ``responder(cmd, **kwargs) -> _FakeCompleted | raise``."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        result = responder(cmd, **kwargs)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    return calls


# ── read_vault ─────────────────────────────────────────────────────────────


def test_read_vault_returns_parsed_json(monkeypatch):
    blob = {"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt"}}

    def responder(cmd, **_):
        assert cmd[:2] == ["security", "find-generic-password"]
        assert "-s" in cmd and "ccswitch-vault" in cmd
        assert "-a" in cmd and "alice@example.com" in cmd
        return _FakeCompleted(0, json.dumps(blob))

    _install_run(monkeypatch, responder)
    assert cp.read_vault("alice@example.com") == blob


def test_read_vault_returns_none_on_not_found(monkeypatch):
    _install_run(monkeypatch, lambda cmd, **_: _FakeCompleted(1, ""))
    assert cp.read_vault("missing@example.com") is None


def test_read_vault_returns_none_on_malformed_json(monkeypatch):
    _install_run(monkeypatch, lambda cmd, **_: _FakeCompleted(0, "{not json"))
    assert cp.read_vault("broken@example.com") is None


def test_read_vault_returns_none_on_timeout(monkeypatch):
    def responder(cmd, **_):
        return subprocess.TimeoutExpired(cmd=cmd, timeout=5)

    _install_run(monkeypatch, responder)
    assert cp.read_vault("slow@example.com") is None


# ── write_vault ────────────────────────────────────────────────────────────


def test_write_vault_delete_then_add(monkeypatch):
    blob = {"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt"}}

    def responder(cmd, **_):
        assert cmd[0] == "security"
        assert cmd[1] in ("delete-generic-password", "add-generic-password")
        assert "-s" in cmd and "ccswitch-vault" in cmd
        assert "-a" in cmd and "alice@example.com" in cmd
        return _FakeCompleted(0)

    calls = _install_run(monkeypatch, responder)
    assert cp.write_vault("alice@example.com", blob) is True
    assert [c[1] for c in calls] == ["delete-generic-password", "add-generic-password"]


def test_write_vault_returns_false_on_add_failure(monkeypatch):
    def responder(cmd, **_):
        if cmd[1] == "add-generic-password":
            return _FakeCompleted(1, "", "kSecDuplicateItem")
        return _FakeCompleted(0)

    _install_run(monkeypatch, responder)
    assert cp.write_vault("alice@example.com", {}) is False


# ── read_standard ──────────────────────────────────────────────────────────


def test_read_standard_uses_correct_service_and_user(monkeypatch):
    import getpass
    blob = {"claudeAiOauth": {"accessToken": "at"}}

    def responder(cmd, **_):
        assert cmd[:2] == ["security", "find-generic-password"]
        assert "-s" in cmd and "Claude Code-credentials" in cmd
        # Uses current unix user for -a
        assert "-a" in cmd and getpass.getuser() in cmd
        return _FakeCompleted(0, json.dumps(blob))

    _install_run(monkeypatch, responder)
    assert cp.read_standard() == blob


# ── Token field extraction ─────────────────────────────────────────────────


def test_access_token_of_handles_nested_shape():
    blob = {"claudeAiOauth": {"accessToken": "nested-at"}}
    assert cp.access_token_of(blob) == "nested-at"


def test_access_token_of_handles_top_level_shape():
    blob = {"accessToken": "top-at"}
    assert cp.access_token_of(blob) == "top-at"


def test_access_token_of_returns_none_for_none_and_empty():
    assert cp.access_token_of(None) is None
    assert cp.access_token_of({}) is None


def test_refresh_token_of_handles_both_shapes():
    assert cp.refresh_token_of({"claudeAiOauth": {"refreshToken": "rt1"}}) == "rt1"
    assert cp.refresh_token_of({"refreshToken": "rt2"}) == "rt2"
    assert cp.refresh_token_of(None) is None


def test_token_info_of_nested_and_top_level():
    nested = {"claudeAiOauth": {"expiresAt": 1234, "subscriptionType": "pro"}}
    assert cp.token_info_of(nested) == {
        "token_expires_at": 1234,
        "subscription_type": "pro",
    }
    top = {"expiresAt": 9999, "subscriptionType": "max"}
    assert cp.token_info_of(top) == {
        "token_expires_at": 9999,
        "subscription_type": "max",
    }
    assert cp.token_info_of(None) == {}
    assert cp.token_info_of({}) == {}


# ── save_refreshed_vault_token ─────────────────────────────────────────────


def test_save_refreshed_vault_token_merges_into_nested(monkeypatch):
    """Updates tokens in the nested claudeAiOauth sub-object, preserves
    the outer oauthAccount + userID identity fields."""
    existing = {
        "claudeAiOauth": {
            "accessToken": "old",
            "refreshToken": "old-rt",
            "expiresAt": 1000,
            "subscriptionType": "pro",
        },
        "oauthAccount": {"emailAddress": "alice@example.com"},
        "userID": "user-abc",
    }

    written: dict = {}

    def fake_read_vault(email):
        return existing

    def fake_write_vault(email, creds):
        written["email"] = email
        written["creds"] = creds
        return True

    monkeypatch.setattr(cp, "read_vault", fake_read_vault)
    monkeypatch.setattr(cp, "write_vault", fake_write_vault)

    cp.save_refreshed_vault_token(
        "alice@example.com",
        access_token="new",
        expires_at=2000,
        refresh_token="new-rt",
    )

    assert written["email"] == "alice@example.com"
    c = written["creds"]
    assert c["claudeAiOauth"]["accessToken"] == "new"
    assert c["claudeAiOauth"]["refreshToken"] == "new-rt"
    assert c["claudeAiOauth"]["expiresAt"] == 2000
    # Identity preserved
    assert c["oauthAccount"] == {"emailAddress": "alice@example.com"}
    assert c["userID"] == "user-abc"
    # Unchanged field preserved
    assert c["claudeAiOauth"]["subscriptionType"] == "pro"


# ── read_login_scratch / delete_login_scratch ──────────────────────────────


def test_read_login_scratch_uses_hashed_service_name(monkeypatch):
    scratch_dir = "/tmp/ccswitch-login/session-abc"
    expected_hash = hashlib.sha256(scratch_dir.encode()).hexdigest()[:8]
    expected_service = f"Claude Code-credentials-{expected_hash}"
    blob = {"claudeAiOauth": {"refreshToken": "fresh-rt"}}

    def responder(cmd, **_):
        assert cmd[:2] == ["security", "find-generic-password"]
        assert expected_service in cmd
        return _FakeCompleted(0, json.dumps(blob))

    _install_run(monkeypatch, responder)
    assert cp.read_login_scratch(scratch_dir) == blob


def test_delete_login_scratch_deletes_hashed_service(monkeypatch):
    scratch_dir = "/tmp/ccswitch-login/session-xyz"
    expected_hash = hashlib.sha256(scratch_dir.encode()).hexdigest()[:8]
    expected_service = f"Claude Code-credentials-{expected_hash}"

    def responder(cmd, **_):
        assert cmd[:2] == ["security", "delete-generic-password"]
        assert expected_service in cmd
        return _FakeCompleted(0)

    calls = _install_run(monkeypatch, responder)
    cp.delete_login_scratch(scratch_dir)
    assert len(calls) == 1


# ── probe_keychain_available ───────────────────────────────────────────────


def test_probe_keychain_available_returns_true_on_44(monkeypatch):
    _install_run(monkeypatch, lambda cmd, **_: _FakeCompleted(44))
    assert cp.probe_keychain_available() is True


def test_probe_keychain_available_false_on_36(monkeypatch):
    _install_run(monkeypatch, lambda cmd, **_: _FakeCompleted(36))
    assert cp.probe_keychain_available() is False


def test_probe_keychain_available_false_on_51(monkeypatch):
    _install_run(monkeypatch, lambda cmd, **_: _FakeCompleted(51))
    assert cp.probe_keychain_available() is False


def test_probe_keychain_available_false_on_timeout(monkeypatch):
    def responder(cmd, **_):
        return subprocess.TimeoutExpired(cmd=cmd, timeout=5)

    _install_run(monkeypatch, responder)
    assert cp.probe_keychain_available() is False
