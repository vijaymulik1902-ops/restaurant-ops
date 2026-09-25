from datetime import date

from app.services import billing, kitchen, orders, reports
from conftest import open_and_order, serve_all

DAY = date(2026, 9, 25)


def _pay(db, order_id, mode, discount=0):
    bill, _ = billing.generate_bill(order_id, discount, db["staff"]["manager"])
    billing.pay_bill(bill["bill_id"], mode)
    return bill


def test_day_close_sales_and_mismatches(db, clock):
    # 12:00 table 1: paid in cash
    paid_cash = open_and_order(db, [("naan", 2)], table_index=0)
    serve_all(db, paid_cash["order_id"])
    b1 = _pay(db, paid_cash["order_id"], "cash")

    # 12:00 table 2: one item cancelled, rest paid by UPI
    upi = open_and_order(db, [("dal", 1), ("lassi", 1)], table_index=1)
    lassi = next(i for i in orders.get_order(upi["order_id"])["items"] if i["name"] == "Sweet Lassi")
    kitchen.cancel_item(lassi["item_id"], "spilled", db["staff"]["waiter"])
    serve_all(db, upi["order_id"])
    b2 = _pay(db, upi["order_id"], "upi")

    report = reports.day_close(DAY)
    assert report["bill_count"] == 2
    assert report["total_sales_paise"] == b1["total_paise"] + b2["total_paise"]
    assert report["sales_by_mode_paise"] == {"cash": b1["total_paise"], "upi": b2["total_paise"], "card": 0}
    assert report["average_bill_paise"] == (b1["total_paise"] + b2["total_paise"] + 1) // 2
    assert [(c["name"], c["reason"]) for c in report["cancelled_items"]] == [("Sweet Lassi", "spilled")]
    assert report["mismatches"] == []


def test_order_at_2330_paid_at_0015_counts_in_same_business_day(db, clock):
    clock.advance(hours=11, minutes=30)  # 23:30
    late = open_and_order(db, [("dal", 1)])
    serve_all(db, late["order_id"])

    # Report run before payment: the open order is a mismatch
    assert [m["order_id"] for m in reports.day_close(DAY)["mismatches"]] == [late["order_id"]]

    clock.advance(minutes=45)  # 00:15 on the next calendar day
    bill = _pay(db, late["order_id"], "card")

    report = reports.day_close(DAY)
    assert report["mismatches"] == []
    assert report["bill_count"] == 1
    assert report["sales_by_mode_paise"]["card"] == bill["total_paise"]
    assert reports.day_close(date(2026, 9, 26))["bill_count"] == 0


def test_mismatch_only_for_orders_created_in_that_business_day(db, clock):
    clock.advance(hours=15, minutes=59)  # 03:59 on 26 Sep -> still business day 25 Sep
    before = open_and_order(db, [("naan", 1)], table_index=0)
    clock.advance(minutes=1)  # 04:00 on 26 Sep -> business day 26 Sep
    after = open_and_order(db, [("dal", 1)], table_index=1)

    assert [m["order_id"] for m in reports.day_close(DAY)["mismatches"]] == [before["order_id"]]
    next_day = reports.day_close(date(2026, 9, 26))
    assert [m["order_id"] for m in next_day["mismatches"]] == [after["order_id"]]


def test_cancelled_order_is_not_a_mismatch(db, clock):
    kot = open_and_order(db, [("naan", 1)])
    orders.cancel_order(kot["order_id"], "guests left", db["staff"]["manager"])
    report = reports.day_close(DAY)
    assert report["mismatches"] == []
    assert [c["reason"] for c in report["cancelled_items"]] == ["guests left"]


def test_day_close_average_kitchen_time(db, clock):
    kot = open_and_order(db, [("naan", 1), ("dal", 1)])
    items = {i["station"]: i["item_id"] for i in orders.get_order(kot["order_id"])["items"]}
    kitchen.start_item(items["tandoor"], "tandoor")
    kitchen.start_item(items["kitchen"], "kitchen")
    clock.advance(minutes=6)
    kitchen.ready_item(items["tandoor"], "tandoor")
    clock.advance(minutes=9)
    kitchen.ready_item(items["kitchen"], "kitchen")

    report = reports.day_close(DAY)
    assert report["avg_kitchen_minutes"] == {"tandoor": 6.0, "kitchen": 15.0, "bar": None}


def test_day_close_empty_day(db):
    report = reports.day_close(date(2020, 1, 1))
    assert report["bill_count"] == 0
    assert report["average_bill_paise"] == 0
    assert report["mismatches"] == [] and report["cancelled_items"] == []
