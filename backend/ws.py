"""
WebSocket manager with sequence numbers and a bounded event replay buffer.

Every event broadcast gets a monotonically-increasing `seq` number.  The
manager keeps a bounded number of recent events so a reconnecting client can ask
for missed events by supplying the `?since=<seq>` query parameter on the
WebSocket handshake URL.

If the client's `since` value is:
  • 0 / missing → only the initial state snapshot is sent (normal connect)
  • N within the buffer  → events N+1 … current are replayed immediately
  • N older than the buffer → a full-state refresh flag is returned so the
    caller can send a snapshot instead of partial history

Sequence numbers are integers starting at 1.  `seq=0` is the sentinel for
"I have no events yet; send me the full state".
"""

import asyncio
import json
from collections import deque
from fastapi import WebSocket

from .config import settings as cfg


# Per-client send deadline.  A stuck browser tab (throttled, paused, or
# with a wedged TCP write buffer) must NOT hold up server operations
# that broadcast — e.g. ``perform_switch`` awaits a broadcast on every
# terminal swap and would otherwise block the HTTP 409 response for as
# long as the client takes to ack.  Any client whose send exceeds this
# deadline is considered dead and disconnected.
_WS_SEND_TIMEOUT = 3.0


class WebSocketManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self._seq: int = 0
        # deque of (seq, json_text) — bounded to cfg.ws_replay_buffer_size
        self._buffer: deque[tuple[int, str]] = deque(maxlen=cfg.ws_replay_buffer_size)

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, data: dict) -> int:
        """
        Broadcast an event to all connected clients.  Stamps the event with
        the next sequence number, stores it in the replay buffer, and
        returns the sequence number assigned.

        Each client receives the message in parallel with a per-send
        timeout of ``_WS_SEND_TIMEOUT`` seconds.  A stuck client cannot
        block the caller or the other clients; on timeout or any send
        exception it is disconnected so subsequent broadcasts skip it.
        """
        self._seq += 1
        seq = self._seq
        payload = {**data, "seq": seq}
        text = json.dumps(payload)
        self._buffer.append((seq, text))

        snapshot = list(self.active_connections)
        if not snapshot:
            return seq

        async def _send(conn: WebSocket) -> None:
            await asyncio.wait_for(conn.send_text(text), timeout=_WS_SEND_TIMEOUT)

        results = await asyncio.gather(
            *(_send(c) for c in snapshot), return_exceptions=True,
        )
        for conn, result in zip(snapshot, results):
            if isinstance(result, BaseException):
                self.disconnect(conn)

        return seq

    def replay_since(self, since: int) -> list[str] | None:
        """
        Return a list of JSON strings for events with seq > since.

        Returns None when the requested `since` is older than the oldest event
        in the buffer — the caller should send a full-state refresh instead.
        Returns an empty list when `since` equals the current seq (no new events).
        """
        if not self._buffer:
            return []

        oldest_seq = self._buffer[0][0]
        if since < oldest_seq - 1:
            # Too old — buffer doesn't cover the gap
            return None

        return [text for seq, text in self._buffer if seq > since]


ws_manager = WebSocketManager()
