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


def test_kitchen_and_cancel_paths_never_load_cost(db):
    """Cost is deferred with raiseload; the flows below would raise if they touched it."""
    from sqlalchemy import event

    from app.db import write_engine

    seen = []

    def spy(conn, cursor, statement, *args):
        if "unit_cost_paise" in statement and statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    event.listen(write_engine, "before_cursor_execute", spy)
    try:
        item_id = _item(db)
        kitchen.start_item(item_id, "tandoor")
        kitchen.ready_item(item_id, "tandoor")
        kitchen.serve_item(item_id, db["staff"]["waiter"])
        other = open_and_order(db, [("dal", 1)], table_index=1)
        dal = orders.get_order(other["order_id"])["items"][0]["item_id"]
        kitchen.cancel_item(dal, "no dal", db["staff"]["waiter"])
        third = open_and_order(db, [("naan", 1)], table_index=2)
        orders.cancel_order(third["order_id"], "left", db["staff"]["manager"])
    finally:
        event.remove(write_engine, "before_cursor_execute", spy)
    assert seen == []


@pytest.mark.parametrize("item_status, role, staff_section, allowed", [
    # Cancel pending item: waiter (own section), counter, manager; never chef
    ("pending", "waiter", "A", True), ("pending", "waiter", "B", False), ("pending", "waiter", None, False),
    ("pending", "counter", None, True), ("pending", "manager", None, True), ("pending", "chef", None, False),
    # Cancel preparing/ready item: manager only
    ("preparing", "waiter", "A", False), ("preparing", "counter", None, False), ("preparing", "manager", None, True),
    ("ready", "waiter", "A", False), ("ready", "counter", None, False), ("ready", "manager", None, True),
    ("served", "manager", None, False), ("cancelled", "manager", None, False),
])
def test_can_cancel_item_matches_access_matrix(item_status, role, staff_section, allowed):
    # The table is in section A
    assert kitchen.can_cancel_item(item_status, "open", role, staff_section, "A") is allowed


def test_waiter_cannot_cancel_pending_item_in_another_section(db):
    item_id = _item(db)  # table 1, section A
    with pytest.raises(ServiceError, match="section A"):
        kitchen.cancel_item(item_id, "Wrong item entered", db["staff"]["waiter2"])  # Sneha, section B
    assert _status(item_id) == "pending"
    kitchen.cancel_item(item_id, "Wrong item entered", db["staff"]["counter"])
    assert _status(item_id) == "cancelled"


def test_can_cancel_item_false_once_billed():
    assert kitchen.can_cancel_item("pending", "billed", "manager") is False


def test_chef_cannot_cancel_or_serve(db):
    item_id = _item(db)
    with pytest.raises(ServiceError, match="floor staff"):
        kitchen.cancel_item(item_id, "no", db["staff"]["chef_tandoor"])
    kitchen.start_item(item_id, "tandoor")
    kitchen.ready_item(item_id, "tandoor")
    with pytest.raises(ServiceError, match="floor staff"):
        kitchen.serve_item(item_id, db["staff"]["chef_tandoor"])
    assert _status(item_id) == "ready"


def test_cancelled_item_leaves_kitchen_board_with_event(db):
    item_id = _item(db)
    _, events = kitchen.cancel_item(item_id, "Out of stock", db["staff"]["waiter"])
    assert kitchen.live_items("tandoor") == []
    assert any(e.channel == "station:tandoor" and e.data["status"] == "cancelled" for e in events)


@pytest.mark.parametrize("finish", ["served", "cancelled"])
def test_pickup_slip_leaves_the_board_live(db, finish):
    """A ready item shows as a green pickup slip; serving or cancelling it sends a station event,
    and the card the kitchen page then re-fetches is empty, which removes the slip."""
    from test_routes import login

    item_id = _item(db)
    kitchen.start_item(item_id, "tandoor")
    kitchen.ready_item(item_id, "tandoor")
    chef = login("chef")
    slip = chef.get(f"/kitchen/items/{item_id}/card?station=tandoor").text
    assert "kcard st-ready" in slip and "waiting for pickup" in slip

    if finish == "served":
        _, events = kitchen.serve_item(item_id, db["staff"]["waiter"])
    else:
        _, events = kitchen.cancel_item(item_id, "Guest left", db["staff"]["manager"])
    assert any(e.channel == "station:tandoor" and e.type == "item" and e.data["item_id"] == item_id
               for e in events)
    card = chef.get(f"/kitchen/items/{item_id}/card?station=tandoor")
    assert card.status_code == 200 and card.text == ""
    assert "waiting for pickup" not in chef.get("/kitchen").text
