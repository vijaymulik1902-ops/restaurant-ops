"""Test setup: a temp DB and fixed GST, set BEFORE any app module is imported."""
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="restaurant-tests-"))
os.environ["DB_PATH"] = str(_TMP / "test.db")
os.environ["GST_PERCENT"] = "5"

import bcrypt  # noqa: E402
import pytest  # noqa: E402

from app.db import Base, init_db, write_engine, write_session  # noqa: E402
from app.models import DiningTable, MenuItem, Staff  # noqa: E402

SERVICE_MODULES = ("tables", "orders", "kitchen", "billing", "reports")
TEST_PIN = "1234"
# Low bcrypt cost keeps the suite fast; every test staff member uses TEST_PIN
PIN_HASH = bcrypt.hashpw(TEST_PIN.encode(), bcrypt.gensalt(rounds=4)).decode()


def reset_and_seed() -> dict:
    """Drop and recreate every table, then add minimal staff, 3 tables and a 4-dish menu."""
    Base.metadata.drop_all(write_engine)
    init_db()
    with write_session() as s:
        staff = {
            "waiter": Staff(name="Rahul", role="waiter", section="A", pin_hash=PIN_HASH),
            "waiter2": Staff(name="Sneha", role="waiter", section="B", pin_hash=PIN_HASH),
            "chef_tandoor": Staff(name="Suresh", role="chef", station="tandoor", pin_hash=PIN_HASH),
            "chef_kitchen": Staff(name="Mahesh", role="chef", station="kitchen", pin_hash=PIN_HASH),
            "counter": Staff(name="Counter", role="counter", pin_hash=PIN_HASH),
            "manager": Staff(name="Manager", role="manager", pin_hash=PIN_HASH),
        }
        s.add_all(staff.values())
        tables = [DiningTable(number=n, capacity=4, section="A" if n <= 2 else "B") for n in (1, 2, 3)]
        s.add_all(tables)
        menu = {
            "naan": MenuItem(name="Butter Naan", category="Breads", station="tandoor",
                             price_paise=4500, cost_paise=1200),
            "dal": MenuItem(name="Dal Tadka", category="Mains", station="kitchen",
                            price_paise=18000, cost_paise=4500),
            "lassi": MenuItem(name="Sweet Lassi", category="Beverages", station="bar",
                              price_paise=7000, cost_paise=2200),
            "off": MenuItem(name="Fish Curry", category="Mains", station="kitchen",
                            price_paise=35000, cost_paise=15000, available=False),
        }
        s.add_all(menu.values())
        s.flush()
        ids = {
            "staff": {k: v.id for k, v in staff.items()},
            "tables": [t.id for t in tables],
            "menu": {k: v.id for k, v in menu.items()},
        }
    return ids


@pytest.fixture(autouse=True)
def db():
    """Fresh schema and minimal data for every test (no bcrypt, so it stays fast)."""
    from app.auth import limiter

    ids = reset_and_seed()
    limiter.reset()
    yield ids
    write_engine.dispose()


class Clock:
    """Controllable replacement for app.db.now inside the service modules."""

    def __init__(self, start: datetime):
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> None:
        self.current += timedelta(**kwargs)


@pytest.fixture
def clock(monkeypatch):
    c = Clock(datetime(2026, 9, 25, 12, 0, 0))
    import importlib

    for name in SERVICE_MODULES:
        module = importlib.import_module(f"app.services.{name}")
        if hasattr(module, "now"):
            monkeypatch.setattr(module, "now", c)
    return c


def new_kot_id() -> str:
    return str(uuid.uuid4())


def open_and_order(ids: dict, lines: list[tuple[str, int]], table_index: int = 0) -> dict:
    """Seat table, send one KOT of (menu key, qty) lines. Returns the KOT result."""
    from app.services import orders, tables

    opened, _ = tables.open_table(ids["tables"][table_index], ids["staff"]["waiter"], 2)
    items = [(ids["menu"][key], qty, None) for key, qty in lines]
    kot, _ = orders.send_kot(opened["order_id"], new_kot_id(), ids["staff"]["waiter"], items)
    return kot


def serve_all(ids: dict, order_id: int) -> None:
    """Push every live item of an order through to served."""
    from app.services import kitchen, orders

    for item in orders.get_order(order_id)["items"]:
        if item["status"] == "pending":
            kitchen.start_item(item["item_id"], item["station"])
            kitchen.ready_item(item["item_id"], item["station"])
            kitchen.serve_item(item["item_id"], ids["staff"]["waiter"])
