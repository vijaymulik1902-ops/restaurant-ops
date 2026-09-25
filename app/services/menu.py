"""Menu as seen by floor and kitchen: prices for the order screen, never cost."""
from sqlalchemy import select
from sqlalchemy.orm import defer

from app.db import read_session, write_session
from app.models import DiningTable, MenuItem
from app.services import Event, ServiceError
from app.services.tables import get_active_staff


def list_menu() -> list[dict]:
    """All dishes sorted by category and name, including unavailable ones (shown greyed)."""
    with read_session() as s:
        rows = s.execute(
            select(MenuItem.id, MenuItem.name, MenuItem.category, MenuItem.station,
                   MenuItem.price_paise, MenuItem.available)
            .order_by(MenuItem.category, MenuItem.name)
        ).all()
    return [
        {"menu_item_id": r.id, "name": r.name, "category": r.category, "station": r.station,
         "price_paise": r.price_paise, "available": r.available}
        for r in rows
    ]


def list_availability(station: str | None = None) -> list[dict]:
    """Dishes with their on/off state for the kitchen (no prices, no costs).

    `station` limits to one station (a chef's own); None lists every station (manager).
    """
    stmt = select(MenuItem.id, MenuItem.name, MenuItem.category, MenuItem.station, MenuItem.available)
    if station is not None:
        stmt = stmt.where(MenuItem.station == station)
    with read_session() as s:
        rows = s.execute(stmt.order_by(MenuItem.station, MenuItem.category, MenuItem.name)).all()
    return [
        {"menu_item_id": r.id, "name": r.name, "category": r.category, "station": r.station,
         "available": r.available}
        for r in rows
    ]


def set_available(menu_item_id: int, available: bool, by_staff_id: int) -> tuple[dict, list[Event]]:
    """Turn a dish on or off ("86" it). Chefs only for their own station; managers for any.

    Only future orders are affected. Tells every section so open order screens grey it out.
    """
    with write_session() as s:
        staff = get_active_staff(s, by_staff_id)
        item = s.get(MenuItem, menu_item_id, options=[
            defer(MenuItem.cost_paise, raiseload=True), defer(MenuItem.price_paise, raiseload=True),
        ])
        if item is None:
            raise ServiceError("Menu item not found")
        if staff.role == "chef":
            if staff.station != item.station:
                raise ServiceError(f"{item.name} belongs to the {item.station} station")
        elif staff.role != "manager":
            raise ServiceError("Only chefs and managers can change availability")

        item.available = bool(available)
        sections = s.scalars(select(DiningTable.section).distinct()).all()
        data = {"menu_item_id": item.id, "name": item.name, "station": item.station,
                "available": item.available}
    events = [Event(f"section:{sec}", "menu", data) for sec in sorted(sections)]
    return data, events
