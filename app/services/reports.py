"""Reports visible to the counter. Never reads cost or profit fields."""
from datetime import date

from sqlalchemy import func, select

from app.db import read_session
from app.models import PAYMENT_MODES, STATIONS, Bill, DiningTable, Order, OrderItem
from app.db import now
from app.services import ServiceError, business_day_bounds, business_day_of

MIN_YEAR, MAX_YEAR = 2000, 2100


def _avg_half_up(total: int, count: int) -> int:
    """Integer average rounded half up (0 when count is 0)."""
    return (2 * total + count) // (2 * count) if count else 0


def day_close(day: date | None = None) -> dict:
    """End-of-day summary for one business day (default: the current one).

    - Sales, bill count and average bill are based on bills PAID during the business day.
    - Cancelled items and kitchen times are based on items ORDERED during the business day.
    - Mismatch: a non-cancelled item on an order created during the business day
      that is still not paid at the moment this report runs.
    """
    day = day or business_day_of(now())
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise ServiceError(f"Pick a date between {MIN_YEAR} and {MAX_YEAR}")
    start, end = business_day_bounds(day)
    paid_today = (Bill.paid_at >= start, Bill.paid_at < end)
    ordered_today = (OrderItem.created_at >= start, OrderItem.created_at < end)

    with read_session() as s:
        by_mode_rows = s.execute(
            select(Bill.payment_mode, func.count(Bill.id), func.coalesce(func.sum(Bill.total_paise), 0))
            .where(*paid_today)
            .group_by(Bill.payment_mode)
        ).all()

        cancelled_rows = s.execute(
            select(OrderItem.id, OrderItem.name, OrderItem.qty, OrderItem.unit_price_paise,
                   OrderItem.station, OrderItem.cancel_reason, OrderItem.created_at,
                   DiningTable.number.label("table_number"))
            .join(Order, Order.id == OrderItem.order_id)
            .join(DiningTable, DiningTable.id == Order.table_id)
            .where(OrderItem.status == "cancelled", *ordered_today)
            .order_by(OrderItem.created_at, OrderItem.id)
        ).all()

        timing_rows = s.execute(
            select(OrderItem.station, OrderItem.created_at, OrderItem.ready_at)
            .where(OrderItem.ready_at.is_not(None), *ordered_today)
        ).all()

        mismatch_rows = s.execute(
            select(OrderItem.id, OrderItem.order_id, OrderItem.name, OrderItem.qty,
                   OrderItem.status, OrderItem.created_at, Order.status.label("order_status"),
                   DiningTable.number.label("table_number"))
            .join(Order, Order.id == OrderItem.order_id)
            .join(DiningTable, DiningTable.id == Order.table_id)
            .where(
                OrderItem.status != "cancelled",
                Order.created_at >= start,
                Order.created_at < end,
                Order.status != "paid",
            )
            .order_by(OrderItem.order_id, OrderItem.id)
        ).all()

    sales_by_mode = {mode: 0 for mode in PAYMENT_MODES}
    bill_count = 0
    for mode, count, total in by_mode_rows:
        sales_by_mode[mode] = total
        bill_count += count
    total_sales = sum(sales_by_mode.values())

    durations: dict[str, list[float]] = {st: [] for st in STATIONS}
    for station, created_at, ready_at in timing_rows:
        durations[station].append((ready_at - created_at).total_seconds() / 60)
    avg_kitchen_minutes = {
        st: round(sum(vals) / len(vals), 1) if vals else None for st, vals in durations.items()
    }

    return {
        "date": day,
        "total_sales_paise": total_sales,
        "sales_by_mode_paise": sales_by_mode,
        "bill_count": bill_count,
        "average_bill_paise": _avg_half_up(total_sales, bill_count),
        "cancelled_items": [
            {"item_id": r.id, "name": r.name, "qty": r.qty, "station": r.station,
             "value_paise": r.qty * r.unit_price_paise, "reason": r.cancel_reason,
             "table_number": r.table_number, "created_at": r.created_at}
            for r in cancelled_rows
        ],
        "avg_kitchen_minutes": avg_kitchen_minutes,
        "mismatches": [
            {"item_id": r.id, "order_id": r.order_id, "name": r.name, "qty": r.qty,
             "item_status": r.status, "order_status": r.order_status,
             "table_number": r.table_number,
             "created_at": r.created_at}
            for r in mismatch_rows
        ],
    }


# The sales summary reads cost of goods, so it lives in the manager-only app.services.sales
# (this module stays cost-free for the counter). Re-exported so reports.sales_summary works.
from app.services.sales import sales_summary  # noqa: E402,F401
