"""Async Server-Sent Events (SSE) broadcaster for real-time Web UI progress updates."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator


class EventBroker:
    """Fan-out async event broker broadcasting JSON SSE payloads to connected UI clients."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        payload = {"event": event_type, "data": data}
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for queue in list(self._subscribers):
            try:
                if queue.full():
                    # Drop oldest queued message for slow clients so latest progress % wins
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(payload)
            except Exception:
                dead.append(queue)
        for q in dead:
            self._subscribers.discard(q)

    async def subscribe(self, heartbeat_seconds: float = 15.0) -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                    event_name = msg["event"]
                    encoded = json.dumps(msg["data"], separators=(",", ":"))
                    yield f"event: {event_name}\ndata: {encoded}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            self._subscribers.discard(queue)
