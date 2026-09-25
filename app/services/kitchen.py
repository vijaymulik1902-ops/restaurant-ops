"""Kitchen: moving items through pending -> preparing -> ready -> served, and cancellations."""
from sqlalchemy import select

from app.db import now, read_session, write_session
from app.models import STATIONS, DiningTable, Kot, Order, OrderItem
from app.services import Event, ServiceError, minutes_since
from app.services.orders import clean_reason
from app.services.tables import get_active_staff

LIVE_ITEM_STATUSES = ("pending", "preparing", "ready")


def _item_data(item: OrderItem, order: Order, table: DiningTable) -> dict:
    """Small event payload for one item. Never includes cost."""
    return {
        "item_id": item.id,
        "order_id": order.id,
        "table_number": table.number,
        "name": item.name,
        "qty": item.qty,
        "station": item.station,
        "status": item.status,
    }


def _load(s, item_id: int) -> tuple[OrderItem, Order, DiningTable]:
    item = s.get(OrderItem, item_id)
    if item is None:
        raise ServiceError("Item not found")
    order = s.get(Order, item.order_id)
    table = s.get(DiningTable, order.table_id)
    return item, order, table


def _require_status(item: OrderItem, expected: str, action: str) -> None:
    if item.status != expected:
        raise ServiceError(f"Cannot {action} {item.name}: it is {item.status}")


def _require_station(item: OrderItem, station: str) -> None:
    if item.station != station:
        raise ServiceError(f"{item.name} belongs to the {item.station} station")


def start_item(item_id: int, station: str) -> tuple[dict, list[Event]]:
    """pending -> preparing. Only the item's own station may start it."""
    with write_session() as s:
        item, order, table = _load(s, item_id)
        _require_station(item, station)
        _require_status(item, "pending", "start")
        item.status = "preparing"
        data = _item_data(item, order, table)
    return data, [Event(f"station:{data['station']}", "item", data)]


def ready_item(item_id: int, station: str) -> tuple[dict, list[Event]]:
    """preparing -> ready, sets ready_at and tells the order's waiter to pick it up."""
    with write_session() as s:
        item, order, table = _load(s, item_id)
        _require_station(item, station)
        _require_status(item, "preparing", "mark ready")
        item.status = "ready"
        item.ready_at = now()
        data = _item_data(item, order, table)
        waiter_id = order.waiter_id
    return data, [
        Event(f"station:{data['station']}", "item", data),
        Event(f"waiter:{waiter_id}", "item_ready", data),
    ]


def serve_item(item_id: int, waiter_id: int) -> tuple[dict, list[Event]]:
    """ready -> served, sets served_at. Any active waiter may carry the plate."""
    with write_session() as s:
        item, order, table = _load(s, item_id)
        get_active_staff(s, waiter_id)
        _require_status(item, "ready", "serve")
        item.status = "served"
        item.served_at = now()
        data = _item_data(item, order, table)
        order_waiter_id = order.waiter_id
    return data, [
        Event(f"station:{data['station']}", "item", data),
        Event(f"waiter:{order_waiter_id}", "item", data),
    ]


def cancel_item(item_id: int, reason: str, by_staff_id: int) -> tuple[dict, list[Event]]:
    """Cancel an item that is not served yet. A reason is always required.

    Once the kitchen has started (preparing/ready), only a manager may cancel.
    The order must still be open (a billed order's total is fixed).
    """
    reason = clean_reason(reason)

    with write_session() as s:
        item, order, table = _load(s, item_id)
        staff = get_active_staff(s, by_staff_id)
        if item.status in ("served", "cancelled"):
            raise ServiceError(f"Cannot cancel {item.name}: it is {item.status}")
        if order.status != "open":
            raise ServiceError(f"Order is {order.status}, items can no longer be cancelled")
        if item.status in ("preparing", "ready") and staff.role != "manager":
            raise ServiceError(f"{item.name} is already {item.status}; ask a manager to cancel")
        item.status = "cancelled"
        item.cancel_reason = reason
        data = _item_data(item, order, table)
        waiter_id = order.waiter_id
    return data, [
        Event(f"station:{data['station']}", "item", data),
        Event(f"waiter:{waiter_id}", "item", data),
        Event("counter", "item", data),
    ]


def live_items(station: str) -> list[dict]:
    """Pending/preparing/ready items for one station, oldest first, with age in minutes."""
    if station not in STATIONS:
        raise ServiceError("Unknown station")
    stmt = (
        select(
            OrderItem.id, OrderItem.order_id, OrderItem.name, OrderItem.qty, OrderItem.note,
            OrderItem.status, OrderItem.created_at, OrderItem.ready_at,
            DiningTable.number.label("table_number"), Kot.number.label("kot_number"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(DiningTable, DiningTable.id == Order.table_id)
        .join(Kot, Kot.id == OrderItem.kot_id)
        .where(OrderItem.station == station, OrderItem.status.in_(LIVE_ITEM_STATUSES))
        .order_by(OrderItem.created_at, OrderItem.id)
    )
    current = now()
    with read_session() as s:
        rows = s.execute(stmt).all()
    return [
        {
            "item_id": r.id, "order_id": r.order_id, "name": r.name, "qty": r.qty,
            "note": r.note, "status": r.status, "created_at": r.created_at,
            "ready_at": r.ready_at, "table_number": r.table_number,
            "kot_number": r.kot_number, "age_minutes": minutes_since(r.created_at, current),
        }
        for r in rows
    ]
