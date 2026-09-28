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
    assert 'id="pin-panel"' in html and "PIN keypad" in html
    assert initials("Rahul Shah") == "RS" and initials("Rahul") == "RA" and initials("") == "?"
    assert role_label("manager") == "Manager" and role_label("waiter", None, "C") == "Waiter · C"


def test_login_layout_hides_pin_until_a_badge_is_chosen(seeded):
    """Normal flow: name, hint, role-grouped badges in seniority order, then the PIN section,
    which is hidden until a badge is picked (and forced visible when JavaScript is off)."""
    import re

    from fastapi.testclient import TestClient

    from app.main import app
    from test_routes import csrf_from

    c = TestClient(app, follow_redirects=False)
    html = c.get("/login").text
    assert re.search(r'<section class="pin-panel" id="pin-panel"[^>]*\bhidden\b', html)
    assert '<noscript><style>#pin-panel[hidden] { display: block !important; }</style></noscript>' in html
    # order on the page: hint -> badge grid -> PIN section
    hint, grid, pin = html.index("Tap your badge"), html.index('class="staff-pick"'), html.index('id="pin-panel"')
    assert hint < grid < pin
    assert re.findall(r'<h2 class="pick-group-title">([^<]+)</h2>', html) == ["Management", "Kitchen", "Floor"]
    assert re.findall(r'name="name" value="([^"]+)"', html) == EXPECTED
    # after a wrong PIN the page comes back with that person chosen and the PIN section showing
    c.headers["X-CSRF-Token"] = csrf_from(html)
    again = c.post("/login", data={"name": "Rahul", "pin": "0000"}).text
    assert re.search(r'<section class="pin-panel" id="pin-panel"[^>]*>', again).group(0).count("hidden") == 0
    assert "PIN for Rahul" in again


def test_login_css_has_no_overlay_positioning():
    """The old login floated the PIN panel over the badges (sticky + translucent)."""
    from pathlib import Path

    css = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text()
    block = css[css.index("Login: role-grouped staff badges"):css.index("Manager screens")]
    assert "sticky" not in block and "position: fixed" not in block and "margin-top: -" not in block
    assert "repeat(auto-fill, minmax(5.75rem, 1fr))" in block
    assert ".pick-grid > label.pick" in block  # beats Pico's label:has([type=radio]) { width: fit-content }
