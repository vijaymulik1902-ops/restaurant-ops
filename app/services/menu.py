"""Menu as seen by floor and kitchen: prices for the order screen, never cost."""
from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.db import read_session, write_session
from app.models import DiningTable, MenuItem
from app.services import Event, ServiceError, audit
from app.services.tables import get_active_staff


def section_events(s: Session, event_type: str, data: dict) -> list[Event]:
    """One small event per floor section (every order screen listens to its section)."""
    sections = s.scalars(select(DiningTable.section).distinct()).all()
    return [Event(f"section:{sec}", event_type, data) for sec in sorted(sections)]


def menu_changed_events(s: Session, menu_item_id: int, change: str) -> list[Event]:
    """Tell order screens to reload their menu block. Ids only: never price or cost."""
    return section_events(s, "menu_changed", {"menu_item_id": menu_item_id, "change": change})


def list_menu() -> list[dict]:
    """Dishes on the menu (not archived), sorted by category and name, including
    unavailable ones (shown greyed)."""
    with read_session() as s:
        rows = s.execute(
            select(MenuItem.id, MenuItem.name, MenuItem.category, MenuItem.station,
                   MenuItem.price_paise, MenuItem.available)
            .where(MenuItem.archived.is_(False))
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
    stmt = (select(MenuItem.id, MenuItem.name, MenuItem.category, MenuItem.station, MenuItem.available)
            .where(MenuItem.archived.is_(False)))
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
        if item is None or item.archived:
            raise ServiceError("Menu item not found")
        if staff.role == "chef":
            if staff.station != item.station:
                raise ServiceError(f"{item.name} belongs to the {item.station} station")
        elif staff.role != "manager":
            raise ServiceError("Only chefs and managers can change availability")

        available = bool(available)
        if item.available == available:
            return {"menu_item_id": item.id, "name": item.name, "station": item.station,
                    "available": available}, []  # already so: nothing to change, audit or announce
        audit.record(s, staff.id, audit.AVAILABILITY, "menu_item", item.id,
                     old={"available": item.available, "name": item.name},
                     new={"available": available})
        item.available = available
        data = {"menu_item_id": item.id, "name": item.name, "station": item.station,
                "available": item.available}
        events = section_events(s, "menu", data)
    return data, events
