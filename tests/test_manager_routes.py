"""Manager-only screens through the real app: audit log and menu management."""
import re

import pytest

from app.services import audit, kitchen, menu, menu_admin, orders
from conftest import open_and_order, serve_all
from test_routes import login


def test_audit_page_shows_flagged_rows_and_filters(db):
    kot = open_and_order(db, [("naan", 1)])
    item_id = orders.get_order(kot["order_id"])["items"][0]["item_id"]
    kitchen.start_item(item_id, "tandoor")
    kitchen.ready_item(item_id, "tandoor")
    kitchen.cancel_item(item_id, "Burnt", db["staff"]["manager"])
    menu.set_available(db["menu"]["dal"], False, db["staff"]["chef_kitchen"])

    c = login("manager")
    html = c.get("/audit").text
    assert "Cancelled after it was ready" in html and 'class="flagged"' in html
    assert "Burnt" in html and "Mahesh" in html
    only = c.get("/audit?action=availability").text
    assert "availability" in only and "Burnt" not in only
    empty = c.get(f"/audit?staff_id={db['staff']['waiter']}").text
    assert "No entries match" in empty


@pytest.mark.parametrize("query", ["action=delete_everything", "date_from=2026-09-30&date_to=2026-09-01",
                                   "page=0", "staff_id=99999999999999", "date_from=0001-01-01"])
def test_audit_bad_filters_are_friendly(db, query):
    c = login("manager")
    resp = c.get(f"/audit?{query}")
    assert resp.status_code == 303
    assert 'class="flash flash-error"' in c.get(resp.headers["location"], follow_redirects=True).text


# ---------- Part C: menu management ----------

def _dish(name):
    return next(d for d in menu_admin.list_menu_admin() if d["name"] == name)


def _flash_after(c, resp):
    return c.get(resp.headers["location"].split("#")[0]).text


def test_menu_page_shows_price_cost_margin(db):
    html = login("manager").get("/menu").text
    # Butter Naan: price 45.00, cost 12.00 -> margin 73.3%
    block = html.split('id="dish-%d"' % db["menu"]["naan"])[1].split("</article>")[0]
    assert 'value="45.00"' in block and 'value="12.00"' in block and "73.3%" in block


def test_price_and_cost_are_saved_separately(db):
    c, naan = login("manager"), db["menu"]["naan"]
    # Even if a tampered form sends both fields, the price route only reads the price
    c.post(f"/menu/{naan}/price", data={"price": "50", "cost": "999"})
    assert (_dish("Butter Naan")["price_paise"], _dish("Butter Naan")["cost_paise"]) == (5000, 1200)
    c.post(f"/menu/{naan}/cost", data={"cost": "15.50", "price": "1"})
    assert (_dish("Butter Naan")["price_paise"], _dish("Butter Naan")["cost_paise"]) == (5000, 1550)
    actions = [r["action"] for r in audit.list_audit()["rows"]]
    assert actions == [audit.COST_CHANGE, audit.PRICE_CHANGE]


def test_add_dish_rejects_duplicates_and_warns_on_cost_above_price(db):
    c = login("manager")
    resp = c.post("/menu", data={"name": "Masala Papad", "category": "Starters", "station": "kitchen",
                                 "price": "40", "cost": "45"})
    assert resp.status_code == 303
    assert "no profit" in _flash_after(c, resp)  # warned, not blocked
    assert _dish("Masala Papad")["price_paise"] == 4000
    assert any(d["name"] == "Masala Papad" for d in menu.list_menu())  # waiters can order it

    resp = c.post("/menu", data={"name": "  masala   PAPAD ", "category": "Starters", "station": "kitchen",
                                 "price": "40", "cost": "10"}, headers={"referer": "http://testserver/menu"})
    assert resp.status_code == 303
    assert "already exists" in c.get("/menu").text
    assert sum(d["name"].lower() == "masala papad" for d in menu_admin.list_menu_admin()) == 1


@pytest.mark.parametrize("field, value", [("price", "0"), ("price", "-5"), ("price", "abc"), ("price", "1e1000000"),
                                          ("price", "99999999"), ("cost", "-1"), ("cost", "12.345")])
def test_bad_money_input_is_refused(db, field, value):
    c, naan = login("manager"), db["menu"]["naan"]
    resp = c.post(f"/menu/{naan}/{field}", data={field: value}, headers={"referer": "http://testserver/menu"})
    assert resp.status_code == 303
    assert (_dish("Butter Naan")["price_paise"], _dish("Butter Naan")["cost_paise"]) == (4500, 1200)


def test_menu_service_is_manager_only(db):
    from app.services import ServiceError

    for who in ("waiter", "chef_tandoor", "counter"):
        with pytest.raises(ServiceError, match="manager"):
            menu_admin.set_price(db["menu"]["naan"], 1, db["staff"][who])
        with pytest.raises(ServiceError, match="manager"):
            menu_admin.add_dish("X", "Y", "bar", 100, 0, db["staff"][who])


# ---------- C2 ----------

def _bill_total(db, order_id, by="counter"):
    from app.services import billing

    serve_all(db, order_id)
    bill, _ = billing.generate_bill(order_id, 0, db["staff"][by])
    return bill["total_paise"]


def test_cost_change_never_changes_any_bill_total(db):
    from conftest import new_kot_id

    before = open_and_order(db, [("naan", 2)], table_index=0)       # sent before the cost edit
    menu_admin.set_cost(db["menu"]["naan"], 4400, db["staff"]["manager"])
    after = open_and_order(db, [("naan", 2)], table_index=1)        # sent after the cost edit
    expected = 9000 + 450                                             # 2 × ₹45 + 5% GST
    assert _bill_total(db, before["order_id"]) == expected
    assert _bill_total(db, after["order_id"]) == expected
    # Cost snapshots do follow the edit (for the manager's profit reports)
    from sqlalchemy import select

    from app.db import read_session
    from app.models import OrderItem
    with read_session() as s:
        costs = dict(s.execute(select(OrderItem.order_id, OrderItem.unit_cost_paise)).all())
    assert costs == {before["order_id"]: 1200, after["order_id"]: 4400}
    assert new_kot_id  # imported helper used above via open_and_order


def test_price_change_affects_only_new_kots(db):
    from conftest import new_kot_id

    kot = open_and_order(db, [("naan", 1)])
    menu_admin.set_price(db["menu"]["naan"], 6000, db["staff"]["manager"])
    assert orders.get_order(kot["order_id"])["items"][0]["unit_price_paise"] == 4500  # already sent: unchanged
    orders.send_kot(kot["order_id"], new_kot_id(), db["staff"]["waiter"], [(db["menu"]["naan"], 1, None)])
    prices = [i["unit_price_paise"] for i in orders.get_order(kot["order_id"])["items"]]
    assert prices == [4500, 6000]
    assert orders.get_order(kot["order_id"])["total_paise"] == 10500
    assert _bill_total(db, kot["order_id"]) == 10500 + 525


def test_non_manager_pages_never_show_cost_or_margin_after_edits(db):
    from app.services import billing

    # Distinctive cost values that must never leak: ₹77.77 and ₹66.66
    menu_admin.set_cost(db["menu"]["naan"], 7777, db["staff"]["manager"])
    menu_admin.set_cost(db["menu"]["dal"], 6666, db["staff"]["manager"])
    menu_admin.set_price(db["menu"]["naan"], 5000, db["staff"]["manager"])
    open_kot = open_and_order(db, [("naan", 1), ("dal", 1)], table_index=0)
    paid = open_and_order(db, [("naan", 1)], table_index=1)
    serve_all(db, paid["order_id"])
    bill, _ = billing.generate_bill(paid["order_id"], 0, db["staff"]["counter"])
    billing.pay_bill(bill["bill_id"], "cash", db["staff"]["counter"])

    o = open_kot["order_id"]
    pages = {
        "waiter": ["/floor", f"/orders/{o}", f"/orders/{o}/items", "/floor/board?all=1"],
        "chef": ["/kitchen", "/kitchen/board", "/kitchen/availability"],
        "counter": ["/counter", f"/counter/orders/{o}", f"/counter/bills/{bill['bill_id']}",
                    f"/counter/bills/{bill['bill_id']}/print", "/reports/day-close"],
    }
    for role, urls in pages.items():
        c = login(role)
        for url in urls:
            html = c.get(url).text
            low = re.sub(r"<style.*?</style>", "", html, flags=re.S).lower()  # CSS has "margin:"
            assert c.get(url).status_code == 200, (role, url)
            for leak in ("77.77", "66.66", "7777", "6666", "margin", "cost"):
                assert leak not in low, (role, url, leak)
    # The manager does see them
    assert "77.77" in login("manager").get("/menu").text


def test_counter_cannot_exceed_ten_percent_by_editing_the_form(db):
    kot = open_and_order(db, [("naan", 2)])  # ₹90
    serve_all(db, kot["order_id"])
    c = login("counter")
    url = f"/counter/orders/{kot['order_id']}"
    version = re.search(r'name="version" value="(\d+)"', c.get(url).text).group(1)
    for tampered in ("9.01", "45", "90", "1000"):
        resp = c.post(f"{url}/bill", data={"discount": tampered, "version": version},
                      headers={"referer": f"http://testserver{url}"})
        assert resp.status_code == 303 and resp.headers["location"] == url
    page = c.get(url).text
    assert "needs a manager" in page or "cannot exceed" in page
    assert orders.get_order(kot["order_id"])["bill_id"] is None
    ok = c.post(f"{url}/bill", data={"discount": "9", "version": version})  # exactly 10% is fine
    assert ok.headers["location"].startswith("/counter/bills/")
