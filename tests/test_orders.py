import pytest
from sqlalchemy import func, select

from app.db import read_session
from app.models import Kot, OrderItem
from app.services import ServiceError, orders, tables
from conftest import new_kot_id, open_and_order


def _open(ids, table_index=0):
    opened, _ = tables.open_table(ids["tables"][table_index], ids["staff"]["waiter"], 2)
    return opened["order_id"]


def _has_cost_key(value) -> bool:
    if isinstance(value, dict):
        return any("cost" in k or _has_cost_key(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_cost_key(v) for v in value)
    return False


def test_same_kot_id_twice_creates_one_kot(db):
    order_id = _open(db)
    kot_id = new_kot_id()
    lines = [(db["menu"]["naan"], 2, None)]
    first, first_events = orders.send_kot(order_id, kot_id, db["staff"]["waiter"], lines, 1)
    # A retry carries the old version; it must still succeed as a no-op
    again, again_events = orders.send_kot(order_id, kot_id, db["staff"]["waiter"], lines, 1)

    assert again["kot_id"] == first["kot_id"] and again["kot_number"] == first["kot_number"]
    assert again["duplicate"] is True and first["duplicate"] is False
    assert first_events and again_events == []
    with read_session() as s:
        assert s.scalar(select(func.count(Kot.id))) == 1
        assert s.scalar(select(func.count(OrderItem.id))) == 1


def test_kot_id_from_another_order_rejected(db):
    kot = open_and_order(db, [("naan", 1)])
    other_order = _open(db, table_index=1)
    with pytest.raises(ServiceError):
        orders.send_kot(other_order, kot["kot_id"], db["staff"]["waiter"],
                        [(db["menu"]["dal"], 1, None)], 1)


def test_stale_version_rejected(db):
    order_id = _open(db)
    first, _ = orders.send_kot(order_id, new_kot_id(), db["staff"]["waiter"],
                               [(db["menu"]["naan"], 1, None)], 1)
    assert first["order_version"] == 2
    with pytest.raises(ServiceError, match="Order changed, reload"):
        orders.send_kot(order_id, new_kot_id(), db["staff"]["waiter"],
                        [(db["menu"]["dal"], 1, None)], 1)
    assert len(orders.get_order(order_id)["items"]) == 1


def test_unavailable_item_and_bad_qty_rejected(db):
    order_id = _open(db)
    waiter = db["staff"]["waiter"]
    with pytest.raises(ServiceError, match="not available"):
        orders.send_kot(order_id, new_kot_id(), waiter, [(db["menu"]["off"], 1, None)], 1)
    for bad_qty in (0, -1):
        with pytest.raises(ServiceError, match="Quantity"):
            orders.send_kot(order_id, new_kot_id(), waiter, [(db["menu"]["naan"], bad_qty, None)], 1)
    with pytest.raises(ServiceError):
        orders.send_kot(order_id, new_kot_id(), waiter, [], 1)
    # Nothing was written by the rejected attempts
    assert orders.get_order(order_id)["items"] == []
    assert orders.get_order(order_id)["version"] == 1


def test_price_and_cost_snapshotted_on_order_lines(db):
    kot = open_and_order(db, [("naan", 2), ("dal", 1)])
    with read_session() as s:
        rows = s.execute(
            select(OrderItem.name, OrderItem.station, OrderItem.unit_price_paise,
                   OrderItem.unit_cost_paise).where(OrderItem.order_id == kot["order_id"])
            .order_by(OrderItem.id)
        ).all()
    assert [tuple(r) for r in rows] == [
        ("Butter Naan", "tandoor", 4500, 1200),
        ("Dal Tadka", "kitchen", 18000, 4500),
    ]


def test_kot_events_per_station_and_counter_without_cost(db):
    order_id = _open(db)
    lines = [(db["menu"]["naan"], 1, "extra butter"), (db["menu"]["dal"], 1, None),
             (db["menu"]["lassi"], 2, None)]
    _, events = orders.send_kot(order_id, new_kot_id(), db["staff"]["waiter"], lines, 1)
    channels = sorted(e.channel for e in events)
    assert channels == ["counter", "station:bar", "station:kitchen", "station:tandoor"]
    assert not any(_has_cost_key(e.data) for e in events)


def test_kot_numbers_continue_past_midnight_and_reset_at_business_day_start(db, clock):
    order_id = _open(db)
    waiter = db["staff"]["waiter"]

    def send(version):
        kot, _ = orders.send_kot(order_id, new_kot_id(), waiter, [(db["menu"]["naan"], 1, None)], version)
        return kot["kot_number"]

    assert send(1) == 1                        # 12:00, 25 Sep
    clock.advance(hours=11, minutes=50)
    assert send(2) == 2                        # 23:50, 25 Sep
    clock.advance(minutes=20)
    assert send(3) == 3                        # 00:10, 26 Sep -> same business day
    clock.advance(hours=3, minutes=49)
    assert send(4) == 4                        # 03:59, 26 Sep -> same business day
    clock.advance(minutes=1)
    assert send(5) == 1                        # 04:00, 26 Sep -> new business day


def test_business_day_helpers():
    from datetime import date, datetime

    from app.services import business_day_bounds, business_day_of

    assert business_day_bounds(date(2026, 9, 25)) == (
        datetime(2026, 9, 25, 4, 0), datetime(2026, 9, 26, 4, 0))
    assert business_day_of(datetime(2026, 9, 26, 0, 15)) == date(2026, 9, 25)
    assert business_day_of(datetime(2026, 9, 26, 3, 59, 59)) == date(2026, 9, 25)
    assert business_day_of(datetime(2026, 9, 26, 4, 0)) == date(2026, 9, 26)


def test_get_order_running_total_excludes_cancelled_and_cost(db):
    from app.services import kitchen

    kot = open_and_order(db, [("naan", 2), ("dal", 1)])
    order = orders.get_order(kot["order_id"])
    assert order["total_paise"] == 2 * 4500 + 18000
    dal = next(i for i in order["items"] if i["name"] == "Dal Tadka")
    kitchen.cancel_item(dal["item_id"], "guest changed mind", db["staff"]["waiter"])
    order = orders.get_order(kot["order_id"])
    assert order["total_paise"] == 2 * 4500
    assert not _has_cost_key(order)
