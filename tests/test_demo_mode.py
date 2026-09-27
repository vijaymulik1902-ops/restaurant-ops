"""DEMO_MODE: PIN 1111 works for everyone, stored hashes unchanged; off = nothing changes."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth, config
from app.db import read_session
from app.main import app
from app.models import Staff
from conftest import PIN_HASH, TEST_PIN
from test_routes import csrf_from


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)


@pytest.mark.parametrize("name", ["Rahul", "Suresh", "Counter", "Manager"])
def test_demo_mode_accepts_1111_for_everyone(db, demo, name):
    assert auth.login(name, "1111").name == name
    assert auth.login(name, TEST_PIN).name == name  # the real PIN still works too
    with read_session() as s:  # stored hashes unchanged
        assert all(h == PIN_HASH for h in s.scalars(select(Staff.pin_hash)))


def test_demo_mode_skips_six_digit_manager_rule(db, demo):
    assert auth.pin_length_for("manager") == 4
    names = {s["name"]: s["pin_length"] for s in auth.active_staff_names()}
    assert names["Manager"] == 4


def test_demo_mode_still_rejects_other_wrong_pins(db, demo):
    with pytest.raises(auth.AuthError):
        auth.login("Rahul", "9999")


def test_demo_off_nothing_changes(db, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", False)
    with pytest.raises(auth.AuthError):
        auth.login("Rahul", "1111")
    assert auth.pin_length_for("manager") == 6
    assert auth.login("Rahul", TEST_PIN).name == "Rahul"


def test_badge_on_login_and_header_only_in_demo(db, monkeypatch):
    c = TestClient(app, follow_redirects=False)
    monkeypatch.setattr(config, "DEMO_MODE", False)
    assert "Demo mode" not in c.get("/login").text
    monkeypatch.setattr(config, "DEMO_MODE", True)
    page = c.get("/login").text
    assert "Demo mode" in page
    c.headers["X-CSRF-Token"] = csrf_from(page)
    assert c.post("/login", data={"name": "Manager", "pin": "1111"}).status_code == 303
    assert "Demo mode" in c.get("/staff").text


def test_refuses_to_start_with_demo_and_secure_cookies():
    with pytest.raises(RuntimeError, match="DEMO_MODE"):
        auth.check_production_settings(secret_key="a-real-key", cookie_secure=True, demo_mode=True)
    auth.check_production_settings(secret_key="a-real-key", cookie_secure=False, demo_mode=True)   # local demo ok
    auth.check_production_settings(secret_key="a-real-key", cookie_secure=True, demo_mode=False)   # production ok


def test_config_reads_demo_mode_from_env(monkeypatch):
    import importlib

    for value, expected in [("true", True), ("1", True), ("false", False), ("", False)]:
        monkeypatch.setenv("DEMO_MODE", value)
        assert importlib.reload(config).DEMO_MODE is expected
    monkeypatch.delenv("DEMO_MODE")
    assert importlib.reload(config).DEMO_MODE is False
