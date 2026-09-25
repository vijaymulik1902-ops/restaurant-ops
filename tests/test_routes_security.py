"""CSRF, cookie flags, single-item cancel and dish availability through the real app."""
import re
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import auth
from app.main import app
from app.services import kitchen, menu, orders, tables
from conftest import TEST_PIN, open_and_order
from test_routes import csrf_from, login

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


# ---------- A4: CSRF ----------

def test_post_without_token_is_403(db):
    c = login("waiter")
    del c.headers["X-CSRF-Token"]
    resp = c.post(f"/floor/tables/{db['tables'][0]}/open", data={"guest_count": "2"})
    assert resp.status_code == 403
    assert tables.get_table(db["tables"][0])["status"] == "available"


def test_post_with_wrong_token_is_403(db):
    c = login("waiter")
    c.headers["X-CSRF-Token"] = "forged"
    assert c.post(f"/floor/tables/{db['tables'][0]}/open", data={"guest_count": "2"}).status_code == 403


def test_post_with_token_in_form_field_works(db):
    """Plain (non-HTMX) forms send the token as a hidden field instead of a header."""
    c = login("waiter")
    token = c.headers.pop("X-CSRF-Token")
    resp = c.post(f"/floor/tables/{db['tables'][0]}/open", data={"guest_count": "2", "csrf_token": token})
    assert resp.status_code == 303
    assert tables.get_table(db["tables"][0])["status"] == "occupied"


def test_login_form_needs_token(db):
    c = TestClient(app, follow_redirects=False)
    c.get("/login")
    assert c.post("/login", data={"name": "Rahul", "pin": TEST_PIN}).status_code == 403
    token = csrf_from(c.get("/login").text)
    assert c.post("/login", data={"name": "Rahul", "pin": TEST_PIN, "csrf_token": token}).status_code == 303


def test_token_from_another_session_rejected(db):
    attacker = login("waiter")
    victim = login("counter")
    victim.headers["X-CSRF-Token"] = attacker.headers["X-CSRF-Token"]
    assert victim.post(f"/floor/tables/{db['tables'][0]}/open", data={"guest_count": "2"}).status_code == 403


def test_every_post_form_in_templates_carries_the_token():
    for path in TEMPLATES.glob("*.html"):
        html = path.read_text()
        for form in re.findall(r"<form\b[^>]*>.*?</form>", html, flags=re.S | re.I):
            opening = form.split(">", 1)[0].lower()
            if 'method="post"' in opening or "hx-post" in opening:
                assert 'name="csrf_token"' in form, f"{path.name}: {opening}"
    assert "X-CSRF-Token" in (TEMPLATES / "base.html").read_text()


# ---------- A5: session cookie ----------

def _cookie_header(secure: bool, monkeypatch) -> str:
    monkeypatch.setattr(auth, "COOKIE_SECURE", secure)
    tiny = FastAPI()
    auth.install(tiny)

    @tiny.get("/set")
    def set_it(request: Request):
        request.session["x"] = 1
        return {}

    return TestClient(tiny).get("/set").headers["set-cookie"].lower()


def test_session_cookie_flags_local(monkeypatch):
    cookie = _cookie_header(False, monkeypatch)
    assert "max-age=50400" in cookie  # 14 hours
    assert "samesite=lax" in cookie and "httponly" in cookie and "secure" not in cookie


def test_session_cookie_secure_in_production(monkeypatch):
    assert "secure" in _cookie_header(True, monkeypatch)


def test_cookie_secure_env_parsing(monkeypatch):
    import importlib

    from app import config

    for value, expected in [("true", True), ("1", True), ("false", False), ("", False)]:
        monkeypatch.setenv("COOKIE_SECURE", value)
        assert importlib.reload(config).COOKIE_SECURE is expected
    monkeypatch.delenv("COOKIE_SECURE")
    assert importlib.reload(config).COOKIE_SECURE is False


# ---------- A2: single-item cancel ----------

def _item_id(order_id):
    return orders.get_order(order_id)["items"][0]["item_id"]


def test_cancel_icon_only_when_allowed(db):
    kot = open_and_order(db, [("naan", 1)])
    url = f"/orders/{kot['order_id']}"
    waiter, manager = login("waiter"), login("manager")
    assert "Cancel 1 × Butter Naan" in waiter.get(url).text  # pending: floor staff may cancel
    kitchen.start_item(_item_id(kot["order_id"]), "tandoor")
    assert "Cancel 1 × Butter Naan" not in waiter.get(url).text  # started: manager only
    assert "Cancel 1 × Butter Naan" in manager.get(url).text


def test_cancel_item_with_quick_pick_reason(db):
    kot = open_and_order(db, [("naan", 1)])
    url = f"/orders/{kot['order_id']}"
    c = login("waiter")
    resp = c.post(f"/items/{_item_id(kot['order_id'])}/cancel",
                  data={"reason_choice": "Wrong item entered"}, headers={"referer": f"http://testserver{url}"})
    assert resp.status_code == 303 and resp.headers["location"] == url
    item = orders.get_order(kot["order_id"])["items"][0]
    assert (item["status"], item["cancel_reason"]) == ("cancelled", "Wrong item entered")
    assert kitchen.live_items("tandoor") == []


def test_cancel_item_typed_reason_wins_and_blank_is_refused(db):
    kot = open_and_order(db, [("naan", 1), ("dal", 1)])
    url = f"/orders/{kot['order_id']}"
    naan, dal = [i["item_id"] for i in orders.get_order(kot["order_id"])["items"]]
    c = login("waiter")
    ref = {"referer": f"http://testserver{url}"}
    c.post(f"/items/{naan}/cancel", data={"reason_choice": "Out of stock", "reason": "Guest allergic"}, headers=ref)
    assert orders.get_order(kot["order_id"])["items"][0]["cancel_reason"] == "Guest allergic"
    resp = c.post(f"/items/{dal}/cancel", data={"reason": "   "}, headers=ref)
    assert resp.status_code == 303
    assert "A reason is required" in c.get(url).text
    assert orders.get_order(kot["order_id"])["items"][1]["status"] == "pending"


def test_waiter_cannot_cancel_started_item_via_route(db):
    kot = open_and_order(db, [("naan", 1)])
    item_id = _item_id(kot["order_id"])
    kitchen.start_item(item_id, "tandoor")
    login("waiter").post(f"/items/{item_id}/cancel", data={"reason": "x"})
    assert orders.get_order(kot["order_id"])["items"][0]["status"] == "preparing"


# ---------- A3: availability ----------

def test_availability_page_scoped_by_role_and_shows_no_prices(db):
    chef = login("chef").get("/kitchen/availability").text
    assert "Butter Naan" in chef and "Dal Tadka" not in chef  # tandoor chef: own station only
    assert "₹" not in chef
    manager = login("manager").get("/kitchen/availability").text
    assert all(name in manager for name in ("Butter Naan", "Dal Tadka", "Sweet Lassi"))
    assert "₹" not in manager


def test_chef_toggle_greys_dish_on_order_screen(db):
    opened, _ = tables.open_table(db["tables"][0], db["staff"]["waiter"], 2)
    naan = db["menu"]["naan"]
    chef = login("chef")
    assert chef.post(f"/kitchen/availability/{naan}", data={"available": "0"}).status_code == 303
    page = login("waiter").get(f"/orders/{opened['order_id']}").text
    row = page.split(f'data-menu-id="{naan}"')[1].split('class="menu-row')[0]
    assert "Not available" in row and re.search(rf'name="qty_{naan}"[^>]*disabled', row)
    chef.post(f"/kitchen/availability/{naan}", data={"available": "1"})
    assert next(d for d in menu.list_availability() if d["menu_item_id"] == naan)["available"] is True


def test_chef_cannot_toggle_other_station_via_route(db):
    login("chef").post(f"/kitchen/availability/{db['menu']['dal']}", data={"available": "0"})
    assert next(d for d in menu.list_availability() if d["menu_item_id"] == db["menu"]["dal"])["available"]


@pytest.mark.parametrize("path", ["/orders/abc", "/orders/0", "/orders/99999999999999999999",
                                  "/kitchen/items/-1/card", "/reports/day-close?day=notadate",
                                  "/floor?all=maybe"])
def test_malformed_urls_give_friendly_redirect_not_error(db, path):
    c = login("manager")
    resp = c.get(path)
    assert resp.status_code == 303, (path, resp.status_code)
    assert "please check and try again" in c.get(resp.headers["location"], follow_redirects=True).text


def test_absurdly_long_pin_is_just_a_wrong_pin(db):
    c = TestClient(app, follow_redirects=False)
    c.headers["X-CSRF-Token"] = csrf_from(c.get("/login").text)
    resp = c.post("/login", data={"name": "Rahul", "pin": "1" * 200})
    assert resp.status_code == 401 and "Wrong name or PIN" in resp.text


# ---------- Part B hardening ----------

@pytest.mark.parametrize("referer, expected", [
    ("http://testserver/orders/5?x=1", "/orders/5?x=1"),
    ("http://evil.example//evil.example/path", "/fallback"),
    ("http://testserver/\\evil.example", "/fallback"),
    ("javascript:alert(1)", "/fallback"),
    (None, "/fallback"),
])
def test_back_url_never_leaves_the_site(referer, expected):
    from starlette.requests import Request as StarletteRequest

    from app.web import back_url

    headers = [(b"referer", referer.encode())] if referer else []
    req = StarletteRequest({"type": "http", "method": "POST", "path": "/", "headers": headers})
    assert back_url(req, fallback="/fallback") == expected


def test_day_close_absurd_date_is_friendly(db):
    c = login("counter")
    resp = c.get("/reports/day-close?day=9999-12-31")
    assert resp.status_code == 303
    assert "Pick a date between" in c.get(resp.headers["location"], follow_redirects=True).text


def test_db_busy_on_get_is_a_page_not_a_redirect_loop(db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.routers import counter

    def locked(*_a, **_k):
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    monkeypatch.setattr(counter.tables, "list_tables", locked)
    resp = login("counter").get("/counter")
    assert resp.status_code == 503 and "busy" in resp.text.lower()


def test_db_busy_on_post_flashes_and_goes_back(db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.routers import floor

    def locked(*_a, **_k):
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(floor.tables, "open_table", locked)
    c = login("waiter")
    resp = c.post(f"/floor/tables/{db['tables'][0]}/open", data={"guest_count": "2"},
                  headers={"referer": "http://testserver/floor"})
    assert resp.status_code == 303 and resp.headers["location"] == "/floor"
    assert "busy" in c.get("/floor").text.lower()


def test_unexpected_error_shows_friendly_page(db, monkeypatch):
    from app.routers import floor

    def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(floor.tables, "list_tables", boom)
    c = login("waiter")
    quiet = TestClient(app, raise_server_exceptions=False, follow_redirects=False, cookies=c.cookies)
    resp = quiet.get("/floor")
    assert resp.status_code == 500 and "Something went wrong" in resp.text and "Traceback" not in resp.text


def test_table_card_survives_inconsistent_data(db):
    from app.db import write_session
    from app.models import DiningTable

    with write_session() as s:
        s.get(DiningTable, db["tables"][0]).status = "occupied"  # occupied but no order
    c = login("waiter")
    assert c.get("/floor").status_code == 200
    assert c.get(f"/floor/tables/{db['tables'][0]}/card").status_code == 200


def test_production_refuses_dev_secret_key():
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        auth.check_production_settings(secret_key=auth.DEV_SECRET_KEY, cookie_secure=True)
    auth.check_production_settings(secret_key=auth.DEV_SECRET_KEY, cookie_secure=False)  # local dev ok
    auth.check_production_settings(secret_key="a-real-long-random-key", cookie_secure=True)


# ---------- Round 5, Part A ----------

def test_cancel_icon_respects_waiter_section(db):
    kot = open_and_order(db, [("naan", 1)])  # table 1, section A
    url = f"/orders/{kot['order_id']}"
    assert "Cancel 1 × Butter Naan" in login("waiter").get(url).text      # Rahul, section A
    other = login("waiter2")                                               # Sneha, section B
    assert "Cancel 1 × Butter Naan" not in other.get(url).text
    other.post(f"/items/{_item_id(kot['order_id'])}/cancel", data={"reason": "x"})
    assert orders.get_order(kot["order_id"])["items"][0]["status"] == "pending"
    assert "Cancel 1 × Butter Naan" in login("counter").get(url).text


def test_cancel_order_button_matches_matrix(db):
    opened, _ = tables.open_table(db["tables"][0], db["staff"]["waiter"], 2)  # Rahul's order, no KOT
    url = f"/orders/{opened['order_id']}"
    shown = {r: "Cancel this order" in login(r).get(url).text for r in ("waiter", "waiter2", "counter", "manager")}
    assert shown == {"waiter": True, "waiter2": False, "counter": True, "manager": True}


def test_stream_is_401_when_not_logged_in(db):
    resp = TestClient(app, follow_redirects=False).get("/stream")
    assert resp.status_code == 401 and "location" not in resp.headers


def test_auth_check(db):
    anon = TestClient(app)
    assert anon.get("/auth/check").status_code == 401
    c = login("chef")
    resp = c.get("/auth/check")
    assert resp.status_code == 200 and resp.json() == {"ok": True, "role": "chef"}
    assert resp.headers["cache-control"] == "no-store"


def test_logout_clears_whole_session_including_csrf(db):
    c = login("waiter")
    old_token = c.headers["X-CSRF-Token"]
    assert c.post("/logout").status_code == 303
    assert c.get("/auth/check").status_code == 401
    # The old token no longer works for anything, even on a new login attempt
    assert c.post("/login", data={"name": "Rahul", "pin": TEST_PIN}).status_code == 403
    assert csrf_from(c.get("/login").text) != old_token


def test_open_stream_ends_when_staff_deactivated(db, monkeypatch):
    import threading

    from app.db import write_session
    from app.models import Staff
    from app.routers import stream as stream_mod

    monkeypatch.setattr(stream_mod, "RECHECK_SECONDS", 0.2)
    c = login("waiter")
    done = threading.Event()

    def consume():
        with c.stream("GET", "/stream") as resp:
            assert resp.status_code == 200
            for _ in resp.iter_lines():
                pass
        done.set()

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    assert not done.wait(0.6)  # still connected while active
    with write_session() as s:
        s.get(Staff, db["staff"]["waiter"]).active = False
    assert done.wait(3), "stream should close after deactivation"
