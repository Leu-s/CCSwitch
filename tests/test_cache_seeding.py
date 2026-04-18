"""
Tests for cache seeding behaviour.

Covers ``cache.seed_usage()`` and the startup seeding logic in
``main.lifespan`` that reads persisted vault usage snapshots from
Account rows and populates the in-memory cache so the dashboard
shows data immediately on restart.
"""
import time

import pytest

from backend.cache import cache as _cache


@pytest.fixture(autouse=True)
async def _wipe_cache():
    _cache._usage.clear()
    _cache._token_info.clear()
    yield
    _cache._usage.clear()
    _cache._token_info.clear()


# ── seed_usage ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_usage_only_if_empty():
    """seed_usage does not overwrite existing cache data."""
    existing = {"five_hour": {"utilization": 99.0, "resets_at": 1}}
    await _cache.set_usage("a@example.com", existing)

    seed_data = {"five_hour": {"utilization": 0, "resets_at": 2}}
    await _cache.seed_usage("a@example.com", seed_data)

    cached = await _cache.get_usage_async("a@example.com")
    assert cached["five_hour"]["utilization"] == 99.0, (
        "seed_usage must not overwrite existing data"
    )
    assert cached["five_hour"]["resets_at"] == 1


@pytest.mark.asyncio
async def test_seed_usage_writes_when_empty():
    """seed_usage writes data when no cache entry exists."""
    seed_data = {"five_hour": {"utilization": 42.0, "resets_at": 100}}
    await _cache.seed_usage("new@example.com", seed_data)

    cached = await _cache.get_usage_async("new@example.com")
    assert cached["five_hour"]["utilization"] == 42.0
    assert cached["five_hour"]["resets_at"] == 100


@pytest.mark.asyncio
async def test_seed_expired_window_sets_zero():
    """Expired resets_at seeds cache with 0% utilization.

    The startup seeding logic in main.lifespan checks
    ``acct.last_five_hour_resets_at < now_epoch`` and writes
    ``utilization: 0`` rather than the stored (stale) value.
    This test validates the behaviour at the cache layer.
    """
    past_epoch = int(time.time()) - 3600
    seed_data = {"five_hour": {"utilization": 0, "resets_at": past_epoch}}
    await _cache.seed_usage("expired@example.com", seed_data)

    cached = await _cache.get_usage_async("expired@example.com")
    assert cached["five_hour"]["utilization"] == 0
    assert cached["five_hour"]["resets_at"] == past_epoch


@pytest.mark.asyncio
async def test_seed_open_window_preserves_utilization():
    """Open (future) resets_at seeds cache with the stored utilization
    value, not zero."""
    future_epoch = int(time.time()) + 3600
    seed_data = {"five_hour": {"utilization": 65.0, "resets_at": future_epoch}}
    await _cache.seed_usage("active@example.com", seed_data)

    cached = await _cache.get_usage_async("active@example.com")
    assert cached["five_hour"]["utilization"] == 65.0
    assert cached["five_hour"]["resets_at"] == future_epoch


@pytest.mark.asyncio
async def test_seed_with_seven_day():
    """seed_usage correctly stores both five_hour and seven_day data."""
    future_5h = int(time.time()) + 3600
    future_7d = int(time.time()) + 86400
    seed_data = {
        "five_hour": {"utilization": 20.0, "resets_at": future_5h},
        "seven_day": {"utilization": 5.0, "resets_at": future_7d},
    }
    await _cache.seed_usage("both@example.com", seed_data)

    cached = await _cache.get_usage_async("both@example.com")
    assert cached["five_hour"]["utilization"] == 20.0
    assert cached["seven_day"]["utilization"] == 5.0
    assert cached["seven_day"]["resets_at"] == future_7d
