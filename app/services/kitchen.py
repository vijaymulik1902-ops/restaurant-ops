"""Kitchen: moving items through pending -> preparing -> ready -> served, and cancellations."""
from sqlalchemy import select
from sqlalchemy.orm import defer

from app.db import now, read_session, write_session
from app.models import STATIONS, DiningTable, Kot, Order, OrderItem
from app.services import Event, ServiceError, audit, clean_reason, minutes_since
from app.services.tables import get_active_staff, table_events

LIVE_ITEM_STATUSES = ("pending", "preparing", "ready")
FLOOR_ROLES = ("waiter", "counter", "manager")  # may serve items


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


# Kitchen never SELECTs cost; touching it by mistake raises instead of silently loading it
NO_COST = defer(OrderItem.unit_cost_paise, raiseload=True)


def _load(s, item_id: int) -> tuple[OrderItem, Order, DiningTable]:
    item = s.get(OrderItem, item_id, options=[NO_COST])
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
        floor = table_events(table, order.id)  # floor card turns red if food waits too long
    return data, [
        Event(f"station:{data['station']}", "item", data),
        Event(f"waiter:{waiter_id}", "item_ready", data),
        *floor,
    ]


def serve_item(item_id: int, waiter_id: int) -> tuple[dict, list[Event]]:
    """ready -> served, sets served_at. Any active floor staff member may carry the plate."""
    with write_session() as s:
        item, order, table = _load(s, item_id)
        if get_active_staff(s, waiter_id).role not in FLOOR_ROLES:
            raise ServiceError("Only floor staff can mark items served")
        _require_status(item, "ready", "serve")
        item.status = "served"
        item.served_at = now()
        data = _item_data(item, order, table)
        order_waiter_id = order.waiter_id
        floor = table_events(table, order.id)
    return data, [
        Event(f"station:{data['station']}", "item", data),
        Event(f"waiter:{order_waiter_id}", "item", data),
        *floor,
    ]


def _check_cancel_item_allowed(name: str, item_status: str, order_status: str, role: str,
                               staff_section: str | None, table_section: str) -> None:
    """Who may cancel an item, and when (CLAUDE.md access matrix). Shared by cancel_item
    and can_cancel_item.

    - pending: a waiter for tables in their own section, counter, manager
    - preparing/ready: manager only
    """
    if item_status in ("served", "cancelled"):
        raise ServiceError(f"Cannot cancel {name}: it is {item_status}")
    if order_status != "open":
        raise ServiceError(f"Order is {order_status}, items can no longer be cancelled")
    if item_status in ("preparing", "ready"):
        if role != "manager":
            raise ServiceError(f"{name} is already {item_status}; ask a manager to cancel")
    elif role == "waiter":
        if staff_section != table_section:
            raise ServiceError(f"Only section {table_section}'s waiter, counter or a manager can cancel")
    elif role not in ("counter", "manager"):
        raise ServiceError("Only floor staff can cancel items")


def can_cancel_item(item_status: str, order_status: str, role: str,
                    staff_section: str | None = None, table_section: str = "") -> bool:
    """Whether cancel_item would be allowed (for showing the cancel icon)."""
    try:
        _check_cancel_item_allowed("item", item_status, order_status, role, staff_section, table_section)
    except ServiceError:
        return False
    return True


def cancel_item(item_id: int, reason: str, by_staff_id: int) -> tuple[dict, list[Event]]:
    """Cancel an item that is not served yet. A reason is always required.

    Once the kitchen has started (preparing/ready), only a manager may cancel.
    The order must still be open (a billed order's total is fixed).
    """
    reason = clean_reason(reason)

    with write_session() as s:
        item, order, table = _load(s, item_id)
        staff = get_active_staff(s, by_staff_id)
        _check_cancel_item_allowed(item.name, item.status, order.status, staff.role,
                                   staff.section, table.section)
        audit.record(s, staff.id, audit.ITEM_CANCEL, "order_item", item.id,
                     old={"status": item.status, "name": item.name, "qty": item.qty,
                          "order_id": order.id, "table_number": table.number},
                     new={"status": "cancelled"}, reason=reason)
        item.status = "cancelled"
        item.cancel_reason = reason
        data = _item_data(item, order, table)
        waiter_id = order.waiter_id
        floor = table_events(table, order.id)
    return data, [
        Event(f"station:{data['station']}", "item", data),
        Event(f"waiter:{waiter_id}", "item", data),
        *floor,
    ]


def live_items(station: str, item_id: int | None = None) -> list[dict]:
    """Pending/preparing/ready items for one station, oldest first, with age in minutes.

    With `item_id`, returns just that item (or [] once it has left the board).
    """
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
    if item_id is not None:
        stmt = stmt.where(OrderItem.id == item_id)
    current = now()
    with read_session() as s:
        rows = s.execute(stmt).all()
    return [
        {
            "item_id": r.id, "order_id": r.order_id, "name": r.name, "qty": r.qty,
            "note": r.note, "status": r.status, "created_at": r.created_at,
            "ready_at": r.ready_at, "table_number": r.table_number,
            "kot_number": r.kot_number, "age_minutes": minutes_since(r.created_at, current),
            "age_seconds": max(0, int((current - r.created_at).total_seconds())),
        }
        for r in rows
    ]


def cooking_summary(station: str) -> list[dict]:
    """What the station has to cook, grouped by (dish name, note), oldest first.

    Read-only aggregate of pending + preparing items (ready/served/cancelled are excluded).
    Items with different notes are never merged. One query, whatever the number of tables.
    """
    if station not in STATIONS:
        raise ServiceError("Unknown station")
    stmt = (
        select(OrderItem.name, OrderItem.note, OrderItem.qty, OrderItem.status, OrderItem.created_at,
               DiningTable.number.label("table_number"))
        .join(Order, Order.id == OrderItem.order_id)
        .join(DiningTable, DiningTable.id == Order.table_id)
        .where(OrderItem.station == station, OrderItem.status.in_(("pending", "preparing")))
        .order_by(OrderItem.created_at, OrderItem.id)
    )
    current = now()
    with read_session() as s:
        rows = s.execute(stmt).all()

    lines: dict[tuple[str, str], dict] = {}
    for r in rows:  # oldest first, so the first row seen for a line is its oldest item
        key = (r.name, r.note or "")
        line = lines.get(key)
        if line is None:
            line = lines[key] = {"name": r.name, "note": r.note, "total_qty": 0, "to_start": 0, "cooking": 0,
                                 "tables": {}, "oldest_age_seconds": max(0, int((current - r.created_at).total_seconds()))}
        line["total_qty"] += r.qty
        line["to_start" if r.status == "pending" else "cooking"] += r.qty
        line["tables"][r.table_number] = line["tables"].get(r.table_number, 0) + r.qty
    result = []
    for line in lines.values():  # dict keeps first-seen (oldest-first) order
        line["tables"] = [{"number": n, "qty": q} for n, q in line["tables"].items()]
        result.append(line)
    return result


def kitchen_screen(station: str) -> dict:
    """Everything /kitchen renders: the tickets and the cooking summary above them. Read-only."""
    return {"items": live_items(station), "lines": cooking_summary(station)}
