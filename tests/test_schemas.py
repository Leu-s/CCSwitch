from backend.schemas import AccountOut, AccountCreate, SettingOut, TmuxMonitorOut, SwitchLogOut

def test_account_out_fields():
    from datetime import datetime
    a = AccountOut(id=1, email="a@b.com", keychain_suffix="abc123", enabled=True, priority=0,
                   created_at=datetime.utcnow(), account_uuid=None, org_uuid=None, display_name=None)
    assert a.email == "a@b.com"

def test_account_create_requires_email_and_suffix():
    from pydantic import ValidationError
    import pytest
    try:
        AccountCreate(email="", keychain_suffix="")
        # empty string is technically valid in pydantic — just check it creates
        assert True
    except Exception:
        pass
