import re
from pathlib import Path

import pytest

from app.services import ServiceError, billing, kitchen, orders, tables
from conftest import open_and_order, serve_all

SERVICES = Path(__file__).resolve().parent.parent / "app" / "services"


def _billable(db, lines, table_index=0):
    kot = open_and_order(db, lines, table_index)
    serve_all(db, kot["order_id"])
    return kot["order_id"]


def _table(number):
    return next(t for t in tables.list_tables() if t["number"] == number)


@pytest.mark.parametrize("taxable, expected", [(0, 0), (1000, 50), (10, 1), (9, 0), (30, 2), (29, 1)])
def test_gst_rounds_half_up(taxable, expected):
    # 5% of 10 = 0.5 -> 1 ; 5% of 30 = 1.5 -> 2 ; 5% of 29 = 1.45 -> 1
    assert billing.gst_for(taxable, 5) == expected


def test_bill_math_with_discount(db):
    order_id = _billable(db, [("naan", 3), ("dal", 1)])  # 13500 + 18000 = 31500
    bill, _ = billing.generate_bill(order_id, 3150, db["staff"]["counter"])  # exactly 10%
    assert bill["subtotal_paise"] == 31500
    assert bill["discount_paise"] == 3150
    assert bill["gst_percent"] == 5
    assert bill["gst_paise"] == 1418  # 28350 * 5% = 1417.5 -> 1418
    assert bill["total_paise"] == 31500 - 3150 + 1418
    assert bill["bill_no"] == 1


def test_cancelled_items_not_billed(db):
    kot = open_and_order(db, [("naan", 1), ("dal", 1)])
    dal = next(i for i in orders.get_order(kot["order_id"])["items"] if i["name"] == "Dal Tadka")
    kitchen.cancel_item(dal["item_id"], "out of dal", db["staff"]["waiter"])
    serve_all(db, kot["order_id"])
    bill, _ = billing.generate_bill(kot["order_id"], 0, db["staff"]["counter"])
    assert bill["subtotal_paise"] == 4500


def test_discount_over_ten_percent_needs_manager(db):
    order_id = _billable(db, [("dal", 1)])  # 18000
    with pytest.raises(ServiceError, match="manager"):
        billing.generate_bill(order_id, 1801, db["staff"]["counter"])
    bill, _ = billing.generate_bill(order_id, 1801, db["staff"]["manager"])
    assert bill["discount_paise"] == 1801


def test_discount_cannot_be_negative_or_exceed_subtotal(db):
    order_id = _billable(db, [("naan", 1)])
    with pytest.raises(ServiceError):
        billing.generate_bill(order_id, -1, db["staff"]["manager"])
    with pytest.raises(ServiceError):
        billing.generate_bill(order_id, 4501, db["staff"]["manager"])


def test_bill_blocked_while_items_in_kitchen(db):
    kot = open_and_order(db, [("naan", 1)])
    with pytest.raises(ServiceError, match="kitchen"):
        billing.generate_bill(kot["order_id"], 0, db["staff"]["counter"])
    item_id = orders.get_order(kot["order_id"])["items"][0]["item_id"]
    kitchen.start_item(item_id, "tandoor")
    with pytest.raises(ServiceError, match="kitchen"):
        billing.generate_bill(kot["order_id"], 0, db["staff"]["counter"])
    assert orders.get_order(kot["order_id"])["status"] == "open"


def test_billed_order_rejects_new_kot_and_rebill(db):
    order_id = _billable(db, [("naan", 1)])
    billing.generate_bill(order_id, 0, db["staff"]["counter"])
    assert _table(1)["status"] == "billing"
    from conftest import new_kot_id

    with pytest.raises(ServiceError, match="billed"):
        orders.send_kot(order_id, new_kot_id(), db["staff"]["waiter"], [(db["menu"]["naan"], 1, None)])
    with pytest.raises(ServiceError):
        billing.generate_bill(order_id, 0, db["staff"]["counter"])


def test_bill_numbers_sequential(db):
    a = _billable(db, [("naan", 1)], table_index=0)
    b = _billable(db, [("dal", 1)], table_index=1)
    assert billing.generate_bill(a, 0, db["staff"]["counter"])[0]["bill_no"] == 1
    assert billing.generate_bill(b, 0, db["staff"]["counter"])[0]["bill_no"] == 2


def test_table_freed_after_payment(db, clock):
    order_id = _billable(db, [("naan", 1)])
    bill, _ = billing.generate_bill(order_id, 0, db["staff"]["counter"])
    clock.advance(minutes=4)
    paid, events = billing.pay_bill(bill["bill_id"], "upi")
    assert paid["paid_at"] == clock.current and paid["payment_mode"] == "upi"
    row = _table(1)
    assert row["status"] == "available"
    assert row["status_since"] == clock.current
    assert row["order_id"] is None
    assert orders.get_order(order_id)["status"] == "paid"
    assert {"section:A", "counter"} <= {e.channel for e in events}
    # Table can be seated again, and the bill can't be paid twice
    tables.open_table(db["tables"][0], db["staff"]["waiter"], 2)
    with pytest.raises(ServiceError, match="already paid"):
        billing.pay_bill(bill["bill_id"], "cash")


def test_pay_rejects_unknown_mode(db):
    order_id = _billable(db, [("naan", 1)])
    bill, _ = billing.generate_bill(order_id, 0, db["staff"]["counter"])
    with pytest.raises(ServiceError):
        billing.pay_bill(bill["bill_id"], "cheque")
    assert _table(1)["status"] == "billing"


@pytest.mark.parametrize("module", ["billing.py", "reports.py"])
def test_billing_and_reports_never_touch_cost_columns(module):
    source = (SERVICES / module).read_text()
    assert not re.search(r"cost_paise|unit_cost", source)


def test_all_cancelled_order_cannot_be_billed_and_uses_no_bill_number(db):
    kot = open_and_order(db, [("naan", 1)], table_index=0)
    item_id = orders.get_order(kot["order_id"])["items"][0]["item_id"]
    kitchen.cancel_item(item_id, "guest changed mind", db["staff"]["waiter"])
    with pytest.raises(ServiceError, match="Nothing to bill - cancel the order instead"):
        billing.generate_bill(kot["order_id"], 0, db["staff"]["counter"])
    assert orders.get_order(kot["order_id"])["status"] == "open"
    assert _table(1)["status"] == "occupied"

    other = _billable(db, [("dal", 1)], table_index=1)
    assert billing.generate_bill(other, 0, db["staff"]["counter"])[0]["bill_no"] == 1


def test_order_without_items_cannot_be_billed(db):
    opened, _ = tables.open_table(db["tables"][0], db["staff"]["waiter"], 2)
    with pytest.raises(ServiceError, match="Nothing to bill"):
        billing.generate_bill(opened["order_id"], 0, db["staff"]["counter"])


def test_bill_from_stale_preview_rejected(db):
    order_id = _billable(db, [("naan", 1)])
    seen_version = orders.get_order(order_id)["version"]
    from conftest import new_kot_id

    kot, _ = orders.send_kot(order_id, new_kot_id(), db["staff"]["waiter"], [(db["menu"]["lassi"], 1, None)])
    serve_all(db, order_id)
    with pytest.raises(ServiceError, match="Order changed, reload"):
        billing.generate_bill(order_id, 0, db["staff"]["counter"], expected_version=seen_version)
    bill, _ = billing.generate_bill(order_id, 0, db["staff"]["counter"],
                                    expected_version=orders.get_order(order_id)["version"])
    assert bill["subtotal_paise"] == 4500 + 7000


def test_only_counter_or_manager_can_bill(db):
    order_id = _billable(db, [("naan", 1)])
    with pytest.raises(ServiceError, match="counter or a manager"):
        billing.generate_bill(order_id, 0, db["staff"]["waiter"])
