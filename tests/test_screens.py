"""Redesigned screens keep their live hooks and show the new pieces."""
from conftest import open_and_order
from test_routes import login
from test_sales import _paid


def test_manager_home_tiles_map_and_insights(db, clock):
    _paid(db, [("dal", 1)])
    open_and_order(db, [("naan", 1)], table_index=1)
    html = login("manager").get("/home").text
    assert 'id="home-live" data-home' in html
    assert "Net sales" in html and "₹180" in html  # 1 Dal Tadka paid today
    assert "Tables in use" in html and "Open orders" in html and "Kitchen items late" in html
    assert html.count('class="mini-map"') == 1 and 'class="st-occupied"' in html
    assert html.count('<article class="insight">') == 3


def test_floor_has_section_chips_legend_and_board_hooks(db):
    html = login("manager").get("/floor").text
    assert 'data-section-filter="A"' in html and 'class="legend"' in html
    assert 'id="board"' in html and 'data-list-url="/floor/board?view=floor&all=1"' in html
    assert 'data-section="A"' in html and 'data-card-url="/floor/tables/' in html
    # a waiter on their own section gets no section chips
    assert "data-section-filter" not in login("waiter").get("/floor").text


def test_order_screen_timeline_categories_and_notes(db):
    kot = open_and_order(db, [("naan", 2), ("dal", 1)])
    html = login("waiter").get(f"/orders/{kot['order_id']}").text
    assert 'class="kot-timeline"' in html and "KOT #1 ·" in html
    assert 'class="cat-strip"' in html and 'href="#cat-1"' in html and 'id="cat-1"' in html
    assert 'data-note-for="note_' in html
    # hooks app.js relies on
    for hook in ('id="order-items"', 'data-order-id=', 'id="menu-block"', 'id="kot-form"', 'id="send-count"',
                 'name="kot_id"', 'data-draft-key='):
        assert hook in html, hook


def test_counter_split_panel_and_bill_wrapper(db):
    kot = open_and_order(db, [("naan", 1)])
    c = login("counter")
    html = c.get("/counter").text
    assert 'id="counter-panel"' in html and 'data-filter="active"' in html and 'id="counter-quiet"' in html
    preview = c.get(f"/counter/orders/{kot['order_id']}").text
    assert 'id="bill-panel"' in preview and 'action="/counter/orders/' in preview


def test_flash_still_rendered_for_no_js(db):
    c = login("waiter")
    c.post("/floor/tables/1/open", data={"guest_count": "0"})
    assert 'role="alert"' in c.get("/floor").text
