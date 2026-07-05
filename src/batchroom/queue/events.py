"""In-process event bus feeding the SSE endpoint and the web UI.

Workers publish from threads via ``publish_threadsafe``; SSE subscribers
consume per-subscriber asyncio queues. Slow consumers drop oldest events
instead of blocking the queue (the UI refetches state on every nudge, so a
dropped event costs nothing).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time


class EventBus:
    def __init__(self, maxsize: int = 256):
        self._subscribers: set[asyncio.Queue] = set()
        self._maxsize = maxsize
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event_type: str, **data) -> None:
        event = {"type": event_type, "ts": time.time(), **data}
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()  # drop oldest
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    def publish_from_thread(self, event_type: str, data: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        def _do() -> None:
            self.publish(event_type, **data)
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_do)


def sse_format(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
