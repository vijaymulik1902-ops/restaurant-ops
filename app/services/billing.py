"""Billing: generating and settling bills.

This module must never read a cost field. It selects only the columns it needs.
"""
from sqlalchemy import func, select

from app.config import GST_PERCENT
from app.db import now, read_session, write_session
from app.models import PAYMENT_MODES, Bill, DiningTable, Order, OrderItem, Staff
from app.services import Event, ServiceError, audit
from app.services.orders import check_version
from app.services.tables import get_active_staff, table_events

MANAGER_FREE_DISCOUNT_PERCENT = 10
BILLING_ROLES = ("counter", "manager")


def gst_for(taxable_paise: int, percent: int = GST_PERCENT) -> int:
    """GST on a taxable amount, rounded half up to the nearest paisa."""
    return (taxable_paise * percent + 50) // 100


def _bill_result(bill: Bill, order: Order, table: DiningTable) -> dict:
    return {
        "bill_id": bill.id, "bill_no": bill.bill_no, "order_id": order.id,
        "table_number": table.number, "subtotal_paise": bill.subtotal_paise,
        "discount_paise": bill.discount_paise, "gst_percent": bill.gst_percent,
        "gst_paise": bill.gst_paise, "total_paise": bill.total_paise,
        "payment_mode": bill.payment_mode, "paid_at": bill.paid_at,
    }


def _bill_events(result: dict, table: DiningTable, order: Order) -> list[Event]:
    counter_data = {k: result[k] for k in ("bill_id", "bill_no", "order_id", "table_number",
                                           "total_paise", "payment_mode")}
    return [*table_events(table, order.id if order.status != "paid" else None),
            Event("counter", "bill", counter_data)]


def generate_bill(order_id: int, discount_paise: int, by_staff_id: int,
                  expected_version: int | None = None) -> tuple[dict, list[Event]]:
    """Bill an open order: order -> billed, table -> billing.

    No item may still be pending or preparing, and at least one item must be
    billable (otherwise the order should be cancelled). Subtotal counts non-cancelled items.
    A discount above 10% of the subtotal needs a manager. GST is charged on
    (subtotal - discount), rounded half up. Bill numbers are sequential with no gaps.
    `expected_version` is the version the bill preview showed: if items were added
    since, the bill is refused so the counter never charges for items it didn't see.
    """
    if not isinstance(discount_paise, int) or isinstance(discount_paise, bool) or discount_paise < 0:
        raise ServiceError("Discount must be zero or more")

    with write_session() as s:
        order = s.get(Order, order_id)
        if order is None:
            raise ServiceError("Order not found")
        if order.status != "open":
            raise ServiceError(f"Order is {order.status}, cannot generate a bill")
        check_version(order, expected_version)
        staff = get_active_staff(s, by_staff_id)
        if staff.role not in BILLING_ROLES:
            raise ServiceError("Only the counter or a manager can generate bills")

        lines = s.execute(
            select(OrderItem.qty, OrderItem.unit_price_paise, OrderItem.status)
            .where(OrderItem.order_id == order.id)
        ).all()
        in_kitchen = sum(1 for ln in lines if ln.status in ("pending", "preparing"))
        if in_kitchen:
            raise ServiceError(f"{in_kitchen} item(s) still in the kitchen; serve or cancel them first")
        billable = [ln for ln in lines if ln.status != "cancelled"]
        if not billable:
            # Never spend a bill number on a zero-value bill
            raise ServiceError("Nothing to bill - cancel the order instead")
        subtotal = sum(ln.qty * ln.unit_price_paise for ln in billable)

        if discount_paise > subtotal:
            raise ServiceError("Discount cannot exceed the subtotal")
        if discount_paise * 100 > subtotal * MANAGER_FREE_DISCOUNT_PERCENT and staff.role != "manager":
            raise ServiceError(
                f"Discount above {MANAGER_FREE_DISCOUNT_PERCENT}% needs a manager"
            )

        gst = gst_for(subtotal - discount_paise)
        ts = now()
        last_no = s.scalar(select(func.max(Bill.bill_no)))
        bill = Bill(
            bill_no=(last_no or 0) + 1, order_id=order.id, subtotal_paise=subtotal,
            discount_paise=discount_paise, gst_percent=GST_PERCENT, gst_paise=gst,
            total_paise=subtotal - discount_paise + gst, created_by=staff.id, created_at=ts,
        )
        s.add(bill)
        order.status = "billed"
        table = s.get(DiningTable, order.table_id)
        table.status = "billing"
        table.status_since = ts
        s.flush()

        audit.record(s, staff.id, audit.BILL_GENERATED, "bill", bill.id, new={
            "bill_no": bill.bill_no, "order_id": order.id, "table_number": table.number,
            "subtotal_paise": subtotal, "discount_paise": discount_paise, "gst_paise": gst,
            "total_paise": bill.total_paise,
        })
        if discount_paise:
            audit.record(s, staff.id, audit.BILL_DISCOUNT, "bill", bill.id, new={
                "bill_no": bill.bill_no, "discount_paise": discount_paise, "subtotal_paise": subtotal,
                "percent": round(discount_paise * 100 / subtotal, 2),
            })

        result = _bill_result(bill, order, table)
        events = _bill_events(result, table, order)
    return result, events


def pay_bill(bill_id: int, payment_mode: str, by_staff_id: int) -> tuple[dict, list[Event]]:
    """Settle a bill: order -> paid (closed_at set), table -> available. Audited."""
    if payment_mode not in PAYMENT_MODES:
        raise ServiceError("Choose a payment mode: " + ", ".join(PAYMENT_MODES))

    with write_session() as s:
        staff = get_active_staff(s, by_staff_id)
        if staff.role not in BILLING_ROLES:
            raise ServiceError("Only the counter or a manager can take payment")
        bill = s.get(Bill, bill_id)
        if bill is None:
            raise ServiceError("Bill not found")
        if bill.paid_at is not None:
            raise ServiceError(f"Bill {bill.bill_no} is already paid")
        order = s.get(Order, bill.order_id)
        if order.status != "billed":
            raise ServiceError(f"Order is {order.status}, cannot take payment")

        ts = now()
        bill.paid_at = ts
        bill.payment_mode = payment_mode
        order.status = "paid"
        order.closed_at = ts
        table = s.get(DiningTable, order.table_id)
        table.status = "available"
        table.status_since = ts
        s.flush()

        audit.record(s, staff.id, audit.BILL_PAID, "bill", bill.id,
                     old={"paid": False},
                     new={"paid": True, "bill_no": bill.bill_no, "payment_mode": payment_mode,
                          "total_paise": bill.total_paise})
        result = _bill_result(bill, order, table)
        events = _bill_events(result, table, order)
    return result, events


def get_bill(bill_id: int) -> dict:
    """A bill with its billed (non-cancelled) lines, for the counter and the print view."""
    with read_session() as s:
        header = s.execute(
            select(Bill, DiningTable.number.label("table_number"), Staff.name.label("waiter_name"),
                   Order.guest_count)
            .join(Order, Order.id == Bill.order_id)
            .join(DiningTable, DiningTable.id == Order.table_id)
            .join(Staff, Staff.id == Order.waiter_id)
            .where(Bill.id == bill_id)
        ).one_or_none()
        if header is None:
            raise ServiceError("Bill not found")
        bill = header.Bill  # ORM object: read it before the session closes
        bill_data = {
            "bill_id": bill.id, "bill_no": bill.bill_no, "order_id": bill.order_id,
            "subtotal_paise": bill.subtotal_paise, "discount_paise": bill.discount_paise,
            "gst_percent": bill.gst_percent, "gst_paise": bill.gst_paise,
            "total_paise": bill.total_paise, "payment_mode": bill.payment_mode,
            "created_at": bill.created_at, "paid_at": bill.paid_at,
        }
        lines = s.execute(
            select(OrderItem.name, OrderItem.qty, OrderItem.unit_price_paise)
            .where(OrderItem.order_id == bill.order_id, OrderItem.status != "cancelled")
            .order_by(OrderItem.id)
        ).all()

    return {
        **bill_data,
        "table_number": header.table_number, "waiter_name": header.waiter_name,
        "guest_count": header.guest_count,
        "lines": [
            {"name": ln.name, "qty": ln.qty, "unit_price_paise": ln.unit_price_paise,
             "line_total_paise": ln.qty * ln.unit_price_paise}
            for ln in lines
        ],
    }
