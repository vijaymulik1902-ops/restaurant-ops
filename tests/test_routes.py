import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import read_session, write_session
from app.main import app
from app.models import Kot, Staff
from app.services import billing, orders, tables
from conftest import TEST_PIN, open_and_order, serve_all

NAMES = {"waiter": "Rahul", "waiter2": "Sneha", "chef": "Suresh", "counter": "Counter", "manager": "Manager"}
HOME = {"waiter": "/floor", "chef": "/kitchen", "counter": "/counter", "manager": "/counter"}
ROLES = ("waiter", "chef", "counter", "manager")


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(role: str) -> TestClient:
    """A logged-in client that sends the CSRF header on every request, like HTMX does."""
    c = TestClient(app, follow_redirects=False)
    token = csrf_from(c.get("/login").text)
    c.headers["X-CSRF-Token"] = token
    resp = c.post("/login", data={"name": NAMES[role], "pin": TEST_PIN})
    assert resp.status_code == 303, resp.text
    return c


def form_field(html: str, name: str) -> str:
    return re.search(rf'name="{name}" value="([^"]*)"', html).group(1)


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


@pytest.mark.parametrize("role", ROLES)
def test_login_redirects_by_role(db, role):
    c = login(role)
    resp = c.get("/")
    assert resp.status_code == 303 and resp.headers["location"] == HOME[role]


def test_login_page_lists_staff_and_wrong_pin_is_rejected(db):
    c = TestClient(app, follow_redirects=False)
    page = c.get("/login")
    assert page.status_code == 200 and "Rahul" in page.text and "keypad" in page.text
    c.headers["X-CSRF-Token"] = csrf_from(page.text)
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
    "/kitchen/availability": {"chef", "manager"},
    "/audit": {"manager"},
    "/menu": {"manager"},
    "/reports/sales": {"manager"},
    "/reports/sales?preset=last_month": {"manager"},
    "/reports/sales.csv?preset=this_week": {"manager"},
    "/expenses": {"manager"},
    "/audit?action=price_change&page=2": {"manager"},
}


@pytest.mark.parametrize("role", ROLES)
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
    ("waiter", "post", "/kitchen/availability/1", {"available": "0"}),
    ("counter", "post", "/kitchen/availability/1", {"available": "0"}),
    ("chef", "post", "/items/1/cancel", {"reason": "x"}),
    ("chef", "post", "/items/1/serve", {}),
    ("counter", "post", "/menu", {"name": "X", "category": "Y", "station": "bar", "price": "10"}),
    ("waiter", "post", "/menu/1/price", {"price": "1"}),
    ("chef", "post", "/menu/1/cost", {"cost": "1"}),
    ("counter", "post", "/menu/1/availability", {"available": "0"}),
    ("counter", "post", "/menu/1/rename", {"name": "X"}),
    ("waiter", "post", "/menu/1/archive", {"archived": "1"}),
    ("counter", "post", "/expenses", {"spent_on": "2026-01-01", "category": "rent", "amount": "1"}),
    ("chef", "post", "/expenses/1/delete", {}),
    ("waiter", "post", "/expenses", {"spent_on": "2026-01-01", "category": "rent", "amount": "1"}),
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
    assert 'name="version"' not in html.split('id="kot-form"')[1].split("</form>")[0]
    kot_id = form_field(html, "kot_id")
    form = {"kot_id": kot_id, f"qty_{db['menu']['naan']}": "2",
            f"note_{db['menu']['naan']}": "extra butter", f"qty_{db['menu']['dal']}": "0"}

    before = _kot_count()
    first = c.post(f"/orders/{order_id}/kot", data=form)
    second = c.post(f"/orders/{order_id}/kot", data=form)  # double tap / retry
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"] == f"/orders/{order_id}"
    assert _kot_count() == before + 1
    order = orders.get_order(order_id)
    assert [(i["name"], i["qty"], i["note"]) for i in order["items"]] == [("Butter Naan", 2, "extra butter")]


def test_two_waiters_same_starting_page_both_succeed(db):
    opened, _ = tables.open_table(db["tables"][0], db["staff"]["waiter"], 2)
    url = f"/orders/{opened['order_id']}"
    rahul, sneha = login("waiter"), login("waiter2")
    page_a, page_b = rahul.get(url).text, sneha.get(url).text  # both loaded before either sends
    ra = rahul.post(f"{url}/kot", data={"kot_id": form_field(page_a, "kot_id"), f"qty_{db['menu']['naan']}": "1"})
    rb = sneha.post(f"{url}/kot", data={"kot_id": form_field(page_b, "kot_id"), f"qty_{db['menu']['dal']}": "1"})
    assert ra.status_code == rb.status_code == 303
    assert sorted(i["name"] for i in orders.get_order(opened["order_id"])["items"]) == ["Butter Naan", "Dal Tadka"]
    assert _kot_count() == 2


def test_failed_kot_rerenders_with_picks_and_notes_preserved(db):
    from app.services import menu

    opened, _ = tables.open_table(db["tables"][0], db["staff"]["waiter"], 2)
    url = f"/orders/{opened['order_id']}"
    c = login("waiter")
    page = c.get(url).text
    naan, dal = db["menu"]["naan"], db["menu"]["dal"]
    menu.set_available(dal, False, db["staff"]["manager"])  # runs out while the waiter is picking
    resp = c.post(f"{url}/kot", data={
        "kot_id": form_field(page, "kot_id"),
        f"qty_{naan}": "3", f"note_{naan}": "well done",
        f"qty_{dal}": "1", f"note_{dal}": "no garlic",
    })
    assert resp.status_code == 422
    html = resp.text
    assert "Dal Tadka is not available" in html
    assert re.search(rf'name="qty_{naan}" value="3"', html)
    assert re.search(rf'name="note_{naan}" class="note" maxlength="120"\s+value="well done"', html)
    # A fresh KOT id (nothing was saved), and the unavailable dish is rendered disabled
    assert form_field(html, "kot_id") != form_field(page, "kot_id")
    assert re.search(rf'name="qty_{dal}" value="1"[^>]*disabled', html)
    assert _kot_count() == 0


@pytest.mark.parametrize("data", [
    {"qty_1": "abc"}, {"qty_1": "-5"}, {"qty_1": "100000"}, {"qty_99999999999999999999": "1"},
    {"qty_1": "1", "note_1": "x" * 5000}, {"qty_1": "9" * 5000},
])
def test_bad_kot_input_is_a_friendly_rerender_not_a_crash(db, data):
    opened, _ = tables.open_table(db["tables"][0], db["staff"]["waiter"], 2)
    url = f"/orders/{opened['order_id']}"
    c = login("waiter")
    resp = c.post(f"{url}/kot", data={"kot_id": form_field(c.get(url).text, "kot_id"), **data})
    assert resp.status_code == 422 and 'class="flash flash-error"' in resp.text
    assert _kot_count() == 0


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
    form = {"kot_id": form_field(html, "kot_id"), f"qty_{db['menu']['naan']}": "2"}
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
    preview = counter.get(f"/counter/orders/{order_id}").text
    resp = counter.post(f"/counter/orders/{order_id}/bill",
                        data={"discount": "9", "version": form_field(preview, "version")})
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
    version = form_field(manager.get(url).text.split(f'action="{url}/cancel"')[1], "version")
    resp = manager.post(f"{url}/cancel", data={"reason": "guests left", "version": version})
    assert resp.status_code == 303
    assert orders.get_order(world["order_id"])["status"] == "cancelled"
