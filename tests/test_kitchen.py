import pytest

from app.services import ServiceError, kitchen, orders
from conftest import open_and_order


def _item(db, key="naan"):
    kot = open_and_order(db, [(key, 1)])
    return orders.get_order(kot["order_id"])["items"][0]["item_id"]


def _status(item_id):
    from app.db import read_session
    from app.models import OrderItem

    with read_session() as s:
        return s.get(OrderItem, item_id).status


def test_happy_path_sets_timestamps_and_notifies_waiter(db, clock):
    item_id = _item(db)
    kitchen.start_item(item_id, "tandoor")
    clock.advance(minutes=12)
    data, events = kitchen.ready_item(item_id, "tandoor")
    waiter_event = next(e for e in events if e.channel == f"waiter:{db['staff']['waiter']}")
    assert waiter_event.data["table_number"] == 1
    assert waiter_event.data["name"] == "Butter Naan"
    kitchen.serve_item(item_id, db["staff"]["waiter"])
    assert _status(item_id) == "served"


@pytest.mark.parametrize("action", ["ready", "serve"])
def test_skipping_steps_rejected(db, action):
    item_id = _item(db)
    with pytest.raises(ServiceError):
        if action == "ready":
            kitchen.ready_item(item_id, "tandoor")
        else:
            kitchen.serve_item(item_id, db["staff"]["waiter"])
    assert _status(item_id) == "pending"


def test_going_backwards_rejected(db):
    item_id = _item(db)
    kitchen.start_item(item_id, "tandoor")
    kitchen.ready_item(item_id, "tandoor")
    with pytest.raises(ServiceError):
        kitchen.start_item(item_id, "tandoor")
    kitchen.serve_item(item_id, db["staff"]["waiter"])
    with pytest.raises(ServiceError):
        kitchen.serve_item(item_id, db["staff"]["waiter"])
    with pytest.raises(ServiceError):
        kitchen.cancel_item(item_id, "too late", db["staff"]["manager"])


def test_wrong_station_rejected(db):
    item_id = _item(db)
    with pytest.raises(ServiceError, match="tandoor"):
        kitchen.start_item(item_id, "kitchen")
    assert _status(item_id) == "pending"


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_cancel_needs_reason(db, reason):
    item_id = _item(db)
    with pytest.raises(ServiceError, match="reason"):
        kitchen.cancel_item(item_id, reason, db["staff"]["manager"])
    assert _status(item_id) == "pending"


def test_cancel_pending_by_waiter_ok(db):
    item_id = _item(db)
    kitchen.cancel_item(item_id, "guest changed mind", db["staff"]["waiter"])
    assert _status(item_id) == "cancelled"
    with pytest.raises(ServiceError):
        kitchen.cancel_item(item_id, "again", db["staff"]["manager"])


def test_cancel_after_kitchen_started_needs_manager(db):
    item_id = _item(db)
    kitchen.start_item(item_id, "tandoor")
    with pytest.raises(ServiceError, match="manager"):
        kitchen.cancel_item(item_id, "burnt", db["staff"]["waiter"])
    kitchen.cancel_item(item_id, "burnt", db["staff"]["manager"])
    assert _status(item_id) == "cancelled"


def test_live_items_oldest_first_with_age(db, clock):
    first = _item(db)
    clock.advance(minutes=5)
    kot = open_and_order(db, [("naan", 2), ("dal", 1)], table_index=1)
    clock.advance(minutes=3)
    board = kitchen.live_items("tandoor")
    assert [i["item_id"] for i in board][0] == first
    assert [i["age_minutes"] for i in board] == [8, 3]
    assert [i["table_number"] for i in board] == [1, 2]
    served_next = board[1]["item_id"]
    kitchen.start_item(served_next, "tandoor")
    kitchen.ready_item(served_next, "tandoor")
    kitchen.serve_item(served_next, db["staff"]["waiter"])
    assert [i["item_id"] for i in kitchen.live_items("tandoor")] == [first]
    assert len(kitchen.live_items("kitchen")) == 1
    assert kot["order_id"]
