"""In-process pub/sub hub for pushing live events to the webview.

Transfer progress, query completion and connection state all originate on
worker threads; they call ``hub.broadcast_threadsafe(...)`` and every connected
WebSocket receives the JSON.
"""

from __future__ import annotations

import asyncio
from typing import Any


class WebSocketHub:
    """Fan-out of backend events to every connected client."""

    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._lock = asyncio.Lock()
        #: Captured on startup so worker threads can hop onto the event loop.
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, event: str, payload: dict[str, Any] | None = None) -> None:
        message = {"event": event, "payload": payload or {}}
        async with self._lock:
            targets = list(self._clients)
        dead = []
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    def broadcast_threadsafe(
        self, event: str, payload: dict[str, Any] | None = None
    ) -> None:
        """Broadcast from a non-async worker thread.

        Transfers and queries run on plain threads, so they cannot await; this
        schedules the send on the event loop instead. Silently does nothing
        before the loop is bound (i.e. during import).
        """
        loop = self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(event, payload), loop)
        except RuntimeError:
            # Loop already closing during shutdown; dropping the event is fine.
            pass

    @property
    def client_count(self) -> int:
        return len(self._clients)


hub = WebSocketHub()
