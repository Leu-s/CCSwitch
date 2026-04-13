"""
Tests for backend.schemas.

AccountOut and AccountCreate have changed:
- removed: keychain_suffix, account_uuid, org_uuid
- added: config_dir (str), threshold_pct (float, default 95.0)
- AccountCreate no longer exists; accounts are created via the login flow.
"""
from datetime import datetime, timezone

from backend.schemas import AccountOut, AccountWithUsage, SettingOut, TmuxMonitorOut, SwitchLogOut


def _now():
    return datetime.now(timezone.utc)


def test_account_out_fields():
    a = AccountOut(
        id=1,
        email="a@b.com",
        config_dir="/home/user/.claude-multi-accounts/account-abc",
        threshold_pct=95.0,
        enabled=True,
        priority=0,
        created_at=_now(),
    )
    assert a.email == "a@b.com"
    assert a.config_dir == "/home/user/.claude-multi-accounts/account-abc"
    assert a.threshold_pct == 95.0
    assert a.enabled is True


def test_account_out_no_old_fields():
    """keychain_suffix, account_uuid and org_uuid must not appear in AccountOut."""
    import backend.schemas as schemas
    assert not hasattr(schemas.AccountOut.model_fields, "keychain_suffix")
    assert not hasattr(schemas.AccountOut.model_fields, "account_uuid")
    assert not hasattr(schemas.AccountOut.model_fields, "org_uuid")


def test_account_out_optional_display_name():
    a = AccountOut(
        id=2,
        email="b@b.com",
        config_dir="/tmp/acc",
        threshold_pct=80.0,
        enabled=False,
        priority=1,
        created_at=_now(),
        display_name="Test",
    )
    assert a.display_name == "Test"


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


def test_tmux_monitor_out():
    m = TmuxMonitorOut(id=1, name="test", pattern_type="manual", pattern="main:0.0", enabled=True)
    assert m.pattern == "main:0.0"
