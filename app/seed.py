"""Seed demo data.

Usage:
    python -m app.seed                  # 20 tables, keeps existing data
    python -m app.seed --reset          # wipe and reseed
    python -m app.seed --reset --tables 150
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

# name, category, station, price in rupees
MENU = [
    ("Paneer Tikka", "Starters", "tandoor", 240),
    ("Chicken Tikka", "Starters", "tandoor", 280),
    ("Veg Manchurian", "Starters", "kitchen", 190),
    ("Crispy Corn", "Starters", "kitchen", 180),
    ("Tandoori Roti", "Breads", "tandoor", 25),
    ("Butter Naan", "Breads", "tandoor", 45),
    ("Garlic Naan", "Breads", "tandoor", 60),
    ("Paneer Butter Masala", "Mains", "kitchen", 260),
    ("Dal Tadka", "Mains", "kitchen", 180),
    ("Veg Kolhapuri", "Mains", "kitchen", 220),
    ("Chicken Kolhapuri", "Mains", "kitchen", 300),
    ("Butter Chicken", "Mains", "kitchen", 320),
    ("Jeera Rice", "Rice", "kitchen", 140),
    ("Veg Biryani", "Rice", "kitchen", 220),
    ("Chicken Biryani", "Rice", "kitchen", 280),
    ("Masala Chaas", "Beverages", "bar", 50),
    ("Sweet Lassi", "Beverages", "bar", 70),
    ("Fresh Lime Soda", "Beverages", "bar", 60),
    ("Cold Coffee", "Beverages", "bar", 110),
    ("Mineral Water", "Beverages", "bar", 20),
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

        for name, category, station, rupees in MENU:
            s.add(MenuItem(name=name, category=category, station=station, price_paise=rupees * 100))

    print(f"Seeded {DB_PATH}")
    print(f"{table_count} tables across sections {sections[0]}-{sections[-1]}, {len(MENU)} menu items\n")
    print(f"{'Name':<10} {'Role':<8} {'Covers':<12} PIN")
    for name, role, covers, pin in credentials:
        print(f"{name:<10} {role:<8} {covers:<12} {pin}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tables", type=int, default=20)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    if args.tables < 1:
        raise SystemExit("--tables must be at least 1")
    seed(args.tables, args.reset)
