"""In-memory event broadcaster for SSE (single Uvicorn worker only).

All subscriber bookkeeping happens on the event loop thread. publish() may be
called from any thread (sync route handlers run in a thread pool); it hands the
work to the loop with call_soon_threadsafe.

A subscriber whose queue fills up is dropped: its queue is emptied and gets a
single CLOSE marker, the SSE response ends, and the browser reconnects and
reloads full state from the DB.
"""
import asyncio
from collections.abc import Iterable

from app.services import Event

QUEUE_SIZE = 100
CLOSE = None  # marker telling a stream to end


class Broadcaster:
    def __init__(self) -> None:
        self._by_channel: dict[str, set[asyncio.Queue]] = {}
        self._channels_of: dict[asyncio.Queue, tuple[str, ...]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self, channels: Iterable[str]) -> asyncio.Queue:
        """Register a new subscriber. Must be called on the event loop."""
        self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        chans = tuple(dict.fromkeys(channels))
        self._channels_of[queue] = chans
        for ch in chans:
            self._by_channel.setdefault(ch, set()).add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Forget a subscriber (safe to call twice). Must be called on the event loop."""
        for ch in self._channels_of.pop(queue, ()):
            subs = self._by_channel.get(ch)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    del self._by_channel[ch]

    def subscriber_count(self) -> int:
        return len(self._channels_of)

    def publish(self, events: Iterable[Event]) -> None:
        """Queue events for delivery. Safe from any thread; no-op with no subscribers."""
        events = list(events)
        loop = self._loop
        if not events or loop is None or loop.is_closed() or not self._channels_of:
            return
        loop.call_soon_threadsafe(self._deliver, events)

    def _deliver(self, events: list[Event]) -> None:
        for ev in events:
            for queue in list(self._by_channel.get(ev.channel, ())):
                try:
                    queue.put_nowait(ev)
                except asyncio.QueueFull:
                    self._drop(queue)

    def _drop(self, queue: asyncio.Queue) -> None:
        self.unsubscribe(queue)
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(CLOSE)


broadcaster = Broadcaster()


def subscribe(channels: Iterable[str]) -> asyncio.Queue:
    return broadcaster.subscribe(channels)


def unsubscribe(queue: asyncio.Queue) -> None:
    broadcaster.unsubscribe(queue)


def publish(events: Iterable[Event]) -> None:
    broadcaster.publish(events)
