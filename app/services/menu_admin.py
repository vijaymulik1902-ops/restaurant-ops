"""Menu management: MANAGER ONLY. This is one of the few modules allowed to read cost.

Price and cost are edited by separate functions that each touch exactly one column,
so saving one can never change the other. Edits apply to future KOTs only: order
lines keep the price/cost snapshot taken when they were sent. Every change is audited.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import read_session, write_session
from app.models import STATIONS, MenuItem
from app.services import Event, ServiceError, audit
from app.services.menu import menu_changed_events
from app.services.tables import get_active_staff

MAX_PRICE_PAISE = 10**7  # ₹1,00,000 per plate: anything above is a typo
MAX_NAME_LEN = 80
MAX_CATEGORY_LEN = 30


def _require_manager(s: Session, staff_id: int) -> None:
    if get_active_staff(s, staff_id).role != "manager":
        raise ServiceError("Only a manager can edit the menu")


def _check_amount(paise: int, label: str, allow_zero: bool) -> None:
    if not isinstance(paise, int) or isinstance(paise, bool):
        raise ServiceError(f"{label} must be a number")
    if paise < 0 or (paise == 0 and not allow_zero):
        raise ServiceError(f"{label} must be more than zero" if not allow_zero else f"{label} cannot be negative")
    if paise > MAX_PRICE_PAISE:
        raise ServiceError(f"{label} looks too high")


def _warning(price_paise: int, cost_paise: int) -> str | None:
    """Not an error (a loss-leader is allowed), but worth a second look."""
    if cost_paise >= price_paise:
        return "Warning: cost is equal to or higher than the price, this dish makes no profit"
    return None


def margin_percent(price_paise: int, cost_paise: int) -> float | None:
    return None if price_paise <= 0 else round((price_paise - cost_paise) * 100 / price_paise, 1)


def list_menu_admin(show_archived: bool = False) -> list[dict]:
    """Dishes with price, cost and margin %, for the manager's menu screen.

    Archived dishes are left out unless `show_archived` (so they can be restored).
    """
    stmt = (select(MenuItem.id, MenuItem.name, MenuItem.category, MenuItem.station,
                   MenuItem.price_paise, MenuItem.cost_paise, MenuItem.available, MenuItem.archived)
            .order_by(MenuItem.category, MenuItem.name))
    if not show_archived:
        stmt = stmt.where(MenuItem.archived.is_(False))
    with read_session() as s:
        rows = s.execute(stmt).all()
    return [
        {"menu_item_id": r.id, "name": r.name, "category": r.category, "station": r.station,
         "price_paise": r.price_paise, "cost_paise": r.cost_paise, "available": r.available,
         "archived": r.archived,
         "margin_percent": margin_percent(r.price_paise, r.cost_paise),
         "warning": _warning(r.price_paise, r.cost_paise)}
        for r in rows
    ]


def add_dish(name: str, category: str, station: str, price_paise: int, cost_paise: int,
             by_staff_id: int) -> tuple[dict, list[Event]]:
    """Add a dish (available immediately). Names are unique, ignoring case.

    Audited as dish_added (no cost in it) plus cost_change for the starting cost.
    Returns ({menu_item_id, name, warning}, events).
    """
    name = _clean_name(name)
    category = " ".join((category or "").split())
    if not category or len(category) > MAX_CATEGORY_LEN:
        raise ServiceError(f"Category is required (max {MAX_CATEGORY_LEN} characters)")
    if station not in STATIONS:
        raise ServiceError("Choose a station: " + ", ".join(STATIONS))
    _check_amount(price_paise, "Price", allow_zero=False)
    _check_amount(cost_paise, "Cost", allow_zero=True)

    with write_session() as s:
        _require_manager(s, by_staff_id)
        _check_unique(s, name)
        item = MenuItem(name=name, category=category, station=station, price_paise=price_paise,
                        cost_paise=cost_paise, available=True)
        s.add(item)
        s.flush()
        audit.record(s, by_staff_id, audit.DISH_ADDED, "menu_item", item.id, new={
            "name": name, "category": category, "station": station, "price_paise": price_paise,
        })
        audit.record(s, by_staff_id, audit.COST_CHANGE, "menu_item", item.id,
                     old={"cost_paise": None}, new={"cost_paise": cost_paise, "name": name})
        events = menu_changed_events(s, item.id, "added")
        return {"menu_item_id": item.id, "name": name, "warning": _warning(price_paise, cost_paise)}, events


def _clean_name(name: str | None) -> str:
    """Trim and collapse spaces; required, max length."""
    name = " ".join((name or "").split())
    if not name or len(name) > MAX_NAME_LEN:
        raise ServiceError(f"Dish name is required (max {MAX_NAME_LEN} characters)")
    return name


def _check_unique(s: Session, name: str, exclude_id: int | None = None) -> None:
    """Dish names are unique ignoring case (archived dishes included: names stay reserved)."""
    stmt = select(MenuItem.name).where(func.lower(MenuItem.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(MenuItem.id != exclude_id)
    clash = s.scalar(stmt)
    if clash:
        raise ServiceError(f'A dish called "{clash}" already exists')


def _load_dish(s: Session, menu_item_id: int) -> MenuItem:
    item = s.get(MenuItem, menu_item_id)
    if item is None:
        raise ServiceError("Dish not found")
    return item


def set_price(menu_item_id: int, price_paise: int, by_staff_id: int) -> tuple[dict, list[Event]]:
    """Change ONLY the price. Lines already sent keep their old price."""
    _check_amount(price_paise, "Price", allow_zero=False)
    with write_session() as s:
        _require_manager(s, by_staff_id)
        item = _load_dish(s, menu_item_id)
        if item.price_paise == price_paise:
            return {"menu_item_id": item.id, "changed": False, "warning": _warning(price_paise, item.cost_paise)}, []
        audit.record(s, by_staff_id, audit.PRICE_CHANGE, "menu_item", item.id,
                     old={"price_paise": item.price_paise, "name": item.name},
                     new={"price_paise": price_paise})
        item.price_paise = price_paise
        events = menu_changed_events(s, item.id, "price")
        return {"menu_item_id": item.id, "changed": True,
                "warning": _warning(price_paise, item.cost_paise)}, events


def set_cost(menu_item_id: int, cost_paise: int, by_staff_id: int) -> tuple[dict, list[Event]]:
    """Change ONLY the cost. Never affects any bill (bills use prices only)."""
    _check_amount(cost_paise, "Cost", allow_zero=True)
    with write_session() as s:
        _require_manager(s, by_staff_id)
        item = _load_dish(s, menu_item_id)
        if item.cost_paise == cost_paise:
            return {"menu_item_id": item.id, "changed": False, "warning": _warning(item.price_paise, cost_paise)}, []
        audit.record(s, by_staff_id, audit.COST_CHANGE, "menu_item", item.id,
                     old={"cost_paise": item.cost_paise, "name": item.name},
                     new={"cost_paise": cost_paise})
        item.cost_paise = cost_paise
        return {"menu_item_id": item.id, "changed": True, "warning": _warning(item.price_paise, cost_paise)}, []


def rename_dish(menu_item_id: int, new_name: str, by_staff_id: int) -> tuple[dict, list[Event]]:
    """Rename a dish for future orders. Past order lines and bills keep the old name (snapshot)."""
    new_name = _clean_name(new_name)
    with write_session() as s:
        _require_manager(s, by_staff_id)
        item = _load_dish(s, menu_item_id)
        if item.name == new_name:
            return {"menu_item_id": item.id, "changed": False, "name": new_name}, []
        _check_unique(s, new_name, exclude_id=item.id)
        audit.record(s, by_staff_id, audit.DISH_RENAMED, "menu_item", item.id,
                     old={"name": item.name}, new={"name": new_name})
        item.name = new_name
        return {"menu_item_id": item.id, "changed": True, "name": new_name}, \
            menu_changed_events(s, item.id, "renamed")


def set_archived(menu_item_id: int, archived: bool, by_staff_id: int) -> tuple[dict, list[Event]]:
    """Archive (retire) or restore a dish. Archived dishes can't be ordered and vanish from
    ordering and availability screens; orders already sent are untouched."""
    archived = bool(archived)
    with write_session() as s:
        _require_manager(s, by_staff_id)
        item = _load_dish(s, menu_item_id)
        if item.archived == archived:
            return {"menu_item_id": item.id, "changed": False, "archived": archived}, []
        audit.record(s, by_staff_id, audit.DISH_ARCHIVED if archived else audit.DISH_RESTORED,
                     "menu_item", item.id, old={"archived": item.archived, "name": item.name},
                     new={"archived": archived})
        item.archived = archived
        return {"menu_item_id": item.id, "changed": True, "archived": archived, "name": item.name}, \
            menu_changed_events(s, item.id, "archived" if archived else "restored")
