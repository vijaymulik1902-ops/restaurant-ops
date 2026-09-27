"""Staff are listed in seniority order everywhere: manager, counter, chefs by station,
waiters by section, then name."""
import pytest

from app import auth
from app import seed as seed_module
from app.ordering import seniority_key
from app.services import audit, staff_admin
from conftest import PIN_HASH
from test_routes import login

EXPECTED = ["Manager", "Counter", "Suresh", "Mahesh", "Ganesh",            # chefs: tandoor, kitchen, bar
            "Rahul", "Sneha", "Amit", "Pooja", "Rohan", "Kiran", "Neha"]   # waiters: sections A..G


@pytest.fixture
def seeded(db, monkeypatch):
    monkeypatch.setattr(seed_module, "_hash", lambda pin: PIN_HASH)
    seed_module.seed(table_count=7, reset=True)


def test_login_list_order(seeded):
    assert [s["name"] for s in auth.active_staff_names()] == EXPECTED


def test_staff_page_and_audit_filter_order(seeded):
    assert [p["name"] for p in staff_admin.list_staff()] == EXPECTED
    assert [s["name"] for s in audit.list_audit()["staff_options"]] == EXPECTED


def test_login_screen_renders_in_order(seeded):
    from fastapi.testclient import TestClient

    from app.main import app

    html = TestClient(app).get("/login").text
    positions = [html.index(f'value="{n}"') for n in EXPECTED]
    assert positions == sorted(positions)


def test_ties_break_by_name_and_unknowns_go_last():
    rows = [("waiter", None, "B", "zed"), ("waiter", None, "B", "Amy"), ("chef", "bar", None, "Al"),
            ("chef", None, None, "Bo"), ("manager", None, None, "Zoe"), ("waiter", None, None, "Nosection")]
    ordered = sorted(rows, key=lambda r: seniority_key(*r))
    assert [r[3] for r in ordered] == ["Zoe", "Al", "Bo", "Amy", "zed", "Nosection"]


def test_login_badges_have_role_rings_initials_and_labels(seeded):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.text import initials, role_label

    html = TestClient(app).get("/login").text
    assert html.count('class="pick role-manager"') == 1 and html.count('class="pick role-chef"') == 3
    assert html.count('class="pick role-waiter"') == 7 and html.count('class="pick role-counter"') == 1
    assert '<span class="initials" aria-hidden="true">RA</span>' in html
    assert "Chef · tandoor" in html and "Waiter · A" in html
    assert 'id="pin-panel"' in html and 'class="pin-panel waiting"' in html and "PIN keypad" in html
    assert initials("Rahul Shah") == "RS" and initials("Rahul") == "RA" and initials("") == "?"
    assert role_label("manager") == "Manager" and role_label("waiter", None, "C") == "Waiter · C"
