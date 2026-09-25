"""Seed demo data.

Usage:
    python -m app.seed                  # 20 tables, keeps existing data
    python -m app.seed --reset          # wipe and reseed
    python -m app.seed --reset --tables 150
    python -m app.seed --reset --history 30 [--seed-value 42]   # plus 30 days of demo history
"""
import argparse
import math

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


def seed(table_count: int, reset: bool) -> None:
    if reset:
        Base.metadata.drop_all(write_engine)
    init_db()

    with write_session() as s:
        if s.scalar(select(Staff.id).limit(1)) is not None:
            print("Data already present. Use --reset to wipe and reseed.")
            return

        sections = [chr(ord("A") + i) for i in range(len(WAITERS))]
        credentials = []

        for i, name in enumerate(WAITERS):
            pin = f"{i + 1}{i + 1}{i + 1}{i + 1}"  # 1111, 2222, ...
            s.add(Staff(name=name, role="waiter", section=sections[i], pin_hash=_hash(pin)))
            credentials.append((name, "waiter", f"section {sections[i]}", pin))

        for i, (name, station) in enumerate(CHEFS):
            pin = f"8{i + 1}8{i + 1}"  # 8181, 8282, 8383
            s.add(Staff(name=name, role="chef", station=station, pin_hash=_hash(pin)))
            credentials.append((name, "chef", station, pin))

        for i, (name, role) in enumerate(COUNTER):
            pin = f"9{i}9{i}"  # 9090, 9191
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
    print(f"{'Name':<10} {'Role':<8} {'Covers':<12} PIN")
    for name, role, covers, pin in credentials:
        print(f"{name:<10} {role:<8} {covers:<12} {pin}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tables", type=int, default=20)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--history", type=int, default=0, metavar="N",
                        help="also simulate N past business days through the real services")
    parser.add_argument("--seed-value", type=int, default=42, help="random seed for --history (default 42)")
    args = parser.parse_args()
    if args.tables < 1:
        raise SystemExit("--tables must be at least 1")
    seed(args.tables, args.reset)
    if args.history:
        from app.history import HistoryError, generate_history, print_summary
        from app.migrations import ensure_schema

        ensure_schema()
        try:
            print_summary(generate_history(args.history, args.seed_value))
        except HistoryError as e:
            raise SystemExit(str(e))
