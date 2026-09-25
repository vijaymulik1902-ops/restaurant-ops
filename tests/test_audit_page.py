"""/audit renders against realistic data: 7 days of generated history plus every action type.

Regression: the filter form submits empty fields (?date_from=&date_to=&staff_id=&action=...),
which used to be rejected as invalid and bounced the manager to the home screen.
"""
from datetime import date

import pytest
from sqlalchemy import select

from app import seed as seed_module
from app.db import read_session
from app.history import generate_history
from app.models import MenuItem, Staff
from app.services import audit, backups, expenses, kitchen, menu, menu_admin, orders, staff_admin, tables
from conftest import PIN_HASH, new_kot_id
from test_routes import login

TODAY = date(2026, 9, 25)


@pytest.fixture
def busy_audit(db, monkeypatch, tmp_path):
    """Seeded restaurant + 7 days of history + one of every audited action."""
    monkeypatch.setattr(seed_module, "_hash", lambda pin: PIN_HASH)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    seed_module.seed(table_count=6, reset=True)  # small floor keeps the suite fast; still 3+ pages
    generate_history(7, seed_value=42, today=TODAY)
    with read_session() as s:
        manager = s.scalar(select(Staff.id).where(Staff.role == "manager"))
        rahul = s.scalar(select(Staff.id).where(Staff.name == "Rahul"))
        dishes = {m.name: m.id for m in s.scalars(select(MenuItem))}
        table_a = next(t["table_id"] for t in tables.list_tables("A") if t["status"] == "available")
    naan, dal = dishes["Butter Naan"], dishes["Dal Tadka"]

    # Actions the history generator doesn't produce (it already makes bills, discounts,
    # payments, cancels and expenses)
    menu_admin.set_price(naan, 5000, manager)
    menu_admin.set_cost(naan, 1300, manager)
    added, _ = menu_admin.add_dish("Masala Papad", "Starters", "kitchen", 6000, 1500, manager)
    menu_admin.rename_dish(added["menu_item_id"], "Masala Papad (2 pc)", manager)
    menu_admin.set_archived(added["menu_item_id"], True, manager)
    menu_admin.set_archived(added["menu_item_id"], False, manager)
    menu.set_available(dal, False, manager)
    e = expenses.add_expense(TODAY, "other", 25000, "Gas refill", manager)
    expenses.delete_expense(e["expense_id"], "entered twice", manager)
    staff_admin.change_pin(rahul, "4821", "4821", manager)
    staff_admin.set_active(rahul, False, manager)
    staff_admin.set_active(rahul, True, manager)
    path, _ = backups.fresh_backup_for_download(manager)
    path.unlink()
    # A cancel of a READY item (highlighted) and a whole-order cancel
    opened, _ = tables.open_table(table_a, rahul, 2)
    orders.send_kot(opened["order_id"], new_kot_id(), rahul, [(naan, 1, None)])
    item = orders.get_order(opened["order_id"])["items"][0]["item_id"]
    kitchen.start_item(item, "tandoor")
    kitchen.ready_item(item, "tandoor")
    kitchen.cancel_item(item, "Dropped while plating", manager)
    orders.cancel_order(opened["order_id"], "Guests left", manager)
    return {"manager": manager, "rahul": rahul}


def test_every_action_type_is_present(busy_audit):
    counts = {a: audit.list_audit(action=a)["total"] for a in audit.ACTIONS}
    assert all(n >= 1 for n in counts.values()), {a: n for a, n in counts.items() if not n}


def test_audit_page_renders_everything(busy_audit):
    c = login("manager")
    first = c.get("/audit")
    assert first.status_code == 200
    assert "Cancelled after it was ready" in first.text and 'class="flagged"' in first.text
    assert "Dropped while plating" in first.text

    # Exactly what the filter form sends: every field, empty ones as ""
    blank = c.get("/audit?date_from=&date_to=&staff_id=&action=")
    assert blank.status_code == 200 and "Audit log" in blank.text

    for action in audit.ACTIONS:
        resp = c.get(f"/audit?date_from=&date_to=&staff_id=&action={action}")
        assert resp.status_code == 200, action
        assert "No entries match" not in resp.text, action

    ranged = c.get("/audit?date_from=2026-09-18&date_to=2026-09-24&staff_id=&action=bill_paid")
    assert ranged.status_code == 200 and "bill paid" in ranged.text
    by_staff = c.get(f"/audit?date_from=&date_to=&staff_id={busy_audit['manager']}&action=")
    assert by_staff.status_code == 200 and "Manager" in by_staff.text


def test_audit_pagination_every_page(busy_audit):
    c = login("manager")
    result = audit.list_audit()
    assert result["pages"] >= 3  # a week of history is several pages
    seen = 0
    for page in range(1, result["pages"] + 1):
        resp = c.get(f"/audit?page={page}")
        assert resp.status_code == 200, page
        assert f"Page {page} of {result['pages']}" in resp.text
        seen += len(audit.list_audit(page=page)["rows"])
    assert seen == result["total"]
    # Pager links keep the filters
    filtered = c.get("/audit?action=bill_generated&page=1").text
    assert "action=bill_generated" in filtered.split('class="pager"')[1]


def test_audit_never_shows_a_pin(busy_audit):
    html = login("manager").get("/audit?action=pin_changed").text
    assert "pin changed" in html and "4821" not in html


@pytest.mark.parametrize("url", [
    "/expenses?start=&end=2026-09-25",
    "/reports/sales?preset=custom&start=&end=2026-09-20",
    "/reports/day-close?day=",
    "/audit?date_from=2026-09-20&date_to=&staff_id=&action=",
])
def test_other_forms_with_a_cleared_date_field(busy_audit, url):
    resp = login("manager").get(url)
    assert resp.status_code == 200, url
