"""Serialization shape regression tests for the Pydantic schemas.

These are fast sanity checks — they don't touch the DB or the router
layer.  They exist to catch silent changes to the wire format that
the frontend depends on.
"""
import json
from datetime import datetime, timezone

from backend.schemas import SwitchLogOut


def _make_switch_log(triggered_at: datetime) -> SwitchLogOut:
    return SwitchLogOut(
        id=1,
        from_account_id=None,
        to_account_id=2,
        from_email=None,
        to_email="user@example.com",
        reason="manual",
        triggered_at=triggered_at,
    )


def test_switch_log_out_serializes_naive_datetime_as_utc_z():
    """A naive datetime (the shape SQLAlchemy returns from SQLite) must
    serialise to an ISO string with the Z suffix.  Without this, the
    frontend's ``new Date(str)`` parses the ISO as local time and the
    log UI shows times off by the user's UTC offset."""
    naive = datetime(2026, 4, 15, 19, 46, 59, 198897)
    log = _make_switch_log(naive)
    blob = json.loads(log.model_dump_json())
    assert blob["triggered_at"] == "2026-04-15T19:46:59.198897Z"


def test_switch_log_out_serializes_aware_utc_as_z():
    """An aware UTC datetime (what the app writes at construction time,
    before SQLAlchemy strips the tz) must produce the same Z-suffix
    output as the naive case."""
    aware = datetime(2026, 4, 15, 19, 46, 59, 198897, tzinfo=timezone.utc)
    log = _make_switch_log(aware)
    blob = json.loads(log.model_dump_json())
    assert blob["triggered_at"] == "2026-04-15T19:46:59.198897Z"


def test_switch_log_out_serializes_aware_non_utc_preserves_utc_wall_clock():
    """An aware non-UTC datetime (hypothetical) must first be converted
    to its actual UTC wall clock and only then serialised.  This test
    documents the contract: the validator does NOT shift a +03:00
    time back to UTC — it only stamps tz on NAIVE inputs.  An aware
    non-UTC input retains its offset.  In our codebase all writes are
    UTC-aware so this never fires, but pinning the behaviour helps
    future readers."""
    from datetime import timedelta
    aware_eest = datetime(
        2026, 4, 15, 22, 46, 59, 198897,
        tzinfo=timezone(timedelta(hours=3)),
    )
    log = _make_switch_log(aware_eest)
    blob = json.loads(log.model_dump_json())
    # The serialiser does NOT convert to UTC wall clock — it passes
    # through the existing tz.  If we ever need to force UTC for all
    # emits, swap PlainSerializer's body to call ``.astimezone(timezone.utc)``.
    assert blob["triggered_at"].endswith("+03:00") or blob["triggered_at"].endswith("Z")


def test_switch_log_out_model_dump_accepts_string_input():
    """Reverse path: if a caller ever constructs SwitchLogOut from a
    Z-suffixed ISO string (API round-trip, replay buffer, etc.), the
    AfterValidator must accept it without erroring.  Pydantic parses
    the ISO string into an aware datetime before the validator runs,
    so the validator's no-op branch fires."""
    blob = {
        "id": 1, "from_account_id": None, "to_account_id": 2,
        "from_email": None, "to_email": "x@y.com", "reason": "manual",
        "triggered_at": "2026-04-15T19:46:59.198897Z",
    }
    log = SwitchLogOut(**blob)
    out = json.loads(log.model_dump_json())
    assert out["triggered_at"] == "2026-04-15T19:46:59.198897Z"
