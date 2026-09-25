"""Menu as seen by waiters and the counter: prices only, never cost."""
from sqlalchemy import select

from app.db import read_session
from app.models import MenuItem


def list_menu() -> list[dict]:
    """All dishes grouped-ready (sorted by category, name), including unavailable ones."""
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
