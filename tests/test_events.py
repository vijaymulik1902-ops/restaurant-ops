import asyncio
import threading

from app.auth import CurrentStaff
from app.events import CLOSE, QUEUE_SIZE, Broadcaster
from app.routers.stream import channels_for
from app.services import Event


async def _settle():
    """Let call_soon_threadsafe callbacks run."""
    for _ in range(3):
        await asyncio.sleep(0)


def _drain(queue: asyncio.Queue) -> list:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_publish_reaches_only_subscribers_of_that_channel():
    async def scenario():
        b = Broadcaster()
        kitchen = b.subscribe(["station:kitchen"])
        bar = b.subscribe(["station:bar"])
        counter = b.subscribe(["counter", "section:A"])
        ev = Event("station:kitchen", "item", {"item_id": 1})
        b.publish([ev])
        await _settle()
        return _drain(kitchen), _drain(bar), _drain(counter)

    kitchen, bar, counter = asyncio.run(scenario())
    assert kitchen == [Event("station:kitchen", "item", {"item_id": 1})]
    assert bar == [] and counter == []


def test_publish_from_another_thread():
    async def scenario():
        b = Broadcaster()
        q = b.subscribe(["counter"])
        t = threading.Thread(target=b.publish, args=([Event("counter", "bill", {"bill_id": 7})],))
        t.start()
        t.join()
        await _settle()
        return _drain(q)

    assert asyncio.run(scenario()) == [Event("counter", "bill", {"bill_id": 7})]


def test_full_queue_drops_only_that_subscriber():
    async def scenario():
        b = Broadcaster()
        slow = b.subscribe(["counter"])
        fast = b.subscribe(["counter"])
        received = []
        for i in range(QUEUE_SIZE + 5):
            b.publish([Event("counter", "table", {"n": i})])
            await _settle()
            received.extend(_drain(fast))  # the fast client keeps up
        return b, slow, received

    b, slow, received = asyncio.run(scenario())
    assert len(received) == QUEUE_SIZE + 5
    # The slow one got a single CLOSE marker, telling its stream to end and reconnect
    assert _drain(slow) == [CLOSE]
    assert b.subscriber_count() == 1


def test_unsubscribe_stops_delivery():
    async def scenario():
        b = Broadcaster()
        q = b.subscribe(["counter"])
        b.unsubscribe(q)
        b.unsubscribe(q)  # harmless twice
        b.publish([Event("counter", "table", {})])
        await _settle()
        return b, _drain(q)

    b, got = asyncio.run(scenario())
    assert got == [] and b.subscriber_count() == 0


def test_channels_by_role():
    sections = ["A", "B"]
    waiter = CurrentStaff(1, "Rahul", "waiter", "A", None)
    assert channels_for(waiter, sections) == ["section:A", "waiter:1"]
    assert channels_for(waiter, sections, all_sections=True) == ["section:A", "section:B", "waiter:1"]
    chef = CurrentStaff(2, "Suresh", "chef", None, "tandoor")
    assert channels_for(chef, sections) == ["station:tandoor"]
    counter = CurrentStaff(3, "Counter", "counter", None, None)
    assert channels_for(counter, sections) == ["counter", "section:A", "section:B"]
    manager = CurrentStaff(4, "Manager", "manager", None, None)
    assert set(channels_for(manager, sections)) >= {"counter", "section:A", "station:bar"}
