import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from app import auth
from app.db import write_session
from app.models import Staff
from conftest import TEST_PIN


@pytest.fixture
def fake_clock(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(auth, "_clock", lambda: t["now"])
    return t


def _deactivate(staff_id: int) -> None:
    with write_session() as s:
        s.get(Staff, staff_id).active = False


@pytest.fixture
def client():
    """A tiny app using the real auth dependencies, independent of main.py."""
    app = FastAPI()
    auth.install(app)

    @app.exception_handler(auth.LoginRequired)
    async def to_login(_request, _exc):
        return RedirectResponse("/login", status_code=303)

    @app.post("/test-login")
    def do_login(request: Request, name: str, pin: str):
        auth.start_session(request, auth.login(name, pin))
        return {"ok": True}

    @app.get("/anyone")
    def anyone(staff: auth.CurrentStaff = Depends(auth.current_staff)):
        return {"name": staff.name}

    @app.get("/kitchen-only")
    def kitchen_only(staff: auth.CurrentStaff = Depends(auth.require_role("chef", "manager"))):
        return {"name": staff.name}

    return TestClient(app, follow_redirects=False)


def test_correct_pin_logs_in(db):
    staff = auth.login("Rahul", TEST_PIN)
    assert (staff.name, staff.role, staff.section) == ("Rahul", "waiter", "A")


@pytest.mark.parametrize("name, pin", [("Rahul", "9999"), ("Rahul", ""), ("Nobody", TEST_PIN)])
def test_wrong_pin_or_name_rejected(db, name, pin):
    with pytest.raises(auth.AuthError, match="Wrong name or PIN"):
        auth.login(name, pin)


def test_lockout_after_five_wrong_pins_then_allowed_after_window(db, fake_clock):
    for _ in range(5):
        with pytest.raises(auth.AuthError, match="Wrong"):
            auth.login("Rahul", "0000")
    # Even the right PIN is refused while locked out
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.login("Rahul", TEST_PIN)
    # Lockout is per staff member
    assert auth.login("Sneha", TEST_PIN).name == "Sneha"

    fake_clock["now"] += auth.LOCKOUT_WINDOW_SEC - 1
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.login("Rahul", TEST_PIN)
    fake_clock["now"] += 1
    assert auth.login("Rahul", TEST_PIN).name == "Rahul"


def test_successful_login_resets_wrong_pin_count(db, fake_clock):
    for _ in range(4):
        with pytest.raises(auth.AuthError):
            auth.login("Rahul", "0000")
    auth.login("Rahul", TEST_PIN)
    for _ in range(4):
        with pytest.raises(auth.AuthError, match="Wrong"):
            auth.login("Rahul", "0000")
    assert auth.login("Rahul", TEST_PIN).name == "Rahul"


def test_deactivated_staff_cannot_log_in(db):
    _deactivate(db["staff"]["waiter"])
    with pytest.raises(auth.AuthError):
        auth.login("Rahul", TEST_PIN)


def test_deactivated_staff_logged_out_on_next_request(db, client):
    assert client.post("/test-login", params={"name": "Rahul", "pin": TEST_PIN}).status_code == 200
    assert client.get("/anyone").json() == {"name": "Rahul"}
    _deactivate(db["staff"]["waiter"])
    resp = client.get("/anyone")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_not_logged_in_redirects_to_login(db, client):
    resp = client.get("/kitchen-only")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_require_role_returns_403_for_wrong_role(db, client):
    client.post("/test-login", params={"name": "Rahul", "pin": TEST_PIN})
    assert client.get("/kitchen-only").status_code == 403
    client.post("/test-login", params={"name": "Suresh", "pin": TEST_PIN})
    assert client.get("/kitchen-only").json() == {"name": "Suresh"}
