"""
Tests for backend.services.switcher.

Covers ``get_next_account`` filtering, ``perform_switch`` orchestration,
and ``maybe_auto_switch`` gating on ``service_enabled``.

Patches:
 - ``ac.swap_to_account`` returns a summary dict (no real Keychain touch)
 - ``tmux_service.fire_nudge`` no-op (no real tmux)

DB + cache state are set up directly on an in-memory SQLite engine; the
tests bypass the router layer because the switcher is a pure service.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.cache import cache as _cache
from backend.database import Base
from backend.models import Account, Setting, SwitchLog
from backend.services import account_service as ac
from backend.services import switcher as sw
from backend.services import tmux_service


TEST_DB_URL = "sqlite+aiosqlite:///./test_switcher.db"


# ── DB fixture ─────────────────────────────────────────────────────────────


@pytest.fixture
async def db_session():
    """Yield a fresh AsyncSession backed by a dropped/recreated SQLite DB."""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _wipe_cache():
    """Reset the module-level in-memory cache between tests."""
    _cache._usage.clear()
    _cache._token_info.clear()
    yield
    _cache._usage.clear()
    _cache._token_info.clear()


@pytest.fixture(autouse=True)
def _silence_nudge(monkeypatch):
    """fire_nudge is blocking tmux — tests never need the real one."""
    monkeypatch.setattr(tmux_service, "fire_nudge", lambda: None)


def _make_account(**kwargs) -> Account:
    defaults = dict(
        email="a@example.com",
        threshold_pct=95.0,
        enabled=True,
        priority=0,
        stale_reason=None,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return Account(**defaults)


# ── get_next_account ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_next_account_skips_stale(db_session, monkeypatch):
    db_session.add_all([
        _make_account(email="a@example.com", priority=0),
        _make_account(
            email="b@example.com", priority=1, stale_reason="Refresh token revoked",
        ),
        _make_account(email="c@example.com", priority=2),
    ])
    await db_session.commit()

    nxt = await sw.get_next_account("a@example.com", db_session)
    assert nxt is not None
    assert nxt.email == "c@example.com"


@pytest.mark.asyncio
async def test_get_next_account_skips_rate_limited(db_session):
    db_session.add_all([
        _make_account(email="a@example.com", priority=0),
        _make_account(email="b@example.com", priority=1),
        _make_account(email="c@example.com", priority=2),
    ])
    await db_session.commit()

    # b has a rate-limited probe result in cache → switcher must skip it.
    await _cache.set_usage("b@example.com", {"rate_limited": True})

    nxt = await sw.get_next_account("a@example.com", db_session)
    assert nxt is not None
    assert nxt.email == "c@example.com"


@pytest.mark.asyncio
async def test_get_next_account_skips_over_threshold(db_session):
    db_session.add_all([
        _make_account(email="a@example.com", priority=0),
        _make_account(email="b@example.com", priority=1, threshold_pct=80.0),
        _make_account(email="c@example.com", priority=2, threshold_pct=95.0),
    ])
    await db_session.commit()

    # b is at 90% — over its 80% threshold, must be skipped.
    await _cache.set_usage(
        "b@example.com", {"five_hour": {"utilization": 90.0}}
    )
    await _cache.set_usage(
        "c@example.com", {"five_hour": {"utilization": 10.0}}
    )

    nxt = await sw.get_next_account("a@example.com", db_session)
    assert nxt is not None
    assert nxt.email == "c@example.com"


@pytest.mark.asyncio
async def test_get_next_account_returns_first_by_priority(db_session):
    """With nothing in the cache, returns the lowest-priority candidate."""
    db_session.add_all([
        _make_account(email="a@example.com", priority=0),
        _make_account(email="b@example.com", priority=2),
        _make_account(email="c@example.com", priority=1),
    ])
    await db_session.commit()

    nxt = await sw.get_next_account("a@example.com", db_session)
    assert nxt is not None
    assert nxt.email == "c@example.com"


# ── perform_switch ────────────────────────────────────────────────────────


class _FakeWS:
    """Minimal WebSocketManager stand-in that just records broadcasts."""
    def __init__(self):
        self.events: list[dict] = []

    async def broadcast(self, payload: dict) -> int:
        self.events.append(payload)
        return len(self.events)


@pytest.mark.asyncio
async def test_perform_switch_writes_log_and_broadcasts(db_session, monkeypatch):
    db_session.add_all([
        _make_account(email="a@example.com", priority=0),
        _make_account(email="b@example.com", priority=1),
    ])
    await db_session.commit()

    # Active email = a; swap_to_account returns a fake summary.
    monkeypatch.setattr(ac, "get_active_email", lambda: "a@example.com")
    monkeypatch.setattr(
        ac, "swap_to_account",
        lambda email: {
            "target_email": email,
            "previous_email": "a@example.com",
            "checkpoint_written": True,
        },
    )

    ws = _FakeWS()
    target = (await db_session.execute(
        select(Account).where(Account.email == "b@example.com")
    )).scalar_one()

    await sw.perform_switch(target, "manual", db_session, ws)

    # SwitchLog row persisted
    logs = (await db_session.execute(select(SwitchLog))).scalars().all()
    assert len(logs) == 1
    assert logs[0].to_account_id == target.id
    assert logs[0].reason == "manual"
    assert logs[0].from_account_id is not None  # a@example.com maps to a row

    # Broadcast sent
    assert any(e.get("type") == "account_switched" for e in ws.events)

    # Lock released
    assert not sw._switch_lock.locked()


@pytest.mark.asyncio
async def test_perform_switch_fires_nudge(db_session, monkeypatch):
    """Every switch — manual or auto — must fire the tmux nudge so
    running claude panes wake up on the new credentials."""
    db_session.add_all([
        _make_account(email="a@example.com", priority=0),
        _make_account(email="b@example.com", priority=1),
    ])
    await db_session.commit()

    monkeypatch.setattr(ac, "get_active_email", lambda: "a@example.com")
    monkeypatch.setattr(ac, "swap_to_account", lambda _e: {})

    nudge_calls: list[int] = []
    monkeypatch.setattr(
        tmux_service, "fire_nudge",
        lambda: nudge_calls.append(1),
    )

    ws = _FakeWS()
    target = (await db_session.execute(
        select(Account).where(Account.email == "b@example.com")
    )).scalar_one()

    await sw.perform_switch(target, "manual", db_session, ws)

    assert len(nudge_calls) == 1


@pytest.mark.asyncio
async def test_perform_switch_preserves_target_cache(db_session, monkeypatch):
    """perform_switch must NOT invalidate the target's cached usage — the
    UI needs the last-known state visible immediately after the swap, and
    auto-switch decides on the next poll from a fresh probe anyway."""
    db_session.add_all([
        _make_account(email="a@example.com", priority=0),
        _make_account(email="b@example.com", priority=1),
    ])
    await db_session.commit()

    await _cache.set_usage(
        "b@example.com", {"five_hour": {"utilization": 95.0}}
    )
    await _cache.set_token_info("b@example.com", {"token_expires_at": 123})

    monkeypatch.setattr(ac, "get_active_email", lambda: "a@example.com")
    monkeypatch.setattr(ac, "swap_to_account", lambda _email: {})

    ws = _FakeWS()
    target = (await db_session.execute(
        select(Account).where(Account.email == "b@example.com")
    )).scalar_one()

    await sw.perform_switch(target, "manual", db_session, ws)

    usage = await _cache.get_usage_async("b@example.com")
    assert usage.get("five_hour") == {"utilization": 95.0}
    assert await _cache.get_token_info_async("b@example.com") is not None


@pytest.mark.asyncio
async def test_perform_switch_swap_error_broadcasts_and_skips_log(db_session, monkeypatch):
    db_session.add_all([
        _make_account(email="a@example.com", priority=0),
        _make_account(email="b@example.com", priority=1),
    ])
    await db_session.commit()

    monkeypatch.setattr(ac, "get_active_email", lambda: "a@example.com")

    def raise_swap(email):
        raise ac.SwapError(f"vault for {email} missing")

    monkeypatch.setattr(ac, "swap_to_account", raise_swap)

    ws = _FakeWS()
    target = (await db_session.execute(
        select(Account).where(Account.email == "b@example.com")
    )).scalar_one()

    # perform_switch re-raises SwapError so callers (manual_switch router,
    # auto-switch loop) can decide whether to surface a 409 to the user or
    # log and continue.  Either way: no SwitchLog row and an error broadcast.
    with pytest.raises(ac.SwapError):
        await sw.perform_switch(target, "manual", db_session, ws)

    logs = (await db_session.execute(select(SwitchLog))).scalars().all()
    assert logs == []

    assert any(e.get("type") == "error" for e in ws.events)


@pytest.mark.asyncio
async def test_perform_switch_times_out_releases_switch_lock(db_session, monkeypatch):
    """Regression guard: when ``swap_to_account`` hangs inside its worker
    thread (stuck asyncio.run shutdown, stuck security subprocess, etc.),
    ``perform_switch`` MUST raise ``SwapError`` after the deadline so the
    ``async with _switch_lock`` block exits and the lock releases.

    Before the fix, a hung swap held ``_switch_lock`` indefinitely and
    every subsequent switch request (manual or auto) queued forever.
    """
    import time
    db_session.add_all([
        _make_account(email="a@example.com", priority=0),
        _make_account(email="b@example.com", priority=1),
    ])
    await db_session.commit()

    monkeypatch.setattr(ac, "get_active_email", lambda: "a@example.com")

    def hang_forever(email):
        time.sleep(10)  # simulate stuck worker thread
        return {"target_email": email}

    monkeypatch.setattr(ac, "swap_to_account", hang_forever)
    monkeypatch.setattr(sw, "_SWAP_DEADLINE", 0.1)

    ws = _FakeWS()
    target = (await db_session.execute(
        select(Account).where(Account.email == "b@example.com")
    )).scalar_one()

    t0 = time.monotonic()
    with pytest.raises(ac.SwapError) as excinfo:
        await sw.perform_switch(target, "manual", db_session, ws)
    elapsed = time.monotonic() - t0

    assert "timed out" in str(excinfo.value).lower()
    assert elapsed < 2.0, (
        f"perform_switch waited {elapsed:.2f}s past the 0.1s deadline — "
        "asyncio.wait_for is not enforcing the ceiling"
    )

    # Error broadcast so the UI can show a toast.
    assert any(e.get("type") == "error" for e in ws.events)

    # Lock must have been released — a second call returns through the
    # same wait_for ceiling instead of queueing indefinitely.
    t0 = time.monotonic()
    with pytest.raises(ac.SwapError):
        await sw.perform_switch(target, "manual", db_session, ws)
    elapsed2 = time.monotonic() - t0
    assert elapsed2 < 2.0, (
        f"second perform_switch waited {elapsed2:.2f}s — "
        "_switch_lock was not released on the first timeout"
    )


# ── maybe_auto_switch ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_maybe_auto_switch_noop_when_service_disabled(db_session, monkeypatch):
    """With service_enabled=false, even a 100% active account must NOT
    trigger a switch."""
    # Seed service_enabled=false
    db_session.add(Setting(key="service_enabled", value="false"))
    db_session.add_all([
        _make_account(email="a@example.com", priority=0, threshold_pct=80.0),
        _make_account(email="b@example.com", priority=1),
    ])
    await db_session.commit()

    # a is at 99% — well over threshold.
    await _cache.set_usage(
        "a@example.com", {"five_hour": {"utilization": 99.0}}
    )

    monkeypatch.setattr(ac, "get_active_email", lambda: "a@example.com")
    swap_calls: list[str] = []
    monkeypatch.setattr(
        ac, "swap_to_account",
        lambda email: swap_calls.append(email) or {},
    )

    ws = _FakeWS()
    await sw.maybe_auto_switch(db_session, ws)

    assert swap_calls == []
    logs = (await db_session.execute(select(SwitchLog))).scalars().all()
    assert logs == []


@pytest.mark.asyncio
async def test_maybe_auto_switch_rate_limited_triggers_switch(db_session, monkeypatch):
    """Active account probed as rate_limited → auto-switch with reason
    'rate_limited' even if utilization number is below threshold."""
    db_session.add(Setting(key="service_enabled", value="true"))
    db_session.add_all([
        _make_account(email="a@example.com", priority=0),
        _make_account(email="b@example.com", priority=1),
    ])
    await db_session.commit()

    await _cache.set_usage(
        "a@example.com",
        {"five_hour": {"utilization": 10.0}, "rate_limited": True},
    )

    monkeypatch.setattr(ac, "get_active_email", lambda: "a@example.com")
    swap_calls: list[str] = []
    monkeypatch.setattr(ac, "swap_to_account",
                        lambda email: swap_calls.append(email) or {})

    ws = _FakeWS()
    await sw.maybe_auto_switch(db_session, ws)

    assert swap_calls == ["b@example.com"]
    logs = (await db_session.execute(select(SwitchLog))).scalars().all()
    assert len(logs) == 1
    assert logs[0].reason == "rate_limited"


@pytest.mark.asyncio
async def test_maybe_auto_switch_stale_fast_path_switches(db_session, monkeypatch):
    """Current account with stale_reason triggers an immediate switch
    with reason='stale', bypassing the usage/threshold check."""
    db_session.add(Setting(key="service_enabled", value="true"))
    db_session.add_all([
        _make_account(
            email="a@example.com", priority=0,
            stale_reason="Refresh token revoked — re-login required",
        ),
        _make_account(email="b@example.com", priority=1),
    ])
    await db_session.commit()

    monkeypatch.setattr(ac, "get_active_email", lambda: "a@example.com")
    swap_calls: list[str] = []
    monkeypatch.setattr(ac, "swap_to_account",
                        lambda email: swap_calls.append(email) or {})

    ws = _FakeWS()
    await sw.maybe_auto_switch(db_session, ws)

    assert swap_calls == ["b@example.com"]
    logs = (await db_session.execute(select(SwitchLog))).scalars().all()
    assert len(logs) == 1
    assert logs[0].reason == "stale"


@pytest.mark.asyncio
async def test_switch_if_active_disabled_bypasses_service_flag(db_session, monkeypatch):
    """switch_if_active_disabled is triggered by an explicit user action;
    it must swap regardless of the service_enabled master toggle."""
    db_session.add(Setting(key="service_enabled", value="false"))
    db_session.add_all([
        _make_account(email="a@example.com", priority=0),
        _make_account(email="b@example.com", priority=1),
    ])
    await db_session.commit()

    monkeypatch.setattr(ac, "get_active_email", lambda: "a@example.com")
    swap_calls: list[str] = []
    monkeypatch.setattr(ac, "swap_to_account",
                        lambda email: swap_calls.append(email) or {})

    ws = _FakeWS()
    disabled = (await db_session.execute(
        select(Account).where(Account.email == "a@example.com")
    )).scalar_one()
    await sw.switch_if_active_disabled(disabled, db_session, ws)

    # Even with service_enabled=false, the swap runs — user intent wins.
    assert swap_calls == ["b@example.com"]


@pytest.mark.asyncio
async def test_switch_if_active_disabled_noop_when_account_inactive(
    db_session, monkeypatch
):
    """Disabling a non-active account is a no-op."""
    db_session.add(Setting(key="service_enabled", value="true"))
    db_session.add_all([
        _make_account(email="a@example.com", priority=0),
        _make_account(email="b@example.com", priority=1),
    ])
    await db_session.commit()

    monkeypatch.setattr(ac, "get_active_email", lambda: "a@example.com")
    swap_calls: list[str] = []
    monkeypatch.setattr(ac, "swap_to_account",
                        lambda email: swap_calls.append(email) or {})

    ws = _FakeWS()
    disabled = (await db_session.execute(
        select(Account).where(Account.email == "b@example.com")
    )).scalar_one()
    await sw.switch_if_active_disabled(disabled, db_session, ws)

    assert swap_calls == []
