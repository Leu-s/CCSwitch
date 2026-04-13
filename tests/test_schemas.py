"""
Tests for backend.schemas.

AccountOut and AccountCreate have changed:
- removed: keychain_suffix, account_uuid, org_uuid
- added: threshold_pct (float, default 95.0)
- removed from AccountOut (unused by frontend): display_name, config_dir, created_at
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


def test_tmux_monitor_out():
    m = TmuxMonitorOut(id=1, name="test", pattern_type="manual", pattern="main:0.0", enabled=True)
    assert m.pattern == "main:0.0"
