"""Insight cards: one rule each, checked against hand-computed figures."""
from datetime import date, datetime

from app.services import kitchen, orders
from app.services.insights import insight_cards, operations
from conftest import open_and_order, serve_all
from test_routes import login
from test_sales import _paid

FRI, SAT = date(2026, 9, 25), date(2026, 9, 26)


def _cards(start=FRI, end=FRI) -> dict:
    return {c["key"]: c for c in insight_cards(start, end)}


def test_top_three_by_gross_profit(db, clock):
    _paid(db, [("naan", 3), ("dal", 1), ("lassi", 2)])
    # profit: dal 18000-4500=13500, naan 3x(4500-1200)=9900, lassi 2x(7000-2200)=9600
    c = _cards()["top_profit"]
    assert c["sentence"] == ("Your top 3 dishes by gross profit were Dal Tadka (₹135), "
                             "Butter Naan (₹99) and Sweet Lassi (₹96).")
    assert c["figure"] == "₹330"


def test_lowest_margin_dish(db, clock):
    _paid(db, [("naan", 3), ("dal", 1), ("lassi", 2)])  # margins 73.3 / 75.0 / 68.6
    c = _cards()["lowest_margin"]
    assert c["figure"] == "68.6%" and c["sentence"].startswith("Sweet Lassi had the lowest margin at 68.6%")


def test_slowest_sellers_include_unsold_dishes(db, clock):
    _paid(db, [("naan", 3), ("dal", 1)])
    c = _cards()["slowest"]
    # Unsold current dishes come first (Fish Curry is off today but still on the menu)
    assert c["figure"] == "0 sold"
    assert "Fish Curry (0 sold)" in c["sentence"] and "Sweet Lassi (0 sold)" in c["sentence"]


def test_weekend_vs_weekday(db, clock):
    _paid(db, [("dal", 1)])                                 # Fri: 18000
    clock.current = datetime(2026, 9, 26, 13, 0)
    _paid(db, [("dal", 2)], table_index=1)                  # Sat: 36000
    c = _cards(FRI, SAT)["weekend"]
    assert c["figure"] == "+100.0%"
    assert c["sentence"] == "Weekends averaged ₹360 a day, 100.0% more than weekdays (₹180)."
    assert "needs sales on both" in _cards()["weekend"]["sentence"]  # Friday alone


def test_peak_hour_and_busiest_day(db, clock):
    _paid(db, [("naan", 1)])                                # seated 12:00
    clock.current = datetime(2026, 9, 25, 19, 30)
    _paid(db, [("dal", 2)], table_index=1)                  # seated 19:30
    cards = _cards()
    assert cards["peak_hour"]["figure"] == "19:00"
    assert "19:00–20:00 (by seating time) with ₹360 from 1 bill" in cards["peak_hour"]["sentence"]
    assert cards["busiest_day"]["figure"] == "₹405"
    assert cards["busiest_day"]["sentence"].startswith("The busiest day was Fri 25 Sep with ₹405")


def test_cancellation_rate_and_top_reason(db, clock):
    kot = open_and_order(db, [("naan", 1), ("dal", 2), ("lassi", 1)])
    items = {i["name"]: i["item_id"] for i in orders.get_order(kot["order_id"])["items"]}
    kitchen.cancel_item(items["Sweet Lassi"], "Out of stock", db["staff"]["waiter"])
    kitchen.cancel_item(items["Butter Naan"], "Out of stock", db["staff"]["waiter"])
    c = _cards()["cancellations"]
    assert c["figure"] == "50.0%"  # 2 of 4 items
    assert c["sentence"] == "50.0% of items ordered were cancelled (2 of 4). Top reason: Out of stock (2)."


def test_discount_share(db, clock):
    _paid(db, [("naan", 2), ("dal", 1)], discount=2000)     # 2000 of 27000
    c = _cards()["discounts"]
    assert c["figure"] == "7.4%" and "(₹20 of ₹270)" in c["sentence"]


def test_kitchen_time_and_slowest_station(db, clock):
    kot = open_and_order(db, [("naan", 1), ("dal", 1)])
    items = {i["station"]: i["item_id"] for i in orders.get_order(kot["order_id"])["items"]}
    kitchen.start_item(items["tandoor"], "tandoor")
    kitchen.start_item(items["kitchen"], "kitchen")
    clock.advance(minutes=8)
    kitchen.ready_item(items["tandoor"], "tandoor")
    clock.advance(minutes=10)
    kitchen.ready_item(items["kitchen"], "kitchen")
    assert operations(FRI, FRI)["kitchen_minutes"] == {"tandoor": 8.0, "kitchen": 18.0, "bar": None}
    c = _cards()["kitchen_time"]
    assert c["figure"] == "18.0 min"
    assert c["sentence"] == ("Average time from order to ready: tandoor 8.0 min and kitchen 18.0 min. "
                             "The slowest station is kitchen.")


def test_empty_period_is_calm(db):
    cards = _cards(date(2020, 1, 6), date(2020, 1, 12))
    assert cards["top_profit"]["sentence"] == "No dishes were sold in this period."
    assert "peak_hour" not in cards and "discounts" not in cards


def test_insights_page_manager_only_and_renders(db, clock):
    _paid(db, [("dal", 1)])
    html = login("manager").get("/insights?preset=today").text
    assert "Most profitable dishes" in html and "Dal Tadka (₹135)" in html
    assert "AI chat isn&#39;t set up." in html or "AI chat isn't set up." in html  # no key in tests
    for role in ("waiter", "chef", "counter"):
        assert login(role).get("/insights").status_code == 403
    assert 'href="/insights"' in login("manager").get("/floor").text
