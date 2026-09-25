"""Seed demo data.

Usage:
    python -m app.seed                  # 20 tables, keeps existing data
    python -m app.seed --reset          # wipe and reseed
    python -m app.seed --reset --tables 150
    python -m app.seed --reset --history 30 [--seed-value 42]   # plus 30 days of demo history
    python -m app.seed --reset --tables 150 --loadtest-staff 30   # extra "LT ..." logins for locust
    python -m app.seed --random-pins    # random PINs (6-digit manager); on existing data: rotate all PINs
"""
import argparse
import math
import secrets

import bcrypt
from sqlalchemy import select

from app.config import DB_PATH
from app.db import Base, init_db, write_engine, write_session
from app.models import DiningTable, MenuItem, Staff

# name, role, section/station, pin
WAITERS = ["Rahul", "Sneha", "Amit", "Pooja", "Rohan", "Kiran", "Neha"]
CHEFS = [("Suresh", "tandoor"), ("Mahesh", "kitchen"), ("Ganesh", "bar")]
COUNTER = [("Counter", "counter"), ("Manager", "manager")]

# name, category, station, price in rupees, approx cost per plate in rupees
MENU = [
    ("Paneer Tikka", "Starters", "tandoor", 240, 95),
    ("Chicken Tikka", "Starters", "tandoor", 280, 120),
    ("Veg Manchurian", "Starters", "kitchen", 190, 60),
    ("Crispy Corn", "Starters", "kitchen", 180, 55),
    ("Tandoori Roti", "Breads", "tandoor", 25, 6),
    ("Butter Naan", "Breads", "tandoor", 45, 12),
    ("Garlic Naan", "Breads", "tandoor", 60, 16),
    ("Paneer Butter Masala", "Mains", "kitchen", 260, 100),
    ("Dal Tadka", "Mains", "kitchen", 180, 45),
    ("Veg Kolhapuri", "Mains", "kitchen", 220, 70),
    ("Chicken Kolhapuri", "Mains", "kitchen", 300, 130),
    ("Butter Chicken", "Mains", "kitchen", 320, 140),
    ("Jeera Rice", "Rice", "kitchen", 140, 35),
    ("Veg Biryani", "Rice", "kitchen", 220, 70),
    ("Chicken Biryani", "Rice", "kitchen", 280, 115),
    ("Masala Chaas", "Beverages", "bar", 50, 12),
    ("Sweet Lassi", "Beverages", "bar", 70, 22),
    ("Fresh Lime Soda", "Beverages", "bar", 60, 14),
    ("Cold Coffee", "Beverages", "bar", 110, 35),
    ("Mineral Water", "Beverages", "bar", 20, 10),
]


def _hash(pin: str) -> str:
    return bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()


def _random_pin(role: str) -> str:
    """4 random digits (6 for managers), never one digit repeated. Printed once, only hashed."""
    length = 6 if role == "manager" else 4
    while True:
        pin = "".join(str(secrets.randbelow(10)) for _ in range(length))
        if len(set(pin)) > 1:
            return pin


def _print_pins(title: str, rows: list[tuple[str, str, str]]) -> None:
    print(f"\n{title}\nWrite these down now: they are not stored anywhere and will not be shown again.\n")
    print(f"{'Name':<10} {'Role':<8} PIN")
    for name, role, pin in rows:
        print(f"{name:<10} {role:<8} {pin}")


def rotate_all_pins() -> None:
    """Give every active staff member a new random PIN (e.g. after a lost manager PIN)."""
    rows = []
    with write_session() as s:
        for staff in s.scalars(select(Staff).where(Staff.active.is_(True)).order_by(Staff.role, Staff.name)):
            pin = _random_pin(staff.role)
            staff.pin_hash = _hash(pin)
            staff.pin_version = (staff.pin_version or 1) + 1  # old sessions are logged out
            rows.append((staff.name, staff.role, pin))
    _print_pins("New random PINs for all active staff", rows)


def seed(table_count: int, reset: bool, random_pins: bool = False) -> None:
    if reset:
        Base.metadata.drop_all(write_engine)
    init_db()

    with write_session() as s:
        already_seeded = s.scalar(select(Staff.id).limit(1)) is not None
    if already_seeded:
        if random_pins:
            rotate_all_pins()
        else:
            print("Data already present. Use --reset to wipe and reseed.")
        return

    with write_session() as s:

        sections = [chr(ord("A") + i) for i in range(len(WAITERS))]
        credentials = []

        for i, name in enumerate(WAITERS):
            pin = _random_pin("waiter") if random_pins else f"{i + 1}{i + 1}{i + 1}{i + 1}"  # 1111, 2222, ...
            s.add(Staff(name=name, role="waiter", section=sections[i], pin_hash=_hash(pin)))
            credentials.append((name, "waiter", f"section {sections[i]}", pin))

        for i, (name, station) in enumerate(CHEFS):
            pin = _random_pin("chef") if random_pins else f"8{i + 1}8{i + 1}"  # 8181, 8282, 8383
            s.add(Staff(name=name, role="chef", station=station, pin_hash=_hash(pin)))
            credentials.append((name, "chef", station, pin))

        for i, (name, role) in enumerate(COUNTER):
            pin = _random_pin(role) if random_pins else f"9{i}9{i}"  # 9090, 9191
            s.add(Staff(name=name, role=role, pin_hash=_hash(pin)))
            credentials.append((name, role, "-", pin))

        # Split tables into contiguous blocks, one block per waiter section
        per_section = math.ceil(table_count / len(sections))
        capacities = [2, 4, 4, 6]
        for n in range(1, table_count + 1):
            s.add(
                DiningTable(
                    number=n,
                    capacity=capacities[n % len(capacities)],
                    section=sections[(n - 1) // per_section],
                )
            )

        for name, category, station, rupees, cost_rupees in MENU:
            s.add(
                MenuItem(
                    name=name,
                    category=category,
                    station=station,
                    price_paise=rupees * 100,
                    cost_paise=cost_rupees * 100,
                )
            )

    print(f"Seeded {DB_PATH}")
    print(f"{table_count} tables across sections {sections[0]}-{sections[-1]}, {len(MENU)} menu items\n")
    if random_pins:
        print("Random PINs: write these down now. They are stored only as hashes and won't be shown again.\n")
    print(f"{'Name':<10} {'Role':<8} {'Covers':<12} PIN")
    for name, role, covers, pin in credentials:
        print(f"{name:<10} {role:<8} {covers:<12} {pin}")


LOADTEST_PIN = "2468"  # load-test logins only; never create these on a real deployment


def add_loadtest_staff(count: int) -> None:
    """Add `count` waiters ("LT Waiter 01".., sections round-robin) and `count` chefs
    ("LT Chef 01".., stations round-robin), all with LOADTEST_PIN. Skips names that exist."""
    with write_session() as s:
        existing = set(s.scalars(select(Staff.name)))
        sections = sorted({t.section for t in s.scalars(select(DiningTable))})
        if not sections:
            raise SystemExit("Seed tables before adding load-test staff")
        pin_hash = _hash(LOADTEST_PIN)
        added = 0
        for i in range(1, count + 1):
            for name, kwargs in (
                (f"LT Waiter {i:02d}", {"role": "waiter", "section": sections[(i - 1) % len(sections)]}),
                (f"LT Chef {i:02d}", {"role": "chef", "station": CHEFS[(i - 1) % len(CHEFS)][1]}),
            ):
                if name not in existing:
                    s.add(Staff(name=name, pin_hash=pin_hash, **kwargs))
                    added += 1
    print(f"Load-test staff: {added} added (LT Waiter 01-{count:02d}, LT Chef 01-{count:02d}), "
          f"PIN {LOADTEST_PIN}. For load testing only.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tables", type=int, default=20)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--history", type=int, default=0, metavar="N",
                        help="also simulate N past business days through the real services")
    parser.add_argument("--seed-value", type=int, default=42, help="random seed for --history (default 42)")
    parser.add_argument("--random-pins", action="store_true",
                        help="random PINs (4 digits, 6 for the manager), printed once; on existing data, rotate all")
    parser.add_argument("--loadtest-staff", type=int, default=0, metavar="N",
                        help="add N extra waiters and N extra chefs for the locust load test")
    args = parser.parse_args()
    if args.tables < 1:
        raise SystemExit("--tables must be at least 1")
    seed(args.tables, args.reset, random_pins=args.random_pins)
    if args.loadtest_staff:
        add_loadtest_staff(args.loadtest_staff)
    if args.history:
        from app.history import HistoryError, generate_history, print_summary
        from app.migrations import ensure_schema

        ensure_schema()
        try:
            print_summary(generate_history(args.history, args.seed_value))
        except HistoryError as e:
            raise SystemExit(str(e))
