"""
Event bus - async pub/sub for live StepEvents.

One publish fans out to:
  - the run channel  (run:{run_id})    -> the /run/{id} live view
  - the dept channel (dept:{department}) -> the admin dashboards

Each channel keeps a small replay buffer so a late-joining WebSocket gets recent history
before live updates. In-memory (single process) - fine for the demo; swap for Redis pub/sub
to scale horizontally.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import AsyncIterator

_BUFFER = 200


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=_BUFFER))

    async def publish(self, channel: str, event: dict) -> None:
        self._history[channel].append(event)
        for q in list(self._subscribers.get(channel, ())):
            q.put_nowait(event)

    async def publish_event(self, event: dict) -> None:
        """Fan a StepEvent to its run channel + department channel."""
        run_id = event.get("run_id")
        dept = event.get("department")
        if run_id:
            await self.publish(f"run:{run_id}", event)
        if dept:
            await self.publish(f"dept:{dept}", event)
        await self.publish("dept:ALL", event)        # company_admin firehose

    def history(self, channel: str) -> list[dict]:
        return list(self._history.get(channel, ()))

    async def subscribe(self, channel: str, replay: bool = True) -> AsyncIterator[dict]:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[channel].add(q)
        try:
            if replay:
                for ev in self.history(channel):
                    yield ev
            while True:
                yield await q.get()
        finally:
            self._subscribers[channel].discard(q)


bus = EventBus()
