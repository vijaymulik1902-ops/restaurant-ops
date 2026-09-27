"""Kitchen "Cooking summary": pending + preparing items grouped by (dish, note), read-only."""
import pytest

from app.services import ServiceError, kitchen, orders, tables
from conftest import new_kot_id
from test_routes import login


def _order(db, table_index, lines):
    """lines: [(menu key, qty, note)] sent as one KOT on a fresh table."""
    opened, _ = tables.open_table(db["tables"][table_index], db["staff"]["waiter"], 2)
    orders.send_kot(opened["order_id"], new_kot_id(), db["staff"]["waiter"],
                    [(db["menu"][k], q, n) for k, q, n in lines])
    return opened["order_id"]


def _items(order_id):
    return orders.get_order(order_id)["items"]


def test_groups_by_dish_and_note_never_merging_notes(db, clock):
    _order(db, 0, [("naan", 2, None), ("dal", 1, "less spicy")])       # T1
    clock.advance(minutes=1)
    _order(db, 1, [("naan", 4, None), ("dal", 3, None)])               # T2
    lines = {(l["name"], l["note"]): l for l in kitchen.cooking_summary("tandoor")}
    assert set(lines) == {("Butter Naan", None)}                        # dal is the kitchen station's
    naan = lines[("Butter Naan", None)]
    assert naan["total_qty"] == 6 and naan["tables"] == [{"number": 1, "qty": 2}, {"number": 2, "qty": 4}]

    dal = {(l["name"], l["note"]): l for l in kitchen.cooking_summary("kitchen")}
    assert dal[("Dal Tadka", "less spicy")]["total_qty"] == 1           # its own line
    assert dal[("Dal Tadka", None)]["total_qty"] == 3


def test_split_counts_and_oldest_first(db, clock):
    first = _order(db, 0, [("naan", 3, None)])
    clock.advance(minutes=2)
    _order(db, 1, [("naan", 2, None), ("dal", 1, None)])
    clock.advance(minutes=1)
    _order(db, 2, [("naan", 1, "well done")])
    kitchen.start_item(_items(first)[0]["item_id"], "tandoor")          # 3 naan now cooking
    lines = kitchen.cooking_summary("tandoor")
    assert [(l["name"], l["note"]) for l in lines] == [("Butter Naan", None), ("Butter Naan", "well done")]
    assert (lines[0]["to_start"], lines[0]["cooking"], lines[0]["total_qty"]) == (2, 3, 5)
    assert lines[0]["oldest_age_seconds"] == 180                        # the first order, 3 min ago


def test_ready_served_and_cancelled_items_excluded(db, clock):
    o = _order(db, 0, [("naan", 2, None), ("naan", 1, "extra butter")])
    plain, butter = [i["item_id"] for i in _items(o)]
    kitchen.cancel_item(butter, "Customer changed mind", db["staff"]["waiter"])
    kitchen.start_item(plain, "tandoor")
    assert [l["total_qty"] for l in kitchen.cooking_summary("tandoor")] == [2]
    kitchen.ready_item(plain, "tandoor")
    assert kitchen.cooking_summary("tandoor") == []                     # ready: no longer "to cook"
    kitchen.serve_item(plain, db["staff"]["waiter"])
    assert kitchen.cooking_summary("tandoor") == []


def test_unknown_station_rejected(db):
    with pytest.raises(ServiceError):
        kitchen.cooking_summary("pastry")


def test_panel_on_kitchen_page_read_only_and_station_scoped(db, clock):
    _order(db, 0, [("naan", 2, "crispy"), ("dal", 1, None)])
    html = login("chef").get("/kitchen").text                            # Suresh: tandoor
    panel = html.split('id="cook-summary"')[1].split("</section>")[0]
    assert "Butter Naan" in panel and "crispy" in panel and "T1 ×2" in panel
    assert "2 to start · 0 cooking" in panel
    assert "Dal Tadka" not in panel                                     # another station's dish
    assert "<form" not in panel and "hx-post" not in panel              # read-only
    assert html.index('id="cook-summary"') < html.index('id="board"')   # above the tickets
    assert 'id="item-' in html.split('id="board"')[1]                   # tickets still rendered below


def test_panel_empty_state_and_partial(db):
    c = login("chef")
    assert "Nothing to cook right now." in c.get("/kitchen").text
    partial = c.get("/kitchen/summary")
    assert partial.status_code == 200 and partial.text.lstrip().startswith("{#") is False
    assert 'id="cook-summary"' in partial.text
    assert login("waiter").get("/kitchen/summary").status_code == 403


def test_collapses_after_five_lines(db, clock):
    notes = ["a", "b", "c", "d", "e", "f", "g"]  # same dish, 7 different notes: 7 lines
    _order(db, 0, [("naan", 1, n) for n in notes])
    html = login("chef").get("/kitchen/summary").text
    visible = html.split('<details class="cook-more">')[0]
    assert visible.count('<li class="cook-line') == 5
    assert "Show all (7 dishes)" in html


def test_line_turns_red_when_oldest_item_is_late(db, clock):
    _order(db, 0, [("naan", 1, None)])
    clock.advance(minutes=21)
    html = login("chef").get("/kitchen/summary").text
    assert 'class="cook-line late"' in html
