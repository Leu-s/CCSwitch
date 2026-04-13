"""
Tests for backend.services.credential_targets — discovery, persisted enabled
state, and fan-out mirror writes.

The service module owns the multi-target mirror feature.  These tests hit it
without going through the FastAPI layer so we can exercise filesystem side
effects directly.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from backend.database import Base


TEST_DB_URL = "sqlite+aiosqlite:///./test_credential_targets.db"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


@pytest.fixture
def db():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())

    async def _get():
        async with SessionLocal() as session:
            yield session

    return _get


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _make_claude_json(fake_home: Path, rel: str, email: str | None = None) -> Path:
    path = fake_home / rel
    body = {}
    if email is not None:
        body = {"oauthAccount": {"emailAddress": email}, "userID": f"uid-{email}"}
    _write_json(path, body)
    return path


# ── Discovery ───────────────────────────────────────────────────────────────


def test_discover_finds_home_root(fake_home):
    from backend.services import credential_targets as ct
    _make_claude_json(fake_home, ".claude.json", "a@x.com")
    targets = ct.discover_targets()
    canonicals = [t["canonical"] for t in targets]
    assert os.path.realpath(str(fake_home / ".claude.json")) in canonicals


def test_discover_finds_home_claude_dir(fake_home):
    from backend.services import credential_targets as ct
    _make_claude_json(fake_home, ".claude/.claude.json", "b@x.com")
    targets = ct.discover_targets()
    canonicals = [t["canonical"] for t in targets]
    assert os.path.realpath(str(fake_home / ".claude" / ".claude.json")) in canonicals


def test_discover_walks_claude_accounts_glob(fake_home):
    from backend.services import credential_targets as ct
    _make_claude_json(fake_home, ".claude-accounts/alpha/.claude.json", "alpha@x.com")
    _make_claude_json(fake_home, ".claude-accounts/beta/.claude.json", "beta@x.com")
    targets = ct.discover_targets()
    canonicals = [t["canonical"] for t in targets]
    assert os.path.realpath(str(fake_home / ".claude-accounts" / "alpha" / ".claude.json")) in canonicals
    assert os.path.realpath(str(fake_home / ".claude-accounts" / "beta" / ".claude.json")) in canonicals


def test_discover_dedupes_symlinks(fake_home):
    """Two display paths that resolve to the same canonical file must be
    reported only once, under the first display path scanned."""
    from backend.services import credential_targets as ct

    real = fake_home / ".claude-accounts" / "real" / ".claude.json"
    _make_claude_json(fake_home, ".claude-accounts/real/.claude.json", "real@x.com")

    # Create ~/.claude → ~/.claude-accounts/real (directory symlink)
    (fake_home / ".claude").symlink_to(fake_home / ".claude-accounts" / "real")

    targets = ct.discover_targets()
    canonicals = [t["canonical"] for t in targets]
    real_canonical = os.path.realpath(str(real))
    # It should appear exactly once.
    assert canonicals.count(real_canonical) == 1


def test_discover_reports_current_email(fake_home):
    from backend.services import credential_targets as ct
    _make_claude_json(fake_home, ".claude.json", "alice@x.com")
    targets = ct.discover_targets()
    home_canonical = os.path.realpath(str(fake_home / ".claude.json"))
    match = next(t for t in targets if t["canonical"] == home_canonical)
    assert match["current_email"] == "alice@x.com"
    assert match["exists"] is True


# ── Persisted state (enabled flags) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_target_enabled_roundtrip(fake_home, db):
    from backend.services import credential_targets as ct

    _make_claude_json(fake_home, ".claude.json", "a@x.com")
    home_canonical = os.path.realpath(str(fake_home / ".claude.json"))

    async for session in db():
        # Initially nothing enabled.
        enabled = await ct.enabled_canonical_paths(session)
        assert enabled == []

        await ct.set_target_enabled(home_canonical, True, session)
        enabled = await ct.enabled_canonical_paths(session)
        assert home_canonical in enabled

        await ct.set_target_enabled(home_canonical, False, session)
        enabled = await ct.enabled_canonical_paths(session)
        assert home_canonical not in enabled


@pytest.mark.asyncio
async def test_list_targets_joins_discovery_with_state(fake_home, db):
    from backend.services import credential_targets as ct

    _make_claude_json(fake_home, ".claude.json", "a@x.com")
    _make_claude_json(fake_home, ".claude-accounts/foo/.claude.json", "foo@x.com")
    home_canonical = os.path.realpath(str(fake_home / ".claude.json"))

    async for session in db():
        await ct.set_target_enabled(home_canonical, True, session)
        listed = await ct.list_targets(session)

    # HOME target is enabled, others are not.
    by_canonical = {t["canonical"]: t for t in listed}
    assert by_canonical[home_canonical]["enabled"] is True
    other = next(
        t for c, t in by_canonical.items() if c != home_canonical
    )
    assert other["enabled"] is False


@pytest.mark.asyncio
async def test_list_targets_surfaces_missing_stored_entry(fake_home, db):
    """A target that was once enabled but has since been deleted from disk
    must still show up in the list (with exists=False) so the user can
    remove it from the enabled set.

    ``set_target_enabled`` now rejects paths that are not in the current
    discovery set, so the realistic shape of this scenario is "user enabled
    a file that Claude Code has since deleted".  We simulate that by saving
    the phantom entry directly through ``_save_state`` (bypassing the
    enable-time validation), then verify ``list_targets`` still surfaces it
    as "missing"."""
    from backend.services import credential_targets as ct

    phantom = "/does/not/exist/.claude.json"
    async for session in db():
        await ct._save_state({phantom: True}, session)
        listed = await ct.list_targets(session)

    phantom_entry = next(t for t in listed if t["canonical"] == phantom)
    assert phantom_entry["exists"] is False
    assert phantom_entry["enabled"] is True


@pytest.mark.asyncio
async def test_set_target_enabled_rejects_non_discovered_path(fake_home, db):
    """Enabling a path that is NOT in the discovery result set must raise
    ValueError — otherwise a caller with local API access could steer the
    mirror fan-out at ``~/.zshrc``, ``~/.ssh/authorized_keys``, etc."""
    from backend.services import credential_targets as ct

    attacker = str(fake_home / ".ssh" / "authorized_keys")
    async for session in db():
        with pytest.raises(ValueError, match="not in discovered targets"):
            await ct.set_target_enabled(attacker, True, session)
        # And the attacker path was NOT persisted.
        enabled = await ct.enabled_canonical_paths(session)
        assert attacker not in enabled


@pytest.mark.asyncio
async def test_set_target_enabled_disable_allows_stale_path(fake_home, db):
    """Disabling must always be allowed so stale DB entries surfaced by
    ``list_targets`` as "missing" can be cleared, even after the file has
    been deleted from disk."""
    from backend.services import credential_targets as ct

    phantom = "/does/not/exist/.claude.json"
    async for session in db():
        # Prime the state map without going through the enable-time check.
        await ct._save_state({phantom: True}, session)
        # Disabling the stale entry must succeed.
        await ct.set_target_enabled(phantom, False, session)
        enabled = await ct.enabled_canonical_paths(session)
        assert phantom not in enabled


# ── Mirror writes ──────────────────────────────────────────────────────────


def test_mirror_writes_identity_keys_only(fake_home):
    """Mirroring must only touch oauthAccount + userID in each target file,
    preserving every other pre-existing field."""
    from backend.services import credential_targets as ct

    src = fake_home / "account-src"
    src.mkdir()
    _write_json(src / ".claude.json", {
        "oauthAccount": {"emailAddress": "new@x.com"},
        "userID": "uid-new",
    })

    target = fake_home / ".claude.json"
    _write_json(target, {
        "oauthAccount": {"emailAddress": "old@x.com"},
        "userID": "uid-old",
        "projects": {"/w": {"x": 1}},
        "mcpServers": {"m": {"cmd": "echo"}},
        "autoUpdates": True,
    })

    summary = ct.mirror_oauth_into_targets(str(src), [str(target)])

    assert summary["errors"] == []
    assert str(target) in summary["written"]

    out = json.loads(target.read_text())
    assert out["oauthAccount"]["emailAddress"] == "new@x.com"
    assert out["userID"] == "uid-new"
    # Preserved:
    assert out["projects"] == {"/w": {"x": 1}}
    assert out["mcpServers"] == {"m": {"cmd": "echo"}}
    assert out["autoUpdates"] is True


def test_mirror_reports_missing_source(fake_home):
    from backend.services import credential_targets as ct

    summary = ct.mirror_oauth_into_targets(str(fake_home / "nowhere"), [str(fake_home / ".claude.json")])
    assert summary["errors"], summary
    assert any("missing" in e for e in summary["errors"])


def test_mirror_reports_source_without_oauth(fake_home):
    from backend.services import credential_targets as ct

    src = fake_home / "account-bad"
    src.mkdir()
    _write_json(src / ".claude.json", {"someKey": 1})

    summary = ct.mirror_oauth_into_targets(str(src), [str(fake_home / ".claude.json")])
    assert summary["errors"], summary
    assert any("oauthAccount" in e for e in summary["errors"])


def test_mirror_empty_targets_skips_cleanly(fake_home):
    from backend.services import credential_targets as ct

    src = fake_home / "account-src"
    src.mkdir()
    _write_json(src / ".claude.json", {
        "oauthAccount": {"emailAddress": "a@x.com"},
        "userID": "uid-a",
    })

    summary = ct.mirror_oauth_into_targets(str(src), [])
    assert summary["written"] == []
    assert summary["errors"] == []
    assert summary["skipped"], "empty target list should surface a skip message"


def test_mirror_writes_to_multiple_targets(fake_home):
    """The whole point of the feature: one activate call fans out to every
    enabled target."""
    from backend.services import credential_targets as ct

    src = fake_home / "account-src"
    src.mkdir()
    _write_json(src / ".claude.json", {
        "oauthAccount": {"emailAddress": "everywhere@x.com"},
        "userID": "uid-everywhere",
    })

    t1 = fake_home / ".claude.json"
    t2 = fake_home / ".claude-accounts" / "foo" / ".claude.json"
    _write_json(t1, {"oauthAccount": {"emailAddress": "stale1@x.com"}, "keep": 1})
    _write_json(t2, {"oauthAccount": {"emailAddress": "stale2@x.com"}, "keep": 2})

    summary = ct.mirror_oauth_into_targets(str(src), [str(t1), str(t2)])

    assert set(summary["written"]) == {str(t1), str(t2)}
    assert summary["errors"] == []

    out1 = json.loads(t1.read_text())
    out2 = json.loads(t2.read_text())
    assert out1["oauthAccount"]["emailAddress"] == "everywhere@x.com"
    assert out2["oauthAccount"]["emailAddress"] == "everywhere@x.com"
    # Unrelated keys preserved in both.
    assert out1["keep"] == 1
    assert out2["keep"] == 2


def test_mirror_creates_missing_target_file(fake_home):
    """If a stored target path does not yet exist on disk (e.g. Claude Code
    hasn't run there yet), the mirror writes a fresh file with just the
    identity keys rather than failing."""
    from backend.services import credential_targets as ct

    src = fake_home / "account-src"
    src.mkdir()
    _write_json(src / ".claude.json", {
        "oauthAccount": {"emailAddress": "fresh@x.com"},
        "userID": "uid-fresh",
    })

    target = fake_home / ".claude-accounts" / "virgin" / ".claude.json"
    assert not target.exists()

    summary = ct.mirror_oauth_into_targets(str(src), [str(target)])

    assert str(target) in summary["written"]
    assert target.exists()
    out = json.loads(target.read_text())
    assert out["oauthAccount"]["emailAddress"] == "fresh@x.com"
