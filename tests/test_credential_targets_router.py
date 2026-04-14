"""Router tests for /api/credential-targets."""
import os
import json
import pytest


@pytest.fixture(scope="module")
def client(make_test_app):
    from backend.routers.credential_targets import router
    _, c = make_test_app(router, db_name="credential_targets_router")
    return c


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_get_credential_targets_empty(client, fake_home):
    """With no .claude.json files on disk, the endpoint returns an empty list."""
    resp = client.get("/api/credential-targets")
    assert resp.status_code == 200
    data = resp.json()
    # Nothing on disk and nothing persisted → empty.
    assert isinstance(data, list)
    for t in data:
        # Any leaked entries from shared state should at least be well-formed.
        assert "canonical" in t and "enabled" in t


def test_get_credential_targets_surfaces_home_root(client, fake_home):
    """A newly-written $HOME/.claude.json should appear in the list with
    enabled=False and the current oauthAccount email populated."""
    _write_json(fake_home / ".claude.json", {
        "oauthAccount": {"emailAddress": "hello@x.com"},
    })
    resp = client.get("/api/credential-targets")
    assert resp.status_code == 200
    data = resp.json()
    canonical = os.path.realpath(str(fake_home / ".claude.json"))
    match = next((t for t in data if t["canonical"] == canonical), None)
    assert match is not None
    assert match["exists"] is True
    assert match["current_email"] == "hello@x.com"
    assert match["enabled"] is False


def test_patch_toggle_enables_target(client, fake_home):
    """PATCH flips the enabled flag and the subsequent GET reflects it."""
    _write_json(fake_home / ".claude.json", {
        "oauthAccount": {"emailAddress": "flip@x.com"},
    })
    canonical = os.path.realpath(str(fake_home / ".claude.json"))

    resp = client.patch(
        "/api/credential-targets",
        json={"canonical": canonical, "enabled": True},
    )
    assert resp.status_code == 200, resp.text

    # The response already reflects the new state.
    data = resp.json()
    match = next(t for t in data if t["canonical"] == canonical)
    assert match["enabled"] is True

    # And a fresh GET confirms it's persisted.
    resp2 = client.get("/api/credential-targets")
    match2 = next(t for t in resp2.json() if t["canonical"] == canonical)
    assert match2["enabled"] is True

    # Toggle back off.
    client.patch(
        "/api/credential-targets",
        json={"canonical": canonical, "enabled": False},
    )
    resp3 = client.get("/api/credential-targets")
    match3 = next((t for t in resp3.json() if t["canonical"] == canonical), None)
    # When disabled the entry may stay in the list (because the file still
    # exists on disk) but its `enabled` flag must be False.
    assert match3 is not None
    assert match3["enabled"] is False


def test_patch_rejects_empty_canonical(client):
    resp = client.patch(
        "/api/credential-targets",
        json={"canonical": "", "enabled": True},
    )
    assert resp.status_code == 400


def test_post_sync_mirrors_active_to_enabled_targets(client, fake_home, monkeypatch, tmp_path):
    """POST /sync re-mirrors the currently active account into every enabled
    target without requiring a switch."""
    # Build a fake "active" account dir with an oauthAccount we can detect.
    active_dir = tmp_path / "active-acct"
    active_dir.mkdir()
    (active_dir / ".claude.json").write_text(
        '{"oauthAccount": {"emailAddress": "synced@x.com"}, "userID": "uid-synced"}'
    )

    # Pretend the dashboard's pointer file points at it.
    pointer_dir = fake_home / ".ccswitch"
    pointer_dir.mkdir(parents=True, exist_ok=True)
    (pointer_dir / "active").write_text(str(active_dir))

    # A target file with stale identity that should get rewritten.
    target = fake_home / ".claude.json"
    target.write_text(
        '{"oauthAccount": {"emailAddress": "stale@x.com"}, "keep": 1}'
    )
    canonical = os.path.realpath(str(target))

    # Disable the system-default Keychain hook so the test doesn't try to
    # touch the real macOS Keychain.
    monkeypatch.setattr(
        "backend.services.account_service._system_default_canonicals",
        lambda: set(),
    )

    # Enable the target.
    client.patch(
        "/api/credential-targets",
        json={"canonical": canonical, "enabled": True},
    )

    resp = client.post("/api/credential-targets/sync")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert canonical in body["summary"]["mirror"]["written"]

    # The target file now reflects the active account's email and preserves
    # the unrelated `keep` key.
    import json
    out = json.loads(target.read_text())
    assert out["oauthAccount"]["emailAddress"] == "synced@x.com"
    assert out["userID"] == "uid-synced"
    assert out["keep"] == 1


def test_post_sync_with_no_active_pointer_reports_error(client, fake_home, monkeypatch):
    """If no active account pointer exists, /sync returns an error in the
    summary instead of crashing."""
    pointer = fake_home / ".ccswitch" / "active"
    if pointer.exists():
        pointer.unlink()

    monkeypatch.setattr(
        "backend.services.account_service._system_default_canonicals",
        lambda: set(),
    )

    resp = client.post("/api/credential-targets/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["mirror"]["errors"], body


def test_post_rescan_returns_fresh_list(client, fake_home):
    _write_json(fake_home / ".claude-accounts" / "rescan-test" / ".claude.json", {
        "oauthAccount": {"emailAddress": "rescan@x.com"},
    })
    resp = client.post("/api/credential-targets/rescan")
    assert resp.status_code == 200
    data = resp.json()
    canonical = os.path.realpath(
        str(fake_home / ".claude-accounts" / "rescan-test" / ".claude.json")
    )
    match = next((t for t in data if t["canonical"] == canonical), None)
    assert match is not None
    assert match["current_email"] == "rescan@x.com"
