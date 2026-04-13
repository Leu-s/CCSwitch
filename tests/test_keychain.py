import pytest
import json
from unittest.mock import patch, MagicMock

CREDS = {"claudeAiOauth": {"accessToken": "sk-ant-test", "refreshToken": "rt-test", "expiresAt": 9999999999}}

def test_read_credentials():
    from backend.services.keychain import read_credentials
    mock_result = MagicMock(stdout=json.dumps(CREDS), returncode=0)
    with patch("subprocess.run", return_value=mock_result):
        result = read_credentials("abc123")
    assert result["claudeAiOauth"]["accessToken"] == "sk-ant-test"

def test_write_active_credentials():
    from backend.services.keychain import write_active_credentials
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        write_active_credentials(CREDS)
    args = mock_run.call_args[0][0]
    assert "add-generic-password" in args
    assert "Claude Code-credentials" in args

def test_scan_keychain_returns_suffixes():
    from backend.services.keychain import scan_keychain
    dump_output = """
    "svce"<blob>="Claude Code-credentials-3798613e"
    "svce"<blob>="Claude Code-credentials-c4d4f94b"
    "svce"<blob>="Claude Code-credentials"
    """
    with patch("subprocess.run", return_value=MagicMock(stdout=dump_output, returncode=0)):
        result = scan_keychain()
    assert "3798613e" in result
    assert "c4d4f94b" in result

def test_update_oauth_account(tmp_path):
    from backend.services.keychain import update_oauth_account
    config = {"oauthAccount": {"emailAddress": "old@x.com"}, "numStartups": 5}
    config_file = tmp_path / ".claude.json"
    config_file.write_text(json.dumps(config))
    new_oauth = {"emailAddress": "new@x.com", "accountUuid": "uuid-1"}
    update_oauth_account(str(tmp_path), new_oauth)
    updated = json.loads(config_file.read_text())
    assert updated["oauthAccount"]["emailAddress"] == "new@x.com"
    assert updated["numStartups"] == 5

def test_get_active_email(tmp_path):
    from backend.services.keychain import get_active_email
    config = {"oauthAccount": {"emailAddress": "active@x.com"}}
    (tmp_path / ".claude.json").write_text(json.dumps(config))
    assert get_active_email(str(tmp_path)) == "active@x.com"

def test_get_active_email_returns_none_on_missing(tmp_path):
    from backend.services.keychain import get_active_email
    assert get_active_email(str(tmp_path)) is None
