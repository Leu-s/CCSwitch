import pytest
from backend.ws import WebSocketManager

@pytest.mark.asyncio
async def test_broadcast_to_empty_set():
    manager = WebSocketManager()
    await manager.broadcast({"type": "test"})

@pytest.mark.asyncio
async def test_connect_disconnect():
    from unittest.mock import AsyncMock
    manager = WebSocketManager()
    ws = AsyncMock()
    await manager.connect(ws)
    assert ws in manager.active_connections
    manager.disconnect(ws)
    assert ws not in manager.active_connections
