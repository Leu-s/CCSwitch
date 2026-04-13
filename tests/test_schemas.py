"""
Tests for backend.schemas.

AccountOut and AccountCreate have changed:
- removed: keychain_suffix, account_uuid, org_uuid
- added: threshold_pct (float, default 95.0)
- removed from AccountOut (unused by frontend): display_name, config_dir, created_at
- AccountCreate no longer exists; accounts are created via the login flow.
"""
from datetime import datetime, timezone

from backend.schemas import (
    AccountOut,
    AccountWithUsage,
    SettingOut,
    SwitchLogOut,
    UsageData,
)


def _now():
    return datetime.now(timezone.utc)


def test_account_out_fields():
    a = AccountOut(
        id=1,
        email="a@b.com",
        threshold_pct=95.0,
        enabled=True,
        priority=0,
    )
    assert a.email == "a@b.com"
    assert a.threshold_pct == 95.0
    assert a.enabled is True


def test_account_out_no_old_fields():
    """keychain_suffix, account_uuid, org_uuid, display_name, config_dir and
    created_at must not appear in AccountOut (none are read by the frontend)."""
    import backend.schemas as schemas
    assert not hasattr(schemas.AccountOut.model_fields, "keychain_suffix")
    assert not hasattr(schemas.AccountOut.model_fields, "account_uuid")
    assert not hasattr(schemas.AccountOut.model_fields, "org_uuid")
    assert "display_name" not in schemas.AccountOut.model_fields
    assert "config_dir" not in schemas.AccountOut.model_fields
    assert "created_at" not in schemas.AccountOut.model_fields


def test_account_create_removed():
    """AccountCreate was removed; it should not exist in the schema module."""
    import backend.schemas as schemas
    assert not hasattr(schemas, "AccountCreate")


def test_setting_out():
    s = SettingOut(key="auto_switch_enabled", value="true")
    assert s.key == "auto_switch_enabled"
    assert s.value == "true"


def test_switch_log_out():
    log = SwitchLogOut(
        id=1,
        from_account_id=1,
        to_account_id=2,
        reason="threshold",
        triggered_at=_now(),
    )
    assert log.reason == "threshold"
    assert log.to_account_id == 2


# ── UsageData.from_raw ───────────────────────────────────────────────────────

class TestUsageDataFromRaw:
    """Covers every branch of UsageData.from_raw so future refactors cannot
    silently regress the shape of what the frontend receives."""

    def test_error_surfaces_with_token_info(self):
        u = UsageData.from_raw(
            {"error": "boom"},
            {"token_expires_at": 123, "subscription_type": "max"},
        )
        assert u is not None
        assert u.error == "boom"
        assert u.token_expires_at == 123
        assert u.subscription_type == "max"
        # No usage percentages when there is only an error
        assert u.five_hour_pct is None
        assert u.seven_day_pct is None
        assert u.rate_limited is None

    def test_five_hour_and_seven_day_populate_from_nested_dicts(self):
        raw = {
            "five_hour": {"utilization": 42.5, "resets_at": 1700000000},
            "seven_day": {"utilization": 10.0, "resets_at": 1700100000},
        }
        u = UsageData.from_raw(raw, {})
        assert u is not None
        assert u.five_hour_pct == 42.5
        assert u.five_hour_resets_at == 1700000000
        assert u.seven_day_pct == 10.0
        assert u.seven_day_resets_at == 1700100000
        assert u.error is None
        assert u.rate_limited is None

    def test_rate_limited_preserves_previous_usage_and_sets_flag(self):
        raw = {
            "five_hour": {"utilization": 90, "resets_at": 1700000000},
            "seven_day": {"utilization": 50, "resets_at": 1700100000},
            "rate_limited": True,
        }
        u = UsageData.from_raw(raw, {"subscription_type": "pro"})
        assert u is not None
        assert u.rate_limited is True
        assert u.five_hour_pct == 90
        assert u.subscription_type == "pro"

    def test_token_info_only_returns_metadata_only_entry(self):
        """Brand new account: no usage yet but Keychain has expiry + tier."""
        u = UsageData.from_raw({}, {"token_expires_at": 999, "subscription_type": "max"})
        assert u is not None
        assert u.token_expires_at == 999
        assert u.subscription_type == "max"
        assert u.five_hour_pct is None
        assert u.error is None

    def test_empty_raw_and_empty_token_info_returns_none(self):
        assert UsageData.from_raw({}, {}) is None

    def test_missing_five_hour_key_gracefully_handled(self):
        """When only seven_day is present, five_hour fields stay None."""
        u = UsageData.from_raw(
            {"seven_day": {"utilization": 10.0, "resets_at": 1700000000}},
            {},
        )
        assert u is not None
        assert u.five_hour_pct is None
        assert u.seven_day_pct == 10.0
