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
