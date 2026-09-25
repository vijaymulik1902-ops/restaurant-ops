import pytest

from app.services import ServiceError, menu, orders
from conftest import new_kot_id, open_and_order


def _available(menu_item_id):
    return next(d["available"] for d in menu.list_availability() if d["menu_item_id"] == menu_item_id)


def test_chef_toggles_own_station_item_and_all_sections_hear_it(db):
    naan = db["menu"]["naan"]  # tandoor
    data, events = menu.set_available(naan, False, db["staff"]["chef_tandoor"])
    assert data["available"] is False and _available(naan) is False
    assert sorted(e.channel for e in events) == ["section:A", "section:B"]
    assert all(e.type == "menu" and set(e.data) == {"menu_item_id", "name", "station", "available"}
               for e in events)
    menu.set_available(naan, True, db["staff"]["chef_tandoor"])
    assert _available(naan) is True


def test_chef_cannot_toggle_other_station(db):
    with pytest.raises(ServiceError, match="kitchen station"):
        menu.set_available(db["menu"]["dal"], False, db["staff"]["chef_tandoor"])
    assert _available(db["menu"]["dal"]) is True


@pytest.mark.parametrize("who", ["waiter", "counter"])
def test_floor_staff_cannot_toggle(db, who):
    with pytest.raises(ServiceError):
        menu.set_available(db["menu"]["naan"], False, db["staff"][who])


def test_manager_toggles_any_station(db):
    menu.set_available(db["menu"]["lassi"], False, db["staff"]["manager"])
    assert _available(db["menu"]["lassi"]) is False


def test_unavailable_item_cannot_be_ordered_but_existing_lines_stay(db):
    kot = open_and_order(db, [("naan", 1)])
    menu.set_available(db["menu"]["naan"], False, db["staff"]["manager"])
    with pytest.raises(ServiceError, match="not available"):
        orders.send_kot(kot["order_id"], new_kot_id(), db["staff"]["waiter"],
                        [(db["menu"]["naan"], 1, None)])
    assert [i["status"] for i in orders.get_order(kot["order_id"])["items"]] == ["pending"]


def test_availability_list_has_no_prices_or_costs(db):
    chef_view = menu.list_availability("tandoor")
    assert [d["name"] for d in chef_view] == ["Butter Naan"]
    assert all("price" not in k and "cost" not in k for d in menu.list_availability() for k in d)


def test_set_available_never_loads_price_or_cost(db):
    from sqlalchemy import event

    from app.db import write_engine

    seen = []

    def spy(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("SELECT") and ("cost_paise" in statement or "price_paise" in statement):
            seen.append(statement)

    event.listen(write_engine, "before_cursor_execute", spy)
    try:
        menu.set_available(db["menu"]["naan"], False, db["staff"]["chef_tandoor"])
    finally:
        event.remove(write_engine, "before_cursor_execute", spy)
    assert seen == []
