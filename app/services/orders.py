"""Orders: sending KOTs to the kitchen and reading an order back."""
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from app.db import now, read_session, write_session
from app.models import DiningTable, Kot, MenuItem, Order, OrderItem, Staff
from app.services import Event, ServiceError, business_day_bounds, business_day_of
from app.services.tables import get_active_staff, table_events

MAX_NOTE_LEN = 120
MAX_QTY = 99
MAX_REASON_LEN = 120


def _bump_version(order: Order) -> None:
    """Force an UPDATE of the orders row so SQLAlchemy increments `version`.

    Adding items only inserts order_items rows; the order row itself is untouched,
    so without this a stale order screen could still send a KOT on the old version.
    """
    flag_modified(order, "status")


def clean_reason(reason: str | None) -> str:
    """A non-empty, trimmed cancel reason, or ServiceError."""
    reason = (reason or "").strip()
    if not reason:
        raise ServiceError("A reason is required to cancel")
    if len(reason) > MAX_REASON_LEN:
        raise ServiceError(f"Reason is too long (max {MAX_REASON_LEN} characters)")
    return reason


def _validate_lines(items: list[tuple[int, int, str | None]]) -> list[tuple[int, int, str | None]]:
    """Check quantities and notes from the client; return cleaned lines."""
    if not items:
        raise ServiceError("Add at least one item")
    cleaned = []
    for menu_item_id, qty, note in items:
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
            raise ServiceError("Quantity must be at least 1")
        if qty > MAX_QTY:
            raise ServiceError(f"Quantity cannot exceed {MAX_QTY}")
        note = (note or "").strip() or None
        if note and len(note) > MAX_NOTE_LEN:
            raise ServiceError(f"Note is too long (max {MAX_NOTE_LEN} characters)")
        cleaned.append((menu_item_id, qty, note))
    return cleaned


def _kot_result(kot: Kot, order: Order, duplicate: bool) -> dict:
    return {
        "kot_id": kot.id,
        "kot_number": kot.number,
        "order_id": order.id,
        "order_version": order.version,
        "duplicate": duplicate,
    }


def send_kot(
    order_id: int,
    kot_id: str,
    waiter_id: int,
    items: list[tuple[int, int, str | None]],
    expected_version: int,
) -> tuple[dict, list[Event]]:
    """Send a batch of items to the kitchen as one KOT.

    Idempotent: resending an existing kot_id for the same order returns that KOT
    with no changes and no events. Otherwise the order must be open and at
    `expected_version`. Name, station, price and cost are snapshotted from the menu.
    Returns (kot info, events for each station involved + counter).
    """
    if not kot_id:
        raise ServiceError("Missing KOT id, reload the order screen")

    with write_session() as s:
        existing = s.get(Kot, kot_id)
        if existing is not None:
            if existing.order_id != order_id:
                raise ServiceError("KOT id belongs to another order, reload")
            order = s.get(Order, order_id)
            return _kot_result(existing, order, duplicate=True), []

        order = s.get(Order, order_id)
        if order is None:
            raise ServiceError("Order not found")
        if order.status != "open":
            raise ServiceError(f"Order is {order.status}, cannot add items")
        if order.version != expected_version:
            raise ServiceError("Order changed, reload")
        get_active_staff(s, waiter_id)

        lines = _validate_lines(items)
        menu_ids = {menu_item_id for menu_item_id, _, _ in lines}
        menu = {m.id: m for m in s.scalars(select(MenuItem).where(MenuItem.id.in_(menu_ids)))}
        for menu_item_id in menu_ids:
            m = menu.get(menu_item_id)
            if m is None:
                raise ServiceError("Unknown menu item")
            if not m.available:
                raise ServiceError(f"{m.name} is not available")

        ts = now()
        start, end = business_day_bounds(business_day_of(ts))
        last_number = s.scalar(
            select(func.max(Kot.number)).where(Kot.created_at >= start, Kot.created_at < end)
        )
        kot = Kot(id=kot_id, number=(last_number or 0) + 1, order_id=order.id,
                  waiter_id=waiter_id, created_at=ts)
        s.add(kot)

        new_items: list[OrderItem] = []
        for menu_item_id, qty, note in lines:
            m = menu[menu_item_id]
            item = OrderItem(
                order_id=order.id, kot_id=kot.id, menu_item_id=m.id,
                name=m.name, station=m.station, qty=qty,
                unit_price_paise=m.price_paise, unit_cost_paise=m.cost_paise,
                note=note, status="pending", created_at=ts,
            )
            s.add(item)
            new_items.append(item)

        _bump_version(order)
        s.flush()

        table = s.get(DiningTable, order.table_id)
        events = _kot_events(kot, order, table, new_items)
        result = _kot_result(kot, order, duplicate=False)
    return result, events


def _kot_events(kot: Kot, order: Order, table: DiningTable, items: list[OrderItem]) -> list[Event]:
    """One event per station with that station's new items, plus one for the counter."""
    by_station: dict[str, list[dict]] = {}
    for it in items:
        by_station.setdefault(it.station, []).append(
            {"item_id": it.id, "name": it.name, "qty": it.qty, "note": it.note,
             "status": it.status, "created_at": it.created_at.isoformat()}
        )
    base = {"kot_number": kot.number, "order_id": order.id, "table_number": table.number}
    events = [
        Event(f"station:{station}", "kot", {**base, "items": station_items})
        for station, station_items in sorted(by_station.items())
    ]
    events.append(Event("counter", "order", {**base, "order_version": order.version}))
    return events


def cancel_order(order_id: int, reason: str, by_staff_id: int) -> tuple[dict, list[Event]]:
    """Cancel an open order and free its table.

    - No KOT sent yet: the order's own waiter, counter or manager may cancel.
    - Something was sent to the kitchen: manager only, and every item not yet
      cancelled is cancelled with the same reason. If anything was served the
      order cannot be cancelled; it must be billed instead.
    """
    reason = clean_reason(reason)

    with write_session() as s:
        order = s.get(Order, order_id)
        if order is None:
            raise ServiceError("Order not found")
        if order.status != "open":
            raise ServiceError(f"Order is {order.status}, cannot cancel")
        staff = get_active_staff(s, by_staff_id)

        has_kots = s.scalar(select(Kot.id).where(Kot.order_id == order.id).limit(1)) is not None
        if not has_kots:
            if staff.id != order.waiter_id and staff.role not in ("counter", "manager"):
                raise ServiceError("Only the order's waiter, counter or a manager can cancel")
        elif staff.role != "manager":
            raise ServiceError("Items were sent to the kitchen; ask a manager to cancel")

        items = list(s.scalars(select(OrderItem).where(OrderItem.order_id == order.id)))
        if any(it.status == "served" for it in items):
            raise ServiceError("Some items were served; bill the order instead")

        table = s.get(DiningTable, order.table_id)
        events: list[Event] = []
        for it in items:
            if it.status != "cancelled":
                it.status = "cancelled"
                it.cancel_reason = reason
                events.append(Event(f"station:{it.station}", "item", {
                    "item_id": it.id, "order_id": order.id, "table_number": table.number,
                    "name": it.name, "qty": it.qty, "station": it.station, "status": it.status,
                }))

        ts = now()
        order.status = "cancelled"
        order.closed_at = ts
        table.status = "available"
        table.status_since = ts
        s.flush()

        events.extend(table_events(table, None))
        result = {"order_id": order.id, "table_number": table.number, "status": order.status}
    return result, events


def get_order(order_id: int) -> dict:
    """An order with its items and running total (prices only, never cost).

    The running total counts every item that is not cancelled.
    """
    with read_session() as s:
        header = s.execute(
            select(
                Order.id, Order.status, Order.version, Order.guest_count, Order.created_at,
                Order.waiter_id, Staff.name.label("waiter_name"),
                DiningTable.id.label("table_id"), DiningTable.number.label("table_number"),
            )
            .join(DiningTable, DiningTable.id == Order.table_id)
            .join(Staff, Staff.id == Order.waiter_id)
            .where(Order.id == order_id)
        ).one_or_none()
        if header is None:
            raise ServiceError("Order not found")
        rows = s.execute(
            select(
                OrderItem.id, OrderItem.name, OrderItem.station, OrderItem.qty,
                OrderItem.unit_price_paise, OrderItem.note, OrderItem.status,
                OrderItem.cancel_reason, OrderItem.created_at, Kot.number.label("kot_number"),
            )
            .join(Kot, Kot.id == OrderItem.kot_id)
            .where(OrderItem.order_id == order_id)
            .order_by(OrderItem.id)
        ).all()

    items = [
        {
            "item_id": r.id, "name": r.name, "station": r.station, "qty": r.qty,
            "unit_price_paise": r.unit_price_paise,
            "line_total_paise": r.unit_price_paise * r.qty,
            "note": r.note, "status": r.status, "cancel_reason": r.cancel_reason,
            "created_at": r.created_at, "kot_number": r.kot_number,
        }
        for r in rows
    ]
    total = sum(i["line_total_paise"] for i in items if i["status"] != "cancelled")
    return {
        "order_id": header.id, "status": header.status, "version": header.version,
        "guest_count": header.guest_count, "created_at": header.created_at,
        "waiter_id": header.waiter_id, "waiter_name": header.waiter_name,
        "table_id": header.table_id, "table_number": header.table_number,
        "items": items, "total_paise": total,
    }
