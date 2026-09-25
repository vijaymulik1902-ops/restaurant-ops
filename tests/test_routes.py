import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import read_session, write_session
from app.main import app
from app.models import Kot, Staff
from app.services import billing, orders, tables
from conftest import TEST_PIN, open_and_order, serve_all

NAMES = {"waiter": "Rahul", "chef": "Suresh", "counter": "Counter", "manager": "Manager"}
HOME = {"waiter": "/floor", "chef": "/kitchen", "counter": "/counter", "manager": "/counter"}


def login(role: str) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    resp = c.post("/login", data={"name": NAMES[role], "pin": TEST_PIN})
    assert resp.status_code == 303, resp.text
    return c


@pytest.fixture
def world(db):
    """One open order with a KOT (table 1) and one generated bill (table 2)."""
    kot = open_and_order(db, [("naan", 1)], table_index=0)
    billed = open_and_order(db, [("dal", 1)], table_index=1)
    serve_all(db, billed["order_id"])
    bill, _ = billing.generate_bill(billed["order_id"], 0, db["staff"]["counter"])
    return {"order_id": kot["order_id"], "bill_id": bill["bill_id"], "billed_order_id": billed["order_id"]}


def test_health(db):
    assert TestClient(app).get("/health").json() == {"ok": True}


@pytest.mark.parametrize("role", list(NAMES))
def test_login_redirects_by_role(db, role):
    c = login(role)
    resp = c.get("/")
    assert resp.status_code == 303 and resp.headers["location"] == HOME[role]


def test_login_page_lists_staff_and_wrong_pin_is_rejected(db):
    c = TestClient(app, follow_redirects=False)
    page = c.get("/login")
    assert page.status_code == 200 and "Rahul" in page.text and "keypad" in page.text
    resp = c.post("/login", data={"name": "Rahul", "pin": "0000"})
    assert resp.status_code == 401 and "Wrong name or PIN" in resp.text
    assert c.get("/").headers["location"] == "/login"


def test_not_logged_in_redirects_to_login(db):
    c = TestClient(app, follow_redirects=False)
    for path in ("/", "/floor", "/kitchen", "/counter", "/reports/day-close"):
        resp = c.get(path)
        assert resp.status_code == 303 and resp.headers["location"] == "/login", path


SCREENS = {
    "/floor": {"waiter", "counter", "manager"},
    "/kitchen": {"chef", "manager"},
    "/counter": {"counter", "manager"},
    "/reports/day-close": {"counter", "manager"},
    "/orders/{order_id}": {"waiter", "counter", "manager"},
    "/counter/orders/{order_id}": {"counter", "manager"},
    "/counter/bills/{bill_id}": {"counter", "manager"},
    "/counter/bills/{bill_id}/print": {"counter", "manager"},
}


@pytest.mark.parametrize("role", list(NAMES))
def test_each_screen_allows_its_roles_and_403s_others(world, role):
    c = login(role)
    for template, allowed in SCREENS.items():
        path = template.format(**world)
        status = c.get(path).status_code
        assert status == (200 if role in allowed else 403), (role, path, status)


@pytest.mark.parametrize("role, method, path, data", [
    ("chef", "post", "/orders/{order_id}/kot", {"kot_id": "x", "version": "2"}),
    ("chef", "post", "/floor/tables/3/open", {"guest_count": "2"}),
    ("waiter", "post", "/kitchen/items/1/start", {}),
    ("counter", "post", "/kitchen/items/1/start", {}),
    ("waiter", "post", "/counter/orders/{order_id}/bill", {"discount": "0"}),
    ("chef", "post", "/counter/bills/{bill_id}/pay", {"payment_mode": "cash"}),
    ("waiter", "post", "/counter/bills/{bill_id}/pay", {"payment_mode": "cash"}),
])
def test_actions_enforce_access_matrix(world, role, method, path, data):
    c = login(role)
    resp = getattr(c, method)(path.format(**world), data=data)
    assert resp.status_code == 403


def _kot_count() -> int:
    with read_session() as s:
        return s.scalar(select(func.count(Kot.id)))


def test_order_page_has_server_generated_kot_id(world):
    c = login("waiter")
    html = c.get(f"/orders/{world['order_id']}").text
    kot_ids = re.findall(r'name="kot_id" value="([0-9a-f-]{36})"', html)
    assert len(kot_ids) == 1
    # Each render gets a fresh id
    again = re.findall(r'name="kot_id" value="([0-9a-f-]{36})"', c.get(f"/orders/{world['order_id']}").text)
    assert again != kot_ids


def test_send_kot_form_twice_with_same_kot_id_creates_one_kot(db):
    opened, _ = tables.open_table(db["tables"][0], db["staff"]["waiter"], 2)
    order_id = opened["order_id"]
    c = login("waiter")
    html = c.get(f"/orders/{order_id}").text
    kot_id = re.search(r'name="kot_id" value="([^"]+)"', html).group(1)
    version = re.search(r'name="version" value="(\d+)"', html).group(1)
    form = {"kot_id": kot_id, "version": version, f"qty_{db['menu']['naan']}": "2",
            f"note_{db['menu']['naan']}": "extra butter", f"qty_{db['menu']['dal']}": "0"}

    before = _kot_count()
    first = c.post(f"/orders/{order_id}/kot", data=form)
    second = c.post(f"/orders/{order_id}/kot", data=form)  # double tap / retry
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"] == f"/orders/{order_id}"
    assert _kot_count() == before + 1
    order = orders.get_order(order_id)
    assert [(i["name"], i["qty"], i["note"]) for i in order["items"]] == [("Butter Naan", 2, "extra butter")]


def test_stale_kot_form_shows_flash(db):
    kot = open_and_order(db, [("naan", 1)])  # version is now 2
    c = login("waiter")
    url = f"/orders/{kot['order_id']}"
    resp = c.post(f"{url}/kot", data={"kot_id": "11111111-1111-1111-1111-111111111111", "version": "1",
                                      f"qty_{db['menu']['dal']}": "1"},
                  headers={"referer": f"http://testserver{url}"})
    assert resp.status_code == 303 and resp.headers["location"] == url
    assert "Order changed, reload" in c.get(url).text


def test_htmx_service_error_returns_toast_without_swap(world):
    c = login("chef")
    item_id = orders.get_order(world["order_id"])["items"][0]["item_id"]
    c.post(f"/kitchen/items/{item_id}/start", headers={"HX-Request": "true"})
    resp = c.post(f"/kitchen/items/{item_id}/start", headers={"HX-Request": "true"})  # already started
    assert resp.status_code == 200
    assert resp.headers["HX-Reswap"] == "none" and "flash" in resp.headers["HX-Trigger"]


def test_deactivated_staff_logged_out(db):
    c = login("waiter")
    assert c.get("/floor").status_code == 200
    with write_session() as s:
        s.get(Staff, db["staff"]["waiter"]).active = False
    resp = c.get("/floor")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_full_service_flow_through_screens(db):
    waiter, chef, counter = login("waiter"), login("chef"), login("counter")
    table_id = db["tables"][0]

    # Waiter seats table 1 and sends a KOT
    resp = waiter.post(f"/floor/tables/{table_id}/open", data={"guest_count": "3"})
    order_url = resp.headers["location"]
    order_id = int(order_url.rsplit("/", 1)[1])
    html = waiter.get(order_url).text
    form = {"kot_id": re.search(r'name="kot_id" value="([^"]+)"', html).group(1),
            "version": re.search(r'name="version" value="(\d+)"', html).group(1),
            f"qty_{db['menu']['naan']}": "2"}
    waiter.post(f"{order_url}/kot", data=form)

    # Chef sees it (no prices), taps start then ready; HTMX gets the fresh card back
    board = chef.get("/kitchen").text
    assert "Butter Naan" in board and "₹" not in board
    item_id = orders.get_order(order_id)["items"][0]["item_id"]
    card = chef.post(f"/kitchen/items/{item_id}/start", headers={"HX-Request": "true"},
                     follow_redirects=True)
    assert "READY" in card.text
    chef.post(f"/kitchen/items/{item_id}/ready", headers={"HX-Request": "true"})

    # Waiter serves it
    assert "Mark served" in waiter.get(order_url).text
    waiter.post(f"/items/{item_id}/serve")

    # Counter bills with a ₹9 discount (10% of ₹90) and takes UPI
    resp = counter.post(f"/counter/orders/{order_id}/bill", data={"discount": "9"})
    bill_url = resp.headers["location"]
    bill_page = counter.get(bill_url).text
    assert "GST 5%" in bill_page and "₹85.05" in bill_page  # 8100 + 405 GST
    counter.post(f"{bill_url}/pay", data={"payment_mode": "upi"})
    printed = counter.get(f"{bill_url}/print").text
    assert "Demo Restaurant" in printed or "Bill No" in printed
    assert "UPI" in printed

    assert tables.get_table(table_id)["status"] == "available"
    report = counter.get("/reports/day-close").text
    assert "₹85.05" in report

    # Cost never reaches any non-manager screen
    for page in (board, bill_page, printed, report, waiter.get(order_url).text):
        assert "cost" not in page.lower()


def test_cancel_button_only_when_allowed(world):
    waiter, manager = login("waiter"), login("manager")
    url = f"/orders/{world['order_id']}"  # has a KOT -> manager only
    assert "Cancel order" not in waiter.get(url).text
    assert "Cancel order" in manager.get(url).text
    resp = manager.post(f"{url}/cancel", data={"reason": "guests left"})
    assert resp.status_code == 303
    assert orders.get_order(world["order_id"])["status"] == "cancelled"
