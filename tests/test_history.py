"""`python -m app.seed --history N`: realistic past days generated through the real services."""
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app import seed as seed_module
from app.db import read_session
from app.history import HistoryError, generate_history
from app.models import AuditLog, Bill, Order, OrderItem
from app.services import business_day_of, reports, sales
from app.services.insights import insight_cards
from conftest import PIN_HASH

TODAY = date(2026, 9, 25)


@pytest.fixture
def seeded(db, monkeypatch):
    """The real seed (12 tables, full menu, all staff), minus slow bcrypt."""
    monkeypatch.setattr(seed_module, "_hash", lambda pin: PIN_HASH)
    seed_module.seed(table_count=12, reset=True)


def test_seven_days_of_history(seeded):
    result = generate_history(7, seed_value=42, today=TODAY)
    first, last = TODAY - timedelta(days=7), TODAY - timedelta(days=1)
    assert (result["first"], result["last"]) == (first, last)

    with read_session() as s:
        paid_at = list(s.scalars(select(Bill.paid_at)))
        bills = s.execute(select(func.count(Bill.id), func.sum(Bill.subtotal_paise), func.sum(Bill.discount_paise),
                                 func.sum(Bill.gst_paise), func.sum(Bill.total_paise))).one()
        unfinished = s.scalar(select(func.count(Order.id)).where(Order.status.in_(("open", "billed"))))
        live_items = s.scalar(select(func.count(OrderItem.id))
                              .where(OrderItem.status.in_(("pending", "preparing", "ready"))))
        audit_actions = dict(s.execute(select(AuditLog.action, func.count(AuditLog.id)).group_by(AuditLog.action)).all())

    # Paid bills on each of the 7 business days, none outside them, nothing left open
    assert all(p is not None for p in paid_at)
    days_with_bills = {business_day_of(p) for p in paid_at}
    assert days_with_bills == {first + timedelta(days=i) for i in range(7)}
    assert unfinished == 0 and live_items == 0
    # Late-night tables paid after midnight still belong to the evening's business day
    assert any(p.hour < 4 for p in paid_at)

    # Zero day-close mismatches on every generated day
    for i in range(7):
        assert reports.day_close(first + timedelta(days=i))["mismatches"] == []

    # The sales summary reconciles with the raw bills
    t = sales.sales_summary(first, last)["totals"]
    count, subtotal, discount, gst, total = bills
    assert t["bill_count"] == count == result["totals"]["bill_count"]
    assert t["gross_sales_paise"] == subtotal
    assert t["discounts_paise"] == discount
    assert t["net_sales_paise"] == subtotal - discount
    assert t["gst_paise"] == gst
    assert t["collected_paise"] == total == t["net_sales_paise"] + t["gst_paise"]
    assert t["gross_profit_paise"] == t["net_sales_paise"] - t["cost_of_goods_paise"]
    assert t["net_profit_paise"] == t["gross_profit_paise"] - t["operating_expenses_paise"]

    # Realistic shape: some discounts (a few big ones), a few cancellations, audit rows written normally
    assert 0 < result["discounts"] < count * 0.3
    assert result["cancelled_items"] < result["items"] * 0.1
    assert audit_actions["bill_generated"] == count and audit_actions["bill_paid"] == count
    assert audit_actions.get("bill_discount", 0) == result["discounts"]
    assert audit_actions.get("item_cancel", 0) + audit_actions.get("order_cancel", 0) >= 1
    assert t["ingredient_purchases_paise"] > 0


def test_history_is_deterministic(seeded):
    a = generate_history(3, seed_value=7, today=TODAY)
    seed_module.seed(table_count=12, reset=True)
    b = generate_history(3, seed_value=7, today=TODAY)
    assert a["totals"] == b["totals"] and a["items"] == b["items"]


def test_history_refuses_a_restaurant_with_orders(seeded):
    generate_history(1, today=TODAY)
    with pytest.raises(HistoryError, match="--reset"):
        generate_history(1, today=TODAY)


def test_peaks_at_lunch_and_dinner_and_busier_weekends(seeded):
    result = generate_history(7, seed_value=42, today=TODAY)
    r = sales.sales_summary(result["first"], result["last"])
    by_hour = {h["hour"]: h["bills"] for h in r["hours"]}
    lunch, dinner = sum(by_hour[h] for h in range(12, 15)), sum(by_hour[h] for h in range(19, 23))
    off_peak = sum(by_hour[h] for h in (9, 10, 16, 17))
    assert lunch > 0 and dinner > lunch and off_peak == 0
    per_day = {d["day"]: d["bills"] for d in r["daily"]}
    weekend = [v for d, v in per_day.items() if d.weekday() >= 5]
    weekday = [v for d, v in per_day.items() if d.weekday() < 5]
    assert sum(weekend) / len(weekend) > 1.2 * sum(weekday) / len(weekday)


def test_thirty_days_have_weekend_bookings_and_at_least_three_no_shows(seeded):
    from app.models import Booking

    result = generate_history(30, seed_value=42, today=TODAY)  # 8 weekend days x 3 bookings (12 tables)
    with read_session() as s:
        rows = s.execute(select(Booking.status, Booking.starts_at, Booking.order_id)).all()
    statuses = [st for st, _, _ in rows]
    assert result["bookings"] == len(rows) > 20
    assert set(statuses) <= {"completed", "no_show", "cancelled"}                  # nothing left hanging
    assert all(ts.weekday() >= 5 and 19 <= ts.hour < 22 for _, ts, _ in rows)     # weekend dinners only
    assert all(order_id for st, _, order_id in rows if st == "completed")
    no_shows = statuses.count("no_show")
    assert no_shows >= 3 and no_shows == result["no_shows"]
    assert no_shows <= 0.15 * len(rows)                                             # about 8%, not a flood
    card = {c["key"]: c for c in insight_cards(TODAY - timedelta(days=30), TODAY - timedelta(days=1))}["no_shows"]
    assert card["figure"] not in ("0.0%", "—")


def test_no_show_picks_are_about_8_percent():
    from app.history import _no_show_picks

    assert _no_show_picks(0) == set() and len(_no_show_picks(6)) == 0     # a short run: none forced
    assert len(_no_show_picks(24)) == 3 and len(_no_show_picks(43)) == 3
    assert len(_no_show_picks(100)) == 8 and _no_show_picks(43) == _no_show_picks(43)  # deterministic
    assert all(0 <= i < 43 for i in _no_show_picks(43))


def test_history_does_not_book_today(seeded):
    from app.db import now
    from app.models import Booking
    from app.services import business_day_bounds

    generate_history(7)  # real "today": bookings only on past days (demo ones need --demo-bookings)
    today_start = business_day_bounds(business_day_of(now()))[0]
    with read_session() as s:
        assert s.scalar(select(func.count(Booking.id)).where(Booking.starts_at >= today_start)) == 0
