"""Part A: live menu changes, rename, archive, and the startup schema upgrade."""
import pytest
from sqlalchemy import create_engine, inspect, text

from app.services import ServiceError, billing, kitchen, menu, menu_admin, orders
from conftest import new_kot_id, open_and_order, serve_all
from test_routes import login


def _has_price_or_cost(value) -> bool:
    if isinstance(value, dict):
        return any("price" in k or "cost" in k or _has_price_or_cost(v) for k, v in value.items())
    return False


# ---------- A1: menu_changed events ----------

@pytest.mark.parametrize("change", ["price", "added", "renamed", "archived", "restored"])
def test_menu_changes_publish_small_event_to_every_section(db, change):
    m, naan = db["staff"]["manager"], db["menu"]["naan"]
    if change == "restored":
        menu_admin.set_archived(naan, True, m)
    _, events = {
        "price": lambda: menu_admin.set_price(naan, 5000, m),
        "added": lambda: menu_admin.add_dish("Jeera Aloo", "Mains", "kitchen", 15000, 4000, m),
        "renamed": lambda: menu_admin.rename_dish(naan, "Makhani Naan", m),
        "archived": lambda: menu_admin.set_archived(naan, True, m),
        "restored": lambda: menu_admin.set_archived(naan, False, m),
    }[change]()
    assert sorted(e.channel for e in events) == ["section:A", "section:B"]
    assert all(e.type == "menu_changed" and e.data["change"] == change for e in events)
    assert not any(_has_price_or_cost(e.data) for e in events)


def test_cost_change_publishes_nothing(db):
    _, events = menu_admin.set_cost(db["menu"]["naan"], 2000, db["staff"]["manager"])
    assert events == []


def test_menu_block_partial_reflects_changes(db):
    opened = orders.get_order(open_and_order(db, [("dal", 1)])["order_id"])
    url = f"/orders/{opened['order_id']}/menu"
    c = login("waiter")
    before = c.get(url).text
    assert 'id="menu-block"' in before and "₹45.00" in before and "<form" not in before
    menu_admin.set_price(db["menu"]["naan"], 5500, db["staff"]["manager"])
    menu_admin.rename_dish(db["menu"]["dal"], "Dal Fry", db["staff"]["manager"])
    menu_admin.set_archived(db["menu"]["lassi"], True, db["staff"]["manager"])
    after = c.get(url).text
    assert "₹55.00" in after and "Dal Fry" in after and "Sweet Lassi" not in after
    assert login("chef").get(url).status_code == 403


# ---------- A2: rename ----------

def test_rename_keeps_old_name_on_history(db):
    kot = open_and_order(db, [("naan", 2)])
    serve_all(db, kot["order_id"])
    bill, _ = billing.generate_bill(kot["order_id"], 0, db["staff"]["counter"])
    menu_admin.rename_dish(db["menu"]["naan"], "Makhani Naan", db["staff"]["manager"])

    assert [ln["name"] for ln in billing.get_bill(bill["bill_id"])["lines"]] == ["Butter Naan"]
    printed = login("counter").get(f"/counter/bills/{bill['bill_id']}/print").text
    assert "Butter Naan" in printed and "Makhani Naan" not in printed
    assert any(d["name"] == "Makhani Naan" for d in menu.list_menu())
    # New orders use the new name
    kot2 = open_and_order(db, [("naan", 1)], table_index=1)
    assert orders.get_order(kot2["order_id"])["items"][0]["name"] == "Makhani Naan"


@pytest.mark.parametrize("name", ["dal tadka", "  DAL   TADKA  ", "", "x" * 81])
def test_rename_rejects_duplicates_and_bad_names(db, name):
    with pytest.raises(ServiceError):
        menu_admin.rename_dish(db["menu"]["naan"], name, db["staff"]["manager"])


def test_rename_to_same_name_with_new_case_is_allowed(db):
    result, _ = menu_admin.rename_dish(db["menu"]["naan"], "BUTTER NAAN", db["staff"]["manager"])
    assert result["changed"] and result["name"] == "BUTTER NAAN"


def test_rename_via_route(db):
    c = login("manager")
    resp = c.post(f"/menu/{db['menu']['naan']}/rename", data={"name": "Makhani Naan"})
    assert resp.status_code == 303
    assert "past bills keep the old name" in c.get("/menu").text
    assert login("counter").post(f"/menu/{db['menu']['naan']}/rename", data={"name": "X"}).status_code == 403


# ---------- A3: archive ----------

def test_archived_dish_disappears_everywhere_but_can_be_restored(db):
    naan, m = db["menu"]["naan"], db["staff"]["manager"]
    menu_admin.set_archived(naan, True, m)
    assert naan not in [d["menu_item_id"] for d in menu.list_menu()]
    assert naan not in [d["menu_item_id"] for d in menu.list_availability()]
    assert naan not in [d["menu_item_id"] for d in menu_admin.list_menu_admin()]
    assert naan in [d["menu_item_id"] for d in menu_admin.list_menu_admin(show_archived=True)]
    with pytest.raises(ServiceError, match="no longer on the menu"):
        kot = open_and_order(db, [("dal", 1)])
        orders.send_kot(kot["order_id"], new_kot_id(), db["staff"]["waiter"], [(naan, 1, None)])
    with pytest.raises(ServiceError):
        menu.set_available(naan, False, db["staff"]["chef_tandoor"])
    with pytest.raises(ServiceError, match="already exists"):  # the name stays reserved
        menu_admin.add_dish("Butter Naan", "Breads", "tandoor", 4500, 1000, m)

    menu_admin.set_archived(naan, False, m)
    assert naan in [d["menu_item_id"] for d in menu.list_menu()]


def test_archiving_dish_on_open_order_does_not_change_its_bill(db):
    kot = open_and_order(db, [("naan", 2), ("dal", 1)])
    menu_admin.set_archived(db["menu"]["naan"], True, db["staff"]["manager"])
    assert orders.get_order(kot["order_id"])["total_paise"] == 9000 + 18000
    serve_all(db, kot["order_id"])  # the archived dish is still cooked and served
    bill, _ = billing.generate_bill(kot["order_id"], 0, db["staff"]["counter"])
    assert bill["subtotal_paise"] == 27000
    assert [ln["name"] for ln in billing.get_bill(bill["bill_id"])["lines"]] == ["Butter Naan", "Dal Tadka"]


def test_archive_routes_and_toggle(db):
    c, naan = login("manager"), db["menu"]["naan"]
    c.post(f"/menu/{naan}/archive", data={"archived": "1"})
    assert f'id="dish-{naan}"' not in c.get("/menu").text
    shown = c.get("/menu?archived=1").text
    assert f'id="dish-{naan}"' in shown and "Restore to menu" in shown
    c.post(f"/menu/{naan}/archive", data={"archived": "0"})
    assert f'id="dish-{naan}"' in c.get("/menu").text
    assert "Butter Naan" in login("chef").get("/kitchen/availability").text  # restored: back for the chef
    assert login("waiter").post(f"/menu/{naan}/archive", data={"archived": "1"}).status_code == 403


def test_kitchen_still_finishes_archived_dish(db):
    kot = open_and_order(db, [("naan", 1)])
    menu_admin.set_archived(db["menu"]["naan"], True, db["staff"]["manager"])
    item_id = kitchen.live_items("tandoor")[0]["item_id"]
    kitchen.start_item(item_id, "tandoor")
    kitchen.ready_item(item_id, "tandoor")
    assert orders.get_order(kot["order_id"])["items"][0]["status"] == "ready"


# ---------- A3: schema upgrade for existing databases ----------

def test_ensure_schema_adds_archived_column_once(tmp_path):
    from app.migrations import ensure_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:  # a menu_items table from before the archived column existed
        conn.execute(text("CREATE TABLE menu_items (id INTEGER PRIMARY KEY, name VARCHAR(80), available BOOLEAN)"))
        conn.execute(text("INSERT INTO menu_items (name, available) VALUES ('Old Dish', 1)"))
    assert ensure_schema(engine) == ["menu_items.archived"]
    assert ensure_schema(engine) == []  # idempotent
    cols = {c["name"] for c in inspect(engine).get_columns("menu_items")}
    assert "archived" in cols
    with engine.connect() as conn:
        assert conn.execute(text("SELECT archived FROM menu_items")).scalar() == 0


def test_ensure_schema_is_noop_on_current_db(db):
    from app.migrations import ensure_schema

    assert ensure_schema() == []
