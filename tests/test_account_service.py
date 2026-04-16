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
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services import account_service as ac
from backend.services import credential_provider as cp


@pytest.fixture(autouse=True)
def _clear_revalidate_module_state():
    """Module-level dicts in account_service survive across tests without
    this.  Clear before AND after each test for test-order independence."""
    ac._refresh_locks.clear()
    # Also clear the background dicts — revalidate mutates them on success.
    try:
        from backend import background as bg
        bg._refresh_backoff_until.clear()
        bg._refresh_backoff_count.clear()
        bg._refresh_backoff_first_failure_at.clear()
    except Exception:
        pass
    yield
    ac._refresh_locks.clear()
    try:
        from backend import background as bg
        bg._refresh_backoff_until.clear()
        bg._refresh_backoff_count.clear()
        bg._refresh_backoff_first_failure_at.clear()
    except Exception:
        pass


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def fake_keychain(monkeypatch):
    """Replace the four vault/standard helpers with an in-memory store.

    Returns the backing dict ``store`` keyed on ``(service, account)`` so
    tests can assert what ended up in each cell after a swap.

    Also stubs ``anthropic_api.refresh_access_token`` to raise a transient
    network error so swap step 0.5 logs a warning and proceeds with the
    stored tokens — keeps the classic swap tests (that assert stored
    token passes through) behaviourally unchanged after M3.  Tests that
    care about swap-time refresh semantics override this directly.
    """
    import httpx

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

    # M3 swap step 0.5 calls anthropic_api.refresh_access_token.  Default
    # to a transient failure so tests that don't care about refresh
    # semantics still see their stored tokens promoted to standard.
    async def default_refresh_transient(rt):
        raise httpx.ConnectError("default fake_keychain: refresh disabled")
    monkeypatch.setattr(
        ac.anthropic_api, "refresh_access_token", default_refresh_transient,
    )

    return store


@pytest.fixture
def fake_claude_home(monkeypatch, tmp_path):
    """Point ac.*_PATH constants at a tmp_path layout mirroring the real
    locations: identity file at HOME root, credentials file inside
    HOME/.claude/."""
    home_root = tmp_path
    claude_dir = home_root / ".claude"
    claude_dir.mkdir(mode=0o700)
    monkeypatch.setattr(ac, "_HOME", str(home_root))
    monkeypatch.setattr(ac, "_CLAUDE_HOME", str(claude_dir))
    monkeypatch.setattr(ac, "_CLAUDE_JSON_PATH", str(home_root / ".claude.json"))
    monkeypatch.setattr(
        ac, "_CREDENTIALS_JSON_PATH", str(claude_dir / ".credentials.json")
    )
    # Return the HOME ROOT so tests can reference .claude.json via it.
    return home_root


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
    # fake_keychain is used via its monkeypatch side-effects; no direct indexing.
    fake_keychain[("standard", "user")] = _blob("alice@example.com")
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


# ── revalidate_account ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revalidate_account_success_clears_stale(monkeypatch):
    """Successful refresh clears stale_reason and returns success=True."""
    from backend.models import Account

    account = Account(
        id=42,
        email="vault@example.com",
        enabled=True,
        priority=0,
        threshold_pct=90,
        stale_reason="Refresh token rejected — re-login required",
    )

    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id",
        AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: {
            "claudeAiOauth": {
                "accessToken": "old",
                "refreshToken": "rt-old",
                "expiresAt": 0,
            },
            "oauthAccount": {"emailAddress": "vault@example.com"},
            "userID": "u",
        },
    )

    async def fake_refresh(refresh_token):
        assert refresh_token == "rt-old"
        return {"access_token": "new-access", "refresh_token": "rt-new", "expires_in": 3600}

    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", fake_refresh)

    saved = {}
    def fake_save(email, new_token, expires_at=None, refresh_token=None, **kwargs):
        saved["email"] = email
        saved["access"] = new_token
        saved["refresh"] = refresh_token
    monkeypatch.setattr(ac.cp, "save_refreshed_vault_token", fake_save)

    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="other@example.com"),
    )

    result = await ac.revalidate_account(42, db)
    assert result["success"] is True
    assert result["stale_reason"] is None
    assert result["active_refused"] is False
    assert account.stale_reason is None
    assert saved["access"] == "new-access"
    assert saved["refresh"] == "rt-new"
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_revalidate_account_refuses_active_account(monkeypatch):
    """Active-account revalidate violates the single-refresher invariant
    (CLI owns the active refresh lifecycle) — must refuse with a clear
    error, leaving stale_reason untouched for the user to switch-then-retry."""
    from backend.models import Account

    account = Account(
        id=42,
        email="active@example.com",
        enabled=True,
        priority=0,
        threshold_pct=90,
        stale_reason="Refresh token rejected — re-login required",
    )

    db = MagicMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="active@example.com"),
    )
    # The refresh function should NEVER be called.
    refresh_calls = []
    async def fake_refresh(refresh_token):
        refresh_calls.append(refresh_token)
        return {}
    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", fake_refresh)

    result = await ac.revalidate_account(42, db)
    assert result["success"] is False
    assert result["active_refused"] is True
    assert "active" in result["stale_reason"].lower()
    # Original stale_reason unchanged — we don't overwrite user-facing state
    # for an operation we refused.
    assert account.stale_reason == "Refresh token rejected — re-login required"
    assert refresh_calls == []


@pytest.mark.asyncio
async def test_revalidate_account_concurrent_calls_are_strictly_serialised(monkeypatch):
    """Stronger assertion than ..._serialize: verifies the SECOND concurrent
    revalidate call does not begin its refresh block until the FIRST one has
    fully released the lock.  Catches the failure mode where two coroutines
    each acquire a different Lock object (broken get_refresh_lock)."""
    from backend.models import Account

    account = Account(
        id=42, email="vault@example.com", enabled=True, priority=0,
        threshold_pct=90,
        stale_reason="Refresh token rejected — re-login required",
    )
    db = MagicMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="other@example.com"),
    )
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: {
            "claudeAiOauth": {
                "accessToken": "a", "refreshToken": "rt", "expiresAt": 0,
            },
        },
    )

    enter_times: list[float] = []
    exit_times: list[float] = []

    async def timed_refresh(refresh_token):
        enter_times.append(asyncio.get_event_loop().time())
        await asyncio.sleep(0.1)  # sizable hold so parallelism would be visible
        exit_times.append(asyncio.get_event_loop().time())
        return {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}

    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", timed_refresh)
    monkeypatch.setattr(ac.cp, "save_refreshed_vault_token", lambda *a, **kw: None)

    # Two concurrent revalidate calls on the SAME email.
    results = await asyncio.gather(
        ac.revalidate_account(42, db),
        ac.revalidate_account(42, db),
    )

    assert all(r["success"] for r in results)
    assert len(enter_times) == 2 and len(exit_times) == 2
    # The CORE assertion: second call entered strictly after first call exited.
    # If the lock were broken (two separate Lock objects), enter_times[1]
    # would be ≈ enter_times[0] (both fire in parallel), not > exit_times[0].
    assert enter_times[1] >= exit_times[0] - 0.005, (
        f"Concurrent revalidate calls overlapped: "
        f"first exit={exit_times[0]}, second enter={enter_times[1]}"
    )


@pytest.mark.asyncio
async def test_revalidate_account_genuine_invalid_grant_keeps_stale(monkeypatch):
    """If refresh returns 400 invalid_grant, stale_reason is updated with precise reason."""
    from backend.models import Account

    account = Account(
        id=42,
        email="vault@example.com",
        enabled=True,
        priority=0,
        threshold_pct=90,
        stale_reason="Refresh endpoint transient failure — re-login required",
    )

    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: {
            "claudeAiOauth": {
                "accessToken": "old", "refreshToken": "rt-dead", "expiresAt": 0,
            },
            "oauthAccount": {"emailAddress": "vault@example.com"},
            "userID": "u",
        },
    )

    import httpx
    req = httpx.Request("POST", "https://api.anthropic.com/oauth2/token")
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 400
    resp.json = MagicMock(return_value={"error": "invalid_grant"})

    async def fake_refresh(refresh_token):
        raise httpx.HTTPStatusError("bad", request=req, response=resp)

    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", fake_refresh)

    # Active-check runs first — if account is not active, proceed.
    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="other@example.com"),
    )

    result = await ac.revalidate_account(42, db)
    assert result["success"] is False
    assert result["active_refused"] is False
    assert "rejected" in result["stale_reason"].lower()
    assert account.stale_reason == result["stale_reason"]


@pytest.mark.asyncio
async def test_revalidate_account_missing_account_returns_none(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=None),
    )
    result = await ac.revalidate_account(999, db)
    assert result is None


@pytest.mark.asyncio
async def test_revalidate_account_missing_refresh_token_returns_error(monkeypatch):
    from backend.models import Account

    account = Account(
        id=42,
        email="vault@example.com",
        enabled=True,
        priority=0,
        threshold_pct=90,
        stale_reason="something",
    )
    db = MagicMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="other@example.com"),
    )
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: None,
    )

    result = await ac.revalidate_account(42, db)
    assert result["success"] is False
    assert result["active_refused"] is False
    assert "vault" in result["stale_reason"].lower()


@pytest.mark.asyncio
async def test_revalidate_account_network_error_returns_sanitized_reason(monkeypatch):
    """Network errors from the refresh endpoint must NOT surface raw exception
    strings (which can contain host/port/socket details) to the user.  Only a
    generic message; full exception goes to logs."""
    import httpx
    from backend.models import Account

    account = Account(
        id=42, email="vault@example.com", enabled=True, priority=0,
        threshold_pct=90,
        stale_reason="Refresh token rejected — re-login required",
    )
    db = MagicMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="other@example.com"),
    )
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: {
            "claudeAiOauth": {"accessToken": "a", "refreshToken": "rt", "expiresAt": 0},
        },
    )

    async def network_error(rt):
        raise httpx.ConnectError("Connection refused to 127.0.0.1:8080")

    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", network_error)

    result = await ac.revalidate_account(42, db)
    assert result["success"] is False
    assert result["active_refused"] is False
    # Sanitised — no host/port/socket/exception details.
    reason = result["stale_reason"]
    assert "127.0.0.1" not in reason
    assert "8080" not in reason
    assert "Connection refused" not in reason
    assert "try again later" in reason.lower()
    assert account.stale_reason == reason


def test_merge_checkpoint_strips_expires_at_from_nested_shape(monkeypatch):
    """Swap step 2 checkpoint: strip expiresAt from the CLI's claudeAiOauth
    nested shape.  Next successful refresh (via _refresh_vault_token) will
    write a fresh one based on the response's expires_in field."""
    from backend.services import credential_provider as cp

    fresh_standard = {
        "claudeAiOauth": {
            "accessToken": "at",
            "refreshToken": "rt",
            "expiresAt": 99999999999,  # bogus claim from CLI's last write
            "subscriptionType": "max",
        },
        "oauthAccount": {"emailAddress": "out@example.com"},
        "userID": "u",
    }
    previous_vault = {
        "oauthAccount": {"emailAddress": "out@example.com"},
        "userID": "u",
    }
    monkeypatch.setattr(cp, "read_vault", lambda email: previous_vault)

    merged = ac._merge_checkpoint("out@example.com", fresh_standard)
    inner = merged.get("claudeAiOauth", {})
    # Tokens preserved, expiresAt stripped.
    assert inner.get("accessToken") == "at"
    assert inner.get("refreshToken") == "rt"
    assert inner.get("subscriptionType") == "max"
    assert "expiresAt" not in inner


def test_merge_checkpoint_strips_expires_at_from_legacy_shape(monkeypatch):
    """Legacy (non-nested) standard entries have top-level expiresAt.
    Strip from both the root AND any subsequent nested refresh."""
    from backend.services import credential_provider as cp

    fresh_standard_legacy = {
        "accessToken": "at",
        "refreshToken": "rt",
        "expiresAt": 99999999999,  # root-level claim
        "subscriptionType": "max",
    }
    monkeypatch.setattr(cp, "read_vault", lambda email: {})

    merged = ac._merge_checkpoint("out@example.com", fresh_standard_legacy)
    # No expiresAt at any level.
    assert "expiresAt" not in merged
    assert "expiresAt" not in merged.get("claudeAiOauth", {})


@pytest.mark.asyncio
async def test_with_refresh_lock_async_releases_on_cancellation():
    """If an awaiter of with_refresh_lock_async is cancelled while the
    lock is held by someone else, the executor-thread acquire must not
    leak the lock when it eventually completes.  Before the cancellation-
    safe fix, this test hangs indefinitely on the final acquire."""
    import threading as _th
    email = "cancel-test@example.com"
    # Start with a clean lock.
    ac.forget_refresh_lock(email)

    # Hold the lock on a different thread.
    holder_lock = ac.get_refresh_lock(email)
    holder_lock.acquire()

    # Task that wants the lock — will block on it.
    async def waiter():
        async with ac.with_refresh_lock_async(email):
            pass  # never reached if we cancel during acquire

    task = asyncio.create_task(waiter())
    # Let the task get into the to_thread acquire call.
    await asyncio.sleep(0.05)

    # Cancel while blocked.
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Release the holder.  The waiter's executor thread should acquire,
    # then the done-callback should immediately release.  After a brief
    # delay, the lock should be free.
    holder_lock.release()

    # Spin briefly to give the done-callback time to fire.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if ac.get_refresh_lock(email).acquire(blocking=False):
            ac.get_refresh_lock(email).release()
            return  # PASS — lock was freed
    ac.forget_refresh_lock(email)
    pytest.fail(
        "Lock leaked after awaiter cancellation — ghost holds the lock. "
        "This is the family-revoke-adjacent bug the fix closes."
    )
