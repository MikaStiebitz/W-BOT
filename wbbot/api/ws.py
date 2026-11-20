"""WebSocket hub: one broadcast channel for status, progress and live drawing.

Machine events arrive on a synchronous callback from the driver's reader loop.
They are put on a bounded queue and drained by a single writer task, so a slow
or dead browser tab cannot block the control path — the worst it can do is drop
its own status frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["Hub", "Client"]

#: Frames buffered per client before the oldest are dropped.
CLIENT_QUEUE_SIZE = 256


@dataclass
class Client:
    """One connected browser."""

    websocket: Any
    queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE))
    dropped: int = 0
    connected_at: float = field(default_factory=time.time)

    def offer(self, message: str) -> None:
        """Non-blocking send; drops the oldest frame when the client lags.

        Status updates are worthless once stale, so dropping is strictly
        better than blocking the machine's event loop on a slow socket.
        """
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
                self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(message)


class Hub:
    """Fan-out of machine events to every connected client."""

    def __init__(self) -> None:
        self.clients: list[Client] = []
        self._lock = asyncio.Lock()

    async def add(self, websocket) -> Client:
        client = Client(websocket=websocket)
        async with self._lock:
            self.clients.append(client)
        log.info("websocket client connected (%d total)", len(self.clients))
        return client

    async def remove(self, client: Client) -> None:
        async with self._lock:
            if client in self.clients:
                self.clients.remove(client)
        log.info("websocket client disconnected (%d left, %d frames dropped)",
                 len(self.clients), client.dropped)

    def broadcast(self, kind: str, payload: dict | None = None) -> None:
        """Queue a frame for every client. Safe to call from any callback."""
        if not self.clients:
            return
        try:
            message = json.dumps({"kind": kind, "payload": payload or {},
                                  "t": time.time()}, default=str)
        except (TypeError, ValueError):     # pragma: no cover - defensive
            log.exception("could not serialise a %s event", kind)
            return
        for client in list(self.clients):
            client.offer(message)

    async def pump(self, client: Client) -> None:
        """Writer task: drain one client's queue onto its socket."""
        try:
            while True:
                message = await client.queue.get()
                await client.websocket.send_text(message)
        except asyncio.CancelledError:      # pragma: no cover
            raise
        except Exception as exc:
            log.debug("websocket pump ended: %s", exc)

    @property
    def client_count(self) -> int:
        return len(self.clients)
