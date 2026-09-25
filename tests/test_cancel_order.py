import pytest

from app.services import ServiceError, billing, kitchen, orders, tables
from conftest import open_and_order, serve_all


def _open(db, table_index=0):
    opened, _ = tables.open_table(db["tables"][table_index], db["staff"]["waiter"], 2)
    return opened["order_id"]


def _table(number):
    return next(t for t in tables.list_tables() if t["number"] == number)


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_cancel_order_needs_reason(db, reason):
    order_id = _open(db)
    with pytest.raises(ServiceError, match="reason"):
        orders.cancel_order(order_id, reason, db["staff"]["manager"])
    assert orders.get_order(order_id)["status"] == "open"


@pytest.mark.parametrize("who", ["waiter", "counter", "manager"])
def test_no_kot_order_cancellable_by_own_waiter_counter_or_manager(db, clock, who):
    order_id = _open(db)
    clock.advance(minutes=3)
    result, events = orders.cancel_order(order_id, "guests left", db["staff"][who])

    assert result["status"] == "cancelled"
    assert orders.get_order(order_id)["status"] == "cancelled"
    row = _table(1)
    assert row["status"] == "available"
    assert row["status_since"] == clock.current
    assert row["order_id"] is None
    assert {e.channel for e in events} == {"section:A", "counter"}


def test_no_kot_order_not_cancellable_by_other_waiter(db):
    order_id = _open(db)
    with pytest.raises(ServiceError, match="waiter"):
        orders.cancel_order(order_id, "guests left", db["staff"]["waiter2"])
    assert _table(1)["status"] == "occupied"


@pytest.mark.parametrize("who", ["waiter", "counter"])
def test_sent_order_needs_manager(db, who):
    kot = open_and_order(db, [("naan", 1)])
    with pytest.raises(ServiceError, match="manager"):
        orders.cancel_order(kot["order_id"], "guests left", db["staff"][who])
    order = orders.get_order(kot["order_id"])
    assert order["status"] == "open"
    assert [i["status"] for i in order["items"]] == ["pending"]


def test_manager_cancel_cancels_all_unserved_items_with_reason(db):
    kot = open_and_order(db, [("naan", 1), ("dal", 1), ("lassi", 1)])
    items = {i["station"]: i["item_id"] for i in orders.get_order(kot["order_id"])["items"]}
    kitchen.start_item(items["tandoor"], "tandoor")                       # preparing
    kitchen.start_item(items["kitchen"], "kitchen")
    kitchen.ready_item(items["kitchen"], "kitchen")                       # ready
    kitchen.cancel_item(items["bar"], "out of curd", db["staff"]["waiter"])  # already cancelled

    _, events = orders.cancel_order(kot["order_id"], "guests left", db["staff"]["manager"])

    order = orders.get_order(kot["order_id"])
    assert order["status"] == "cancelled"
    reasons = {i["station"]: (i["status"], i["cancel_reason"]) for i in order["items"]}
    assert reasons == {
        "tandoor": ("cancelled", "guests left"),
        "kitchen": ("cancelled", "guests left"),
        "bar": ("cancelled", "out of curd"),  # keeps its original reason
    }
    assert kitchen.live_items("tandoor") == [] and kitchen.live_items("kitchen") == []
    # Kitchen boards are told to drop the items; floor and counter see the free table
    assert {e.channel for e in events} == {"station:tandoor", "station:kitchen", "section:A", "counter"}
    assert _table(1)["status"] == "available"


def test_served_items_block_cancel(db):
    kot = open_and_order(db, [("naan", 1), ("dal", 1)])
    naan = next(i for i in orders.get_order(kot["order_id"])["items"] if i["name"] == "Butter Naan")
    kitchen.start_item(naan["item_id"], "tandoor")
    kitchen.ready_item(naan["item_id"], "tandoor")
    kitchen.serve_item(naan["item_id"], db["staff"]["waiter"])

    with pytest.raises(ServiceError, match="bill"):
        orders.cancel_order(kot["order_id"], "guests left", db["staff"]["manager"])
    # Nothing changed: the other item is still live, table still occupied
    order = orders.get_order(kot["order_id"])
    assert order["status"] == "open"
    assert sorted(i["status"] for i in order["items"]) == ["pending", "served"]
    assert _table(1)["status"] == "occupied"


def test_only_open_orders_can_be_cancelled(db):
    kot = open_and_order(db, [("naan", 1)])
    serve_all(db, kot["order_id"])
    billing.generate_bill(kot["order_id"], 0, db["staff"]["counter"])
    with pytest.raises(ServiceError, match="billed"):
        orders.cancel_order(kot["order_id"], "changed mind", db["staff"]["manager"])

    order_id = _open(db, table_index=1)
    orders.cancel_order(order_id, "guests left", db["staff"]["manager"])
    with pytest.raises(ServiceError, match="cancelled"):
        orders.cancel_order(order_id, "again", db["staff"]["manager"])


def test_table_can_be_reseated_after_cancel(db):
    order_id = _open(db)
    orders.cancel_order(order_id, "wrong table", db["staff"]["waiter"])
    reopened, _ = tables.open_table(db["tables"][0], db["staff"]["waiter"], 4)
    assert reopened["order_id"] != order_id
    assert _table(1)["order_id"] == reopened["order_id"]
