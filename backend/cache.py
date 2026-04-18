import asyncio


class _UsageCache:
    """Thread-safe wrapper for in-memory usage and token-info caches.

    All public methods acquire the internal lock, so callers never access
    the raw dicts directly.  The _unlocked_ helpers are only for use inside
    already-locked code paths within this module.
    """

    def __init__(self):
        self._usage: dict[str, dict] = {}
        self._token_info: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    # ── Usage ──────────────────────────────────────────────────────────────

    async def set_usage(self, email: str, data: dict) -> None:
        async with self._lock:
            self._usage[email] = data

    async def snapshot(self) -> dict[str, dict]:
        """Return a shallow copy safe for iteration outside the lock."""
        async with self._lock:
            return dict(self._usage)

    def get_usage(self, email: str) -> dict:
        """Read without locking — safe only when called from already-locked code."""
        return self._usage.get(email, {})

    async def get_usage_async(self, email: str) -> dict:
        """Thread-safe read — acquires lock, safe to call from any context."""
        async with self._lock:
            return self._usage.get(email, {})

    # ── Token info ──────────────────────────────────────────────────────────

    async def set_token_info(self, email: str, data: dict) -> None:
        async with self._lock:
            self._token_info[email] = data

    def get_token_info(self, email: str) -> dict | None:
        """Read without locking — safe from within an already-locked context."""
        return self._token_info.get(email)

    async def get_token_info_async(self, email: str) -> dict | None:
        """Thread-safe read — acquires lock, safe to call from any context."""
        async with self._lock:
            return self._token_info.get(email)

    # ── Invalidation ──────────────────────────────────────────────────────

    async def invalidate(self, email: str) -> None:
        """Remove all cache entries for an account (e.g. on account delete)."""
        async with self._lock:
            self._usage.pop(email, None)
            self._token_info.pop(email, None)

    async def invalidate_token_info(self, email: str) -> None:
        """Remove only the token-info entry, preserving the last known usage."""
        async with self._lock:
            self._token_info.pop(email, None)

    async def seed_usage(self, email: str, data: dict) -> None:
        """Seed cache from DB on startup.

        Only writes if no fresher data exists — avoids overwriting
        a value that a poll cycle set between DB read and seed call.
        """
        async with self._lock:
            if email not in self._usage:
                self._usage[email] = data

    async def set_usage_error(
        self, email: str, err_str: str, is_rate_limited: bool,
        rl_data: dict | None = None,
    ) -> tuple[dict, str]:
        """Atomically update the cache for a failed probe.

        Returns (new_entry, final_err_str) — err_str may become 'Rate limited'.

        The ``rate_limited`` flag is set on the cache entry whenever
        ``is_rate_limited`` is True, regardless of whether prior usage data
        exists.  The auto-switch loop in ``switcher.maybe_auto_switch``
        reads this flag to decide whether to switch — losing it for accounts
        that have never returned usable data would silently break the switch.

        ``rl_data``: fresh rate-limit window data parsed from the 429
        response headers (see ``anthropic_api.parse_rate_limit_headers``).
        When provided alongside ``is_rate_limited=True``, the fresh window
        data is used directly (authoritative over anything cached).  When
        absent, prior window data is preserved if any — otherwise we fall
        back to recording just the error + flag.
        """
        async with self._lock:
            if is_rate_limited:
                if rl_data:
                    new_entry = {**rl_data, "rate_limited": True}
                    final_err = "Rate limited"
                else:
                    prev = self._usage.get(email, {})
                    if prev and "error" not in prev:
                        new_entry = {**prev, "rate_limited": True}
                        final_err = "Rate limited"
                    else:
                        new_entry = {"error": err_str, "rate_limited": True}
                        final_err = err_str
            else:
                new_entry = {"error": err_str}
                final_err = err_str
            self._usage[email] = new_entry
        return new_entry, final_err


cache = _UsageCache()
