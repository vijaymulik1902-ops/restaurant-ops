"""Tables: seating guests and the floor overview."""
from sqlalchemy import and_, select

from app.db import now, read_session, write_session
from app.models import DiningTable, Order, Staff
from app.services import Event, ServiceError, minutes_since

LIVE_ORDER_STATUSES = ("open", "billed")


def table_events(table: DiningTable, order_id: int | None) -> list[Event]:
    """Events telling the floor and the counter that one table changed."""
    data = {
        "table_id": table.id,
        "number": table.number,
        "section": table.section,
        "status": table.status,
        "status_since": table.status_since.isoformat(),
        "order_id": order_id,
    }
    return [
        Event(f"section:{table.section}", "table", data),
        Event("counter", "table", data),
    ]


def get_active_staff(s, staff_id: int) -> Staff:
    """Load a staff member who is allowed to act, or raise ServiceError."""
    staff = s.get(Staff, staff_id)
    if staff is None or not staff.active:
        raise ServiceError("Unknown or inactive staff member")
    return staff


def open_table(table_id: int, waiter_id: int, guest_count: int) -> tuple[dict, list[Event]]:
    """Seat guests: create an open order and mark the table occupied.

    The table must be available. Returns ({"order_id", "table_number"}, events).
    """
    if not isinstance(guest_count, int) or guest_count < 1:
        raise ServiceError("Guest count must be at least 1")

    with write_session() as s:
        table = s.get(DiningTable, table_id)
        if table is None:
            raise ServiceError("Table not found")
        if table.status != "available":
            raise ServiceError(f"Table {table.number} is {table.status}, not available")
        get_active_staff(s, waiter_id)

        ts = now()
        order = Order(
            table_id=table.id, waiter_id=waiter_id, guest_count=guest_count,
            status="open", created_at=ts,
        )
        s.add(order)
        table.status = "occupied"
        table.status_since = ts
        s.flush()

        result = {"order_id": order.id, "table_number": table.number}
        events = table_events(table, order.id)
    return result, events


def list_tables(section: str | None = None) -> list[dict]:
    """All tables (optionally one section) with live order, waiter and time in current state."""
    stmt = (
        select(
            DiningTable.id, DiningTable.number, DiningTable.capacity, DiningTable.section,
            DiningTable.status, DiningTable.status_since,
            Order.id.label("order_id"), Order.guest_count, Staff.name.label("waiter_name"),
        )
        .outerjoin(
            Order,
            and_(Order.table_id == DiningTable.id, Order.status.in_(LIVE_ORDER_STATUSES)),
        )
        .outerjoin(Staff, Staff.id == Order.waiter_id)
        .order_by(DiningTable.number)
    )
    if section is not None:
        stmt = stmt.where(DiningTable.section == section)

    current = now()
    with read_session() as s:
        rows = s.execute(stmt).all()
    return [
        {
            "table_id": r.id,
            "number": r.number,
            "capacity": r.capacity,
            "section": r.section,
            "status": r.status,
            "status_since": r.status_since,
            "minutes_in_status": minutes_since(r.status_since, current),
            "order_id": r.order_id,
            "guest_count": r.guest_count,
            "waiter_name": r.waiter_name,
        }
        for r in rows
    ]
