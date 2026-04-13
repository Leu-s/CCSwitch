"""
Tests for backend.services.keychain.

The new keychain module only exposes get_active_email(config_dir).
All other functions (read_credentials, write_active_credentials,
scan_keychain, update_oauth_account, read_active_credentials) have
been removed.  Old tests for those functions are replaced with
placeholder stubs or removed.
"""
import json
import pytest


def test_get_active_email(tmp_path):
    from backend.services.keychain import get_active_email
    config = {"oauthAccount": {"emailAddress": "active@x.com"}}
    (tmp_path / ".claude.json").write_text(json.dumps(config))
    assert get_active_email(str(tmp_path)) == "active@x.com"


def test_get_active_email_returns_none_on_missing(tmp_path):
    from backend.services.keychain import get_active_email
    assert get_active_email(str(tmp_path)) is None


def test_get_active_email_returns_none_on_malformed_json(tmp_path):
    from backend.services.keychain import get_active_email
    (tmp_path / ".claude.json").write_text("not valid json{{")
    assert get_active_email(str(tmp_path)) is None


def test_get_active_email_returns_none_when_email_absent(tmp_path):
    from backend.services.keychain import get_active_email
    config = {"oauthAccount": {}}
    (tmp_path / ".claude.json").write_text(json.dumps(config))
    assert get_active_email(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# Functions removed from keychain — kept as placeholder stubs so that any
# import of these names fails cleanly and the test suite stays green.
# ---------------------------------------------------------------------------

def test_removed_read_credentials_is_not_importable():
    """read_credentials was removed from keychain; verify it is gone."""
    import backend.services.keychain as kc
    assert not hasattr(kc, "read_credentials")


def test_removed_write_active_credentials_is_not_importable():
    """write_active_credentials was removed from keychain; verify it is gone."""
    import backend.services.keychain as kc
    assert not hasattr(kc, "write_active_credentials")


def test_removed_scan_keychain_is_not_importable():
    """scan_keychain was removed from keychain; verify it is gone."""
    import backend.services.keychain as kc
    assert not hasattr(kc, "scan_keychain")
