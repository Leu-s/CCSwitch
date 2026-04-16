import json
import pytest
from unittest.mock import AsyncMock
from backend.ws import WebSocketManager


@pytest.mark.asyncio
async def test_broadcast_to_empty_set():
    manager = WebSocketManager()
    seq = await manager.broadcast({"type": "test"})
    assert seq == 1


@pytest.mark.asyncio
async def test_connect_disconnect():
    manager = WebSocketManager()
    ws = AsyncMock()
    await manager.connect(ws)
    assert ws in manager.active_connections
    manager.disconnect(ws)
    assert ws not in manager.active_connections


@pytest.mark.asyncio
async def test_seq_increments_per_broadcast():
    manager = WebSocketManager()
    s1 = await manager.broadcast({"type": "a"})
    s2 = await manager.broadcast({"type": "b"})
    s3 = await manager.broadcast({"type": "c"})
    assert s1 == 1
    assert s2 == 2
    assert s3 == 3


@pytest.mark.asyncio
async def test_seq_included_in_payload():
    manager = WebSocketManager()
    ws = AsyncMock()
    await manager.connect(ws)
    await manager.broadcast({"type": "x"})
    call_args = ws.send_text.call_args[0][0]
    data = json.loads(call_args)
    assert data["seq"] == 1
    assert data["type"] == "x"


@pytest.mark.asyncio
async def test_replay_since_returns_missed_events():
    manager = WebSocketManager()
    await manager.broadcast({"type": "a"})  # seq 1
    await manager.broadcast({"type": "b"})  # seq 2
    await manager.broadcast({"type": "c"})  # seq 3

    missed = manager.replay_since(1)
    assert missed is not None
    assert len(missed) == 2
    payloads = [json.loads(t) for t in missed]
    assert payloads[0]["seq"] == 2
    assert payloads[1]["seq"] == 3


@pytest.mark.asyncio
async def test_replay_since_zero_returns_all():
    manager = WebSocketManager()
    await manager.broadcast({"type": "a"})
    await manager.broadcast({"type": "b"})

    missed = manager.replay_since(0)
    assert missed is not None
    assert len(missed) == 2


@pytest.mark.asyncio
async def test_replay_since_returns_none_when_too_old():
    manager = WebSocketManager()
    # Fill the buffer with 100 events (BUFFER_SIZE)
    for i in range(100):
        await manager.broadcast({"type": "fill", "i": i})
    # Buffer now holds seq 1..100; asking for since=0 should return None
    # because oldest is seq 1 and 0 < 1-1 is False, so we get all.
    # Ask for something definitely before the buffer: since=-1 not meaningful,
    # let's broadcast one more to roll out seq 1.
    await manager.broadcast({"type": "extra"})  # seq 101, oldest in buffer is now 2
    result = manager.replay_since(0)
    # since=0, oldest=2, 0 < 2-1=1 is True → returns None
    assert result is None


@pytest.mark.asyncio
async def test_replay_since_empty_buffer_returns_empty():
    manager = WebSocketManager()
    result = manager.replay_since(5)
    assert result == []


# ── Per-client send timeout regression guards ────────────────────────
#
# A stuck browser tab (throttled, backgrounded, or with a wedged TCP
# write buffer) must NOT hold up the server.  Before the fix, a single
# slow client blocked the HTTP 409 response of every swap because
# ``perform_switch`` awaits ``ws.broadcast`` on the terminal-refresh
# path — one stuck send_text would hang that broadcast, which would
# hang perform_switch, which would hang the route handler, which would
# hang uvicorn's response to the /switch POST.  Live symptom: every
# swap request returned httpx.ReadTimeout after 15-30s.


@pytest.mark.asyncio
async def test_broadcast_disconnects_slow_client(monkeypatch):
    """A client whose send_text exceeds the per-send timeout is
    disconnected so subsequent broadcasts bypass it."""
    import asyncio
    from backend import ws as ws_module

    monkeypatch.setattr(ws_module, "_WS_SEND_TIMEOUT", 0.05)

    manager = WebSocketManager()
    slow = AsyncMock()

    async def stuck(_text):
        await asyncio.sleep(1.0)  # way past the 50 ms deadline

    slow.send_text = stuck
    await manager.connect(slow)

    import time
    t0 = time.monotonic()
    await manager.broadcast({"type": "x"})
    elapsed = time.monotonic() - t0

    assert elapsed < 0.5, (
        f"broadcast took {elapsed:.2f}s — timeout not enforced; "
        "a stuck client can block every swap response"
    )
    assert slow not in manager.active_connections, (
        "slow client must be removed so the next broadcast skips it"
    )


@pytest.mark.asyncio
async def test_broadcast_slow_client_does_not_block_fast_clients(monkeypatch):
    """With one slow client and one fast client, the fast client
    receives the message within the timeout window — parallel send."""
    import asyncio
    from backend import ws as ws_module

    monkeypatch.setattr(ws_module, "_WS_SEND_TIMEOUT", 0.1)

    manager = WebSocketManager()

    slow = AsyncMock()
    async def stuck(_text):
        await asyncio.sleep(5.0)
    slow.send_text = stuck

    fast = AsyncMock()
    fast_received: list[str] = []
    async def ok(text):
        fast_received.append(text)
    fast.send_text = ok

    await manager.connect(slow)
    await manager.connect(fast)

    await manager.broadcast({"type": "ping"})

    # Fast client got the message; slow client was disconnected.
    assert len(fast_received) == 1
    assert fast in manager.active_connections
    assert slow not in manager.active_connections


@pytest.mark.asyncio
async def test_broadcast_returns_seq_even_with_all_clients_slow(monkeypatch):
    """If every client is stuck, broadcast still returns its seq number
    (doesn't raise) and disconnects the stuck clients — the caller (e.g.
    ``perform_switch``) can proceed to return its HTTP response."""
    import asyncio
    from backend import ws as ws_module

    monkeypatch.setattr(ws_module, "_WS_SEND_TIMEOUT", 0.05)

    manager = WebSocketManager()
    stuck_clients = []
    for _ in range(3):
        c = AsyncMock()
        async def stuck(_text):
            await asyncio.sleep(1.0)
        c.send_text = stuck
        stuck_clients.append(c)
        await manager.connect(c)

    seq = await manager.broadcast({"type": "terminal_swap"})
    assert seq == 1
    for c in stuck_clients:
        assert c not in manager.active_connections
