"""Counter: all tables, billing, payment, printable bill, day close."""
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request

from app.auth import CurrentStaff, require_role
from app.db import now
from app.models import PAYMENT_MODES
from app.services import ServiceError, billing, business_day_of, orders, reports, tables
from app.web import COUNTER_ROLES, publish, render, see_other

router = APIRouter()
counter_staff = require_role(*COUNTER_ROLES)


@router.get("/counter")
def counter_page(request: Request, staff: CurrentStaff = Depends(counter_staff)):
    return render(request, "counter.html", staff, tables=tables.list_tables(), view="counter",
                  stream_url="/stream")


@router.get("/counter/orders/{order_id}")
def bill_preview(request: Request, order_id: int, staff: CurrentStaff = Depends(counter_staff)):
    order = orders.get_order(order_id)
    if order["bill_id"]:
        return see_other(f"/counter/bills/{order['bill_id']}")
    return render(request, "bill_preview.html", staff, order=order, stream_url="/stream")


def _rupees_to_paise(text: str) -> int:
    """'50' / '49.5' / '' -> paise. Parsing only; the service validates the amount."""
    text = (text or "").strip().replace(",", "").lstrip("₹")
    if not text:
        return 0
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ServiceError("Enter the discount in rupees, e.g. 50 or 49.50")
    if not value.is_finite():
        raise ServiceError("Enter the discount in rupees, e.g. 50 or 49.50")
    if value != value.quantize(Decimal("0.01")):
        raise ServiceError("Discount can have at most 2 decimal places")
    return int(value * 100)


@router.post("/counter/orders/{order_id}/bill")
def generate_bill(order_id: int, discount: str = Form(""), staff: CurrentStaff = Depends(counter_staff)):
    result, events = billing.generate_bill(order_id, _rupees_to_paise(discount), staff.id)
    publish(events)
    return see_other(f"/counter/bills/{result['bill_id']}")


@router.get("/counter/bills/{bill_id}")
def bill_page(request: Request, bill_id: int, staff: CurrentStaff = Depends(counter_staff)):
    return render(request, "bill.html", staff, bill=billing.get_bill(bill_id), modes=PAYMENT_MODES)


@router.post("/counter/bills/{bill_id}/pay")
def pay(bill_id: int, payment_mode: str = Form(""), staff: CurrentStaff = Depends(counter_staff)):
    _, events = billing.pay_bill(bill_id, payment_mode)
    publish(events)
    return see_other(f"/counter/bills/{bill_id}")


@router.get("/counter/bills/{bill_id}/print")
def bill_print(request: Request, bill_id: int, staff: CurrentStaff = Depends(counter_staff)):
    return render(request, "bill_print.html", staff, bill=billing.get_bill(bill_id))


@router.get("/reports/day-close")
def day_close(request: Request, day: date | None = None, staff: CurrentStaff = Depends(counter_staff)):
    day = day or business_day_of(now())
    return render(request, "day_close.html", staff, report=reports.day_close(day))
