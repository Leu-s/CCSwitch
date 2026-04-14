"""Tests for backend/services/credential_provider.py"""
import hashlib
import json
import os
import pytest
from unittest.mock import patch, MagicMock

from backend.services.credential_provider import (
    _keychain_service_name,
    get_access_token_from_config_dir,
    get_refresh_token_from_config_dir,
    get_token_info,
    save_refreshed_token,
)


# ── _keychain_service_name ────────────────────────────────────────────────────

class TestKeychainServiceName:
    def test_returns_string(self):
        name = _keychain_service_name("/some/config/dir")
        assert isinstance(name, str)

    def test_starts_with_prefix(self):
        name = _keychain_service_name("/some/config/dir")
        assert name.startswith("Claude Code-credentials-")

    def test_suffix_is_8_hex_chars(self):
        name = _keychain_service_name("/some/config/dir")
        suffix = name.split("-")[-1]
        assert len(suffix) == 8
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_suffix_matches_sha256(self):
        config_dir = "/home/user/.config/ccswitch/account-abc123"
        expected_hash = hashlib.sha256(config_dir.encode()).hexdigest()[:8]
        name = _keychain_service_name(config_dir)
        assert name == f"Claude Code-credentials-{expected_hash}"

    @pytest.mark.parametrize("config_dir", [
        "/home/user/.ccswitch/account-aabbccdd",
        "/Users/alice/.ccswitch-accounts/account-deadbeef",
        "/tmp/test-dir",
    ])
    def test_deterministic_for_same_input(self, config_dir):
        name1 = _keychain_service_name(config_dir)
        name2 = _keychain_service_name(config_dir)
        assert name1 == name2

    def test_different_dirs_produce_different_names(self):
        name1 = _keychain_service_name("/dir/one")
        name2 = _keychain_service_name("/dir/two")
        assert name1 != name2


# ── get_access_token_from_config_dir ─────────────────────────────────────────

class TestGetAccessTokenFromConfigDir:
    def test_returns_token_from_keychain_nested(self):
        kc = {"claudeAiOauth": {"accessToken": "kc-nested-token"}}
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value=kc):
            result = get_access_token_from_config_dir("/some/dir")
        assert result == "kc-nested-token"

    def test_returns_token_from_keychain_flat(self):
        kc = {"accessToken": "kc-flat-token"}
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value=kc):
            result = get_access_token_from_config_dir("/some/dir")
        assert result == "kc-flat-token"

    def test_falls_back_to_credentials_json_nested(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        cred_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "file-nested-token"}}))
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            result = get_access_token_from_config_dir(str(tmp_path))
        assert result == "file-nested-token"

    def test_falls_back_to_credentials_json_flat(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        cred_file.write_text(json.dumps({"accessToken": "file-flat-token"}))
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            result = get_access_token_from_config_dir(str(tmp_path))
        assert result == "file-flat-token"

    def test_falls_back_to_credentials_json_without_dot(self, tmp_path):
        cred_file = tmp_path / "credentials.json"
        cred_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "nodot-token"}}))
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            result = get_access_token_from_config_dir(str(tmp_path))
        assert result == "nodot-token"

    def test_falls_back_to_claude_json(self, tmp_path):
        claude_file = tmp_path / ".claude.json"
        claude_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "claude-json-token"}}))
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            result = get_access_token_from_config_dir(str(tmp_path))
        assert result == "claude-json-token"

    def test_returns_none_when_nothing_found(self, tmp_path):
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            result = get_access_token_from_config_dir(str(tmp_path))
        assert result is None

    def test_returns_none_when_keychain_has_no_token(self, tmp_path):
        kc = {"claudeAiOauth": {"refreshToken": "only-refresh"}}
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value=kc):
            result = get_access_token_from_config_dir(str(tmp_path))
        assert result is None


# ── get_refresh_token_from_config_dir ────────────────────────────────────────

class TestGetRefreshTokenFromConfigDir:
    def test_returns_token_from_keychain_nested(self):
        kc = {"claudeAiOauth": {"refreshToken": "kc-refresh-nested"}}
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value=kc):
            result = get_refresh_token_from_config_dir("/some/dir")
        assert result == "kc-refresh-nested"

    def test_returns_token_from_keychain_flat(self):
        kc = {"refreshToken": "kc-refresh-flat"}
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value=kc):
            result = get_refresh_token_from_config_dir("/some/dir")
        assert result == "kc-refresh-flat"

    def test_falls_back_to_credentials_json_nested(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        cred_file.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "file-refresh-nested"}}))
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            result = get_refresh_token_from_config_dir(str(tmp_path))
        assert result == "file-refresh-nested"

    def test_falls_back_to_credentials_json_flat(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        cred_file.write_text(json.dumps({"refreshToken": "file-refresh-flat"}))
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            result = get_refresh_token_from_config_dir(str(tmp_path))
        assert result == "file-refresh-flat"

    def test_returns_none_when_nothing_found(self, tmp_path):
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            result = get_refresh_token_from_config_dir(str(tmp_path))
        assert result is None

    @pytest.mark.parametrize("filename", [".credentials.json", "credentials.json", ".claude.json"])
    def test_reads_from_all_candidate_files(self, tmp_path, filename):
        cred_file = tmp_path / filename
        cred_file.write_text(json.dumps({"claudeAiOauth": {"refreshToken": f"token-from-{filename}"}}))
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            result = get_refresh_token_from_config_dir(str(tmp_path))
        assert result == f"token-from-{filename}"


# ── save_refreshed_token ──────────────────────────────────────────────────────

class TestSaveRefreshedToken:
    def test_updates_access_token_in_nested_credentials_json(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        original = {"claudeAiOauth": {"accessToken": "old-token", "refreshToken": "refresh"}}
        cred_file.write_text(json.dumps(original))

        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            save_refreshed_token(str(tmp_path), "new-token")

        data = json.loads(cred_file.read_text())
        assert data["claudeAiOauth"]["accessToken"] == "new-token"
        assert data["claudeAiOauth"]["refreshToken"] == "refresh"  # preserved

    def test_updates_expires_at_when_provided(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        original = {"claudeAiOauth": {"accessToken": "old-token", "expiresAt": 1000}}
        cred_file.write_text(json.dumps(original))

        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            save_refreshed_token(str(tmp_path), "new-token", expires_at=9999)

        data = json.loads(cred_file.read_text())
        assert data["claudeAiOauth"]["accessToken"] == "new-token"
        assert data["claudeAiOauth"]["expiresAt"] == 9999

    def test_does_not_add_expires_at_when_not_provided(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        original = {"claudeAiOauth": {"accessToken": "old-token"}}
        cred_file.write_text(json.dumps(original))

        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            save_refreshed_token(str(tmp_path), "new-token")

        data = json.loads(cred_file.read_text())
        assert "expiresAt" not in data["claudeAiOauth"]

    def test_updates_flat_access_token_format(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        original = {"accessToken": "old-flat", "refreshToken": "flat-refresh"}
        cred_file.write_text(json.dumps(original))

        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            save_refreshed_token(str(tmp_path), "new-flat-token")

        data = json.loads(cred_file.read_text())
        assert data["accessToken"] == "new-flat-token"
        assert data["refreshToken"] == "flat-refresh"  # preserved

    def test_updates_flat_format_expires_at(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        original = {"accessToken": "old-flat", "expiresAt": 500}
        cred_file.write_text(json.dumps(original))

        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            save_refreshed_token(str(tmp_path), "new-flat-token", expires_at=7777)

        data = json.loads(cred_file.read_text())
        assert data["accessToken"] == "new-flat-token"
        assert data["expiresAt"] == 7777

    def test_prefers_dot_credentials_json_over_credentials_json(self, tmp_path):
        # Both files exist — .credentials.json should be used first
        dot_cred = tmp_path / ".credentials.json"
        plain_cred = tmp_path / "credentials.json"
        dot_cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "dot-old"}}))
        plain_cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "plain-old"}}))

        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            save_refreshed_token(str(tmp_path), "dot-new")

        assert json.loads(dot_cred.read_text())["claudeAiOauth"]["accessToken"] == "dot-new"
        # plain_cred should be untouched
        assert json.loads(plain_cred.read_text())["claudeAiOauth"]["accessToken"] == "plain-old"

    def test_does_nothing_when_no_credential_files(self, tmp_path):
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            # Should not raise
            save_refreshed_token(str(tmp_path), "some-token")

    def test_also_updates_keychain_when_present(self, tmp_path):
        # Use .claude.json so the function does NOT early-return from the file loop
        # and falls through to the Keychain update block at the end.
        claude_file = tmp_path / ".claude.json"
        claude_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old"}}))

        kc_data = {"claudeAiOauth": {"accessToken": "kc-old", "refreshToken": "kc-refresh"}}
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value=kc_data), \
             patch("backend.services.credential_provider._write_keychain_credentials") as mock_write_kc:
            save_refreshed_token(str(tmp_path), "updated-token", expires_at=1234)

        # Keychain should have been written with the new token
        mock_write_kc.assert_called_once()
        written_creds = mock_write_kc.call_args[0][0]
        assert written_creds["claudeAiOauth"]["accessToken"] == "updated-token"
        assert written_creds["claudeAiOauth"]["expiresAt"] == 1234

    def test_saves_rotated_refresh_token_nested(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        original = {"claudeAiOauth": {"accessToken": "old", "refreshToken": "old-rt"}}
        cred_file.write_text(json.dumps(original))

        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            save_refreshed_token(str(tmp_path), "new-at", expires_at=5000, refresh_token="new-rt")

        data = json.loads(cred_file.read_text())
        assert data["claudeAiOauth"]["accessToken"] == "new-at"
        assert data["claudeAiOauth"]["refreshToken"] == "new-rt"
        assert data["claudeAiOauth"]["expiresAt"] == 5000

    def test_saves_rotated_refresh_token_flat(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        original = {"accessToken": "old", "refreshToken": "old-rt"}
        cred_file.write_text(json.dumps(original))

        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            save_refreshed_token(str(tmp_path), "new-at", refresh_token="new-rt")

        data = json.loads(cred_file.read_text())
        assert data["accessToken"] == "new-at"
        assert data["refreshToken"] == "new-rt"

    def test_preserves_refresh_token_when_not_rotated(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        original = {"claudeAiOauth": {"accessToken": "old", "refreshToken": "keep-me"}}
        cred_file.write_text(json.dumps(original))

        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            save_refreshed_token(str(tmp_path), "new-at")

        data = json.loads(cred_file.read_text())
        assert data["claudeAiOauth"]["refreshToken"] == "keep-me"

    def test_rotated_refresh_token_written_to_keychain(self, tmp_path):
        claude_file = tmp_path / ".claude.json"
        claude_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old"}}))

        kc_data = {"claudeAiOauth": {"accessToken": "kc-old", "refreshToken": "kc-old-rt"}}
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value=kc_data), \
             patch("backend.services.credential_provider._write_keychain_credentials") as mock_write_kc:
            save_refreshed_token(str(tmp_path), "new-at", refresh_token="new-rt")

        mock_write_kc.assert_called_once()
        written = mock_write_kc.call_args[0][0]
        assert written["claudeAiOauth"]["accessToken"] == "new-at"
        assert written["claudeAiOauth"]["refreshToken"] == "new-rt"


# ── get_token_info ────────────────────────────────────────────────────────────

class TestGetTokenInfo:
    def test_returns_token_info_from_keychain(self):
        kc = {"claudeAiOauth": {"expiresAt": 9999, "subscriptionType": "pro"}}
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value=kc):
            result = get_token_info("/some/dir")
        assert result["token_expires_at"] == 9999
        assert result["subscription_type"] == "pro"

    def test_returns_partial_info_when_only_expires_at(self):
        kc = {"claudeAiOauth": {"expiresAt": 1111}}
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value=kc):
            result = get_token_info("/some/dir")
        assert result["token_expires_at"] == 1111
        assert "subscription_type" not in result

    def test_returns_partial_info_when_only_subscription_type(self):
        kc = {"claudeAiOauth": {"subscriptionType": "free"}}
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value=kc):
            result = get_token_info("/some/dir")
        assert result["subscription_type"] == "free"
        assert "token_expires_at" not in result

    def test_falls_back_to_file_when_keychain_empty(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        cred_file.write_text(json.dumps({"claudeAiOauth": {"expiresAt": 5555, "subscriptionType": "team"}}))
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            result = get_token_info(str(tmp_path))
        assert result["token_expires_at"] == 5555
        assert result["subscription_type"] == "team"

    def test_returns_empty_dict_when_nothing_found(self, tmp_path):
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            result = get_token_info(str(tmp_path))
        assert result == {}

    def test_result_has_expected_key_names(self):
        kc = {"claudeAiOauth": {"expiresAt": 42, "subscriptionType": "max"}}
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value=kc):
            result = get_token_info("/some/dir")
        assert set(result.keys()) <= {"token_expires_at", "subscription_type"}

    def test_flat_keychain_format(self):
        kc = {"expiresAt": 8888, "subscriptionType": "pro"}
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value=kc):
            result = get_token_info("/some/dir")
        assert result.get("token_expires_at") == 8888
        assert result.get("subscription_type") == "pro"

    @pytest.mark.parametrize("filename", [".credentials.json", "credentials.json", ".claude.json"])
    def test_fallback_reads_all_candidate_files(self, tmp_path, filename):
        cred_file = tmp_path / filename
        cred_file.write_text(json.dumps({"claudeAiOauth": {"expiresAt": 1234}}))
        with patch("backend.services.credential_provider._read_keychain_credentials", return_value={}):
            result = get_token_info(str(tmp_path))
        assert result.get("token_expires_at") == 1234
