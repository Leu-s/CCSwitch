"""
Tests for backend.services.account_service.

Covers the 5-step ``swap_to_account`` orchestrator, the
``get_active_email`` accessor, ``save_new_vault_account``,
``delete_account_everywhere``, and ``startup_integrity_check``.

Strategy: replace the Keychain helpers (``cp.read_vault`` / ``write_vault``
/ ``read_standard`` / ``write_standard``) with an in-memory dict so the
assertions can inspect exactly which service/account keys were written
without touching the real Keychain.  Redirect the module's hardcoded
``~/.claude/`` paths at a ``tmp_path`` subdirectory per test.
"""
import json

import pytest

from backend.services import account_service as ac
from backend.services import credential_provider as cp


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def fake_keychain(monkeypatch):
    """Replace the four vault/standard helpers with an in-memory store.

    Returns the backing dict ``store`` keyed on ``(service, account)`` so
    tests can assert what ended up in each cell after a swap.
    """
    store: dict[tuple[str, str], dict] = {}

    def read_vault(email):
        return store.get(("vault", email))

    def write_vault(email, creds):
        store[("vault", email)] = dict(creds)
        return True

    def delete_vault(email):
        store.pop(("vault", email), None)

    def read_standard():
        return store.get(("standard", "user"))

    def write_standard(creds):
        store[("standard", "user")] = dict(creds)
        return True

    def delete_standard():
        store.pop(("standard", "user"), None)

    monkeypatch.setattr(cp, "read_vault", read_vault)
    monkeypatch.setattr(cp, "write_vault", write_vault)
    monkeypatch.setattr(cp, "delete_vault", delete_vault)
    monkeypatch.setattr(cp, "read_standard", read_standard)
    monkeypatch.setattr(cp, "write_standard", write_standard)
    monkeypatch.setattr(cp, "delete_standard", delete_standard)
    return store


@pytest.fixture
def fake_claude_home(monkeypatch, tmp_path):
    """Point ac.*_PATH constants at a tmp_path subdirectory."""
    home = tmp_path / ".claude"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(ac, "_CLAUDE_HOME", str(home))
    monkeypatch.setattr(ac, "_CLAUDE_JSON_PATH", str(home / ".claude.json"))
    monkeypatch.setattr(
        ac, "_CREDENTIALS_JSON_PATH", str(home / ".credentials.json")
    )
    return home


def _blob(email: str, refresh="rt", access="at") -> dict:
    return {
        "claudeAiOauth": {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": 1_700_000_000_000,
        },
        "oauthAccount": {"emailAddress": email},
        "userID": f"uid-{email.split('@')[0]}",
    }


# ── swap_to_account happy path ─────────────────────────────────────────────


def test_swap_happy_path_a_to_b(fake_keychain, fake_claude_home):
    """A is currently standard; B is in vault.  After swap: standard holds
    B, vault[A] holds the checkpointed standard contents, .claude.json
    names B."""
    store = fake_keychain
    store[("standard", "user")] = _blob("alice@example.com", refresh="a-rt")
    store[("vault", "bob@example.com")] = _blob("bob@example.com", refresh="b-rt")

    summary = ac.swap_to_account("bob@example.com")

    assert summary["target_email"] == "bob@example.com"
    assert summary["previous_email"] == "alice@example.com"
    assert summary["checkpoint_written"] is True
    # Standard now holds B's creds
    assert store[("standard", "user")]["oauthAccount"]["emailAddress"] == "bob@example.com"
    # Vault[A] exists and holds A's tokens (the checkpoint)
    assert store[("vault", "alice@example.com")]["claudeAiOauth"]["refreshToken"] == "a-rt"
    # .claude.json names B
    data = json.loads((fake_claude_home / ".claude.json").read_text())
    assert data["oauthAccount"]["emailAddress"] == "bob@example.com"


def test_swap_first_activation_no_outgoing(fake_keychain, fake_claude_home):
    """Standard empty, only vault[B] exists.  Swap promotes B, no checkpoint."""
    store = fake_keychain
    store[("vault", "bob@example.com")] = _blob("bob@example.com")

    summary = ac.swap_to_account("bob@example.com")

    assert summary["target_email"] == "bob@example.com"
    assert summary["previous_email"] is None
    assert summary["checkpoint_written"] is False
    assert store[("standard", "user")]["oauthAccount"]["emailAddress"] == "bob@example.com"
    data = json.loads((fake_claude_home / ".claude.json").read_text())
    assert data["oauthAccount"]["emailAddress"] == "bob@example.com"


# ── swap_to_account error paths ───────────────────────────────────────────


def test_swap_raises_when_vault_missing(fake_keychain, fake_claude_home):
    with pytest.raises(ac.SwapError) as excinfo:
        ac.swap_to_account("ghost@example.com")
    assert "no vault entry" in str(excinfo.value).lower() or "re-login" in str(excinfo.value).lower()


def test_swap_raises_when_vault_has_no_refresh_token(fake_keychain, fake_claude_home):
    # Vault blob with accessToken but no refreshToken
    fake_keychain[("vault", "bob@example.com")] = {
        "claudeAiOauth": {"accessToken": "at"},
        "oauthAccount": {"emailAddress": "bob@example.com"},
    }
    with pytest.raises(ac.SwapError) as excinfo:
        ac.swap_to_account("bob@example.com")
    assert "refresh token" in str(excinfo.value).lower()


def test_swap_checkpoint_failure_aborts_before_promote(monkeypatch, fake_keychain, fake_claude_home):
    """If the checkpoint (vault write for outgoing) fails, the swap must
    raise and NOT overwrite the standard entry."""
    store = fake_keychain
    store[("standard", "user")] = _blob("alice@example.com", refresh="a-rt")
    store[("vault", "bob@example.com")] = _blob("bob@example.com", refresh="b-rt")

    original_standard_snapshot = dict(store[("standard", "user")])

    # Make write_vault fail ONLY for alice (the outgoing checkpoint).
    original_write_vault = cp.write_vault

    def failing_write_vault(email, creds):
        if email == "alice@example.com":
            return False
        return original_write_vault(email, creds)

    monkeypatch.setattr(cp, "write_vault", failing_write_vault)

    with pytest.raises(ac.SwapError):
        ac.swap_to_account("bob@example.com")

    # Standard was NOT overwritten — still alice's credentials.
    assert store[("standard", "user")] == original_standard_snapshot


# ── .claude.json preservation ─────────────────────────────────────────────


def test_swap_preserves_unrelated_claude_json_keys(fake_keychain, fake_claude_home):
    """Existing .claude.json has projects + mcp keys.  After a swap those
    keys must survive — only oauthAccount + userID are replaced."""
    store = fake_keychain
    store[("standard", "user")] = _blob("alice@example.com")
    store[("vault", "bob@example.com")] = _blob("bob@example.com")

    claude_json_path = fake_claude_home / ".claude.json"
    claude_json_path.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "alice@example.com"},
        "userID": "uid-alice",
        "projects": ["proj-1", "proj-2"],
        "mcp": {"servers": ["filesystem"]},
    }))

    ac.swap_to_account("bob@example.com")

    data = json.loads(claude_json_path.read_text())
    assert data["oauthAccount"]["emailAddress"] == "bob@example.com"
    assert data["projects"] == ["proj-1", "proj-2"]
    assert data["mcp"] == {"servers": ["filesystem"]}


# ── get_active_email ──────────────────────────────────────────────────────


def test_get_active_email_reads_identity_file(fake_claude_home):
    (fake_claude_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "carol@example.com"},
        "userID": "uid-carol",
    }))
    assert ac.get_active_email() == "carol@example.com"


def test_get_active_email_returns_none_when_missing(fake_claude_home):
    # .claude.json does not exist → get_active_email returns None
    assert ac.get_active_email() is None


def test_get_active_email_returns_none_when_oauthaccount_absent(fake_claude_home):
    (fake_claude_home / ".claude.json").write_text(json.dumps({
        "projects": [],
    }))
    assert ac.get_active_email() is None


# ── save_new_vault_account ─────────────────────────────────────────────────


def test_save_new_vault_account_writes_blob(fake_keychain):
    ac.save_new_vault_account(
        email="dan@example.com",
        oauth_tokens={"accessToken": "at", "refreshToken": "rt"},
        oauth_account={"emailAddress": "dan@example.com"},
        user_id="uid-dan",
    )
    written = fake_keychain[("vault", "dan@example.com")]
    assert written["claudeAiOauth"] == {"accessToken": "at", "refreshToken": "rt"}
    assert written["oauthAccount"] == {"emailAddress": "dan@example.com"}
    assert written["userID"] == "uid-dan"


# ── delete_account_everywhere ─────────────────────────────────────────────


def test_delete_account_everywhere_active_clears_standard_and_identity(
    fake_keychain, fake_claude_home
):
    store = fake_keychain
    store[("vault", "alice@example.com")] = _blob("alice@example.com")
    store[("standard", "user")] = _blob("alice@example.com")
    (fake_claude_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "alice@example.com"},
        "userID": "uid-alice",
        "projects": [],
    }))

    ac.delete_account_everywhere("alice@example.com")

    assert ("vault", "alice@example.com") not in store
    assert ("standard", "user") not in store
    data = json.loads((fake_claude_home / ".claude.json").read_text())
    assert "oauthAccount" not in data
    assert "userID" not in data
    assert data.get("projects") == []  # unrelated key preserved


def test_delete_account_everywhere_inactive_only_clears_vault(
    fake_keychain, fake_claude_home
):
    """When deleting a non-active account, the standard entry and the
    identity file must not be touched."""
    store = fake_keychain
    store[("vault", "bob@example.com")] = _blob("bob@example.com")
    store[("standard", "user")] = _blob("alice@example.com")
    (fake_claude_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "alice@example.com"},
        "userID": "uid-alice",
    }))

    ac.delete_account_everywhere("bob@example.com")

    assert ("vault", "bob@example.com") not in store
    assert ("standard", "user") in store  # alice still standard
    data = json.loads((fake_claude_home / ".claude.json").read_text())
    assert data["oauthAccount"]["emailAddress"] == "alice@example.com"


# ── startup_integrity_check ───────────────────────────────────────────────


def test_startup_integrity_rewrites_claude_json_on_disagreement(
    fake_keychain, fake_claude_home
):
    """Standard has bob, .claude.json has alice — rewrite to bob (Keychain wins)."""
    store = fake_keychain
    store[("standard", "user")] = _blob("bob@example.com")
    (fake_claude_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "alice@example.com"},
        "userID": "uid-alice",
    }))

    ac.startup_integrity_check()

    data = json.loads((fake_claude_home / ".claude.json").read_text())
    assert data["oauthAccount"]["emailAddress"] == "bob@example.com"


def test_startup_integrity_noop_when_they_agree(fake_keychain, fake_claude_home):
    store = fake_keychain
    store[("standard", "user")] = _blob("alice@example.com")
    identity_path = fake_claude_home / ".claude.json"
    identity_path.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "alice@example.com"},
        "userID": "uid-alice",
        "projects": ["p1"],
    }))

    ac.startup_integrity_check()

    # File may or may not be touched but content remains identical.
    data = json.loads(identity_path.read_text())
    assert data["oauthAccount"]["emailAddress"] == "alice@example.com"
    assert data["projects"] == ["p1"]


def test_startup_integrity_noop_when_standard_empty(fake_keychain, fake_claude_home):
    # Standard has no entry at all → nothing to reconcile.
    (fake_claude_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "alice@example.com"},
        "userID": "uid-alice",
    }))

    ac.startup_integrity_check()

    data = json.loads((fake_claude_home / ".claude.json").read_text())
    assert data["oauthAccount"]["emailAddress"] == "alice@example.com"
