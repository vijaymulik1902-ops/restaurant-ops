"""Sales summary: MANAGER ONLY (reads cost of goods). Follows CLAUDE.md "Sales report definitions":

- Only PAID bills count, by Bill.paid_at within the range (inclusive business days).
- Gross sales = sum of bill subtotals. Net sales = gross - discounts. GST is NOT revenue.
- Cost of goods = sum(qty * unit_cost_paise) of non-cancelled items on those paid bills.
- Gross profit = net sales - cost of goods (per dish: item revenue - item cost).
- Expenses = sum of Expense.amount_paise with spent_on in the range, by category.
- Net profit = net sales - expenses. Cost of goods is NOT subtracted as well (that double counts
  ingredients, which are already in expenses).
- Menu-wise revenue is at list price; bill discounts are one separate line.

Every figure comes from a fixed set of GROUP BY queries, whatever the number of bills.
"""
from datetime import date, timedelta

from sqlalchemy import Integer, and_, cast, func, select

from app.config import BUSINESS_DAY_START_HOUR
from app.db import now, read_session
from app.models import EXPENSE_CATEGORIES, PAYMENT_MODES, Bill, Expense, MenuItem, Order, OrderItem
from app.services import ServiceError, business_day_bounds, business_day_of, validate_range

TOP_N = 5
PRESETS = ("today", "yesterday", "this_week", "this_month", "last_month")


def _pct(part: int, whole: int) -> float | None:
    """part / whole as a percentage with 1 decimal; None when whole is 0 (no division by zero)."""
    return None if not whole else round(part * 100 / whole, 1)


def _div(total: int, count: int) -> int:
    """Integer average in paise, rounded half up; 0 when count is 0."""
    return (2 * total + count) // (2 * count) if count else 0


def preset_range(name: str, today: date | None = None) -> tuple[date, date]:
    """Date range for a preset, in business days. Weeks start on Monday."""
    today = today or business_day_of(now())
    if name == "today":
        return today, today
    if name == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if name == "this_week":
        return today - timedelta(days=today.weekday()), today
    if name == "this_month":
        return today.replace(day=1), today
    if name == "last_month":
        last_day = today.replace(day=1) - timedelta(days=1)
        return last_day.replace(day=1), last_day
    raise ServiceError("Unknown period")


def sales_summary(start: date, end: date) -> dict:
    """Totals, daily trend, menu-wise, payment modes and peak hours for business days start..end."""
    validate_range(start, end)
    lo, hi = business_day_bounds(start)[0], business_day_bounds(end)[1]
    paid = and_(Bill.paid_at.is_not(None), Bill.paid_at >= lo, Bill.paid_at < hi)
    # Business day of a payment: shift back by the start hour, then take the date (SQLite)
    bill_day = func.date(Bill.paid_at, f"-{BUSINESS_DAY_START_HOUR} hours").label("day")
    net_expr = Bill.subtotal_paise - Bill.discount_paise
    line_rev = OrderItem.qty * OrderItem.unit_price_paise
    line_cost = OrderItem.qty * OrderItem.unit_cost_paise
    zero = 0

    with read_session() as s:
        t = s.execute(
            select(func.count(Bill.id).label("bills"),
                   func.coalesce(func.sum(Bill.subtotal_paise), zero).label("gross"),
                   func.coalesce(func.sum(Bill.discount_paise), zero).label("discounts"),
                   func.coalesce(func.sum(Bill.gst_paise), zero).label("gst"),
                   func.coalesce(func.sum(Bill.total_paise), zero).label("collected"),
                   func.coalesce(func.sum(Order.guest_count), zero).label("guests"))
            .join(Order, Order.id == Bill.order_id).where(paid)
        ).one()
        by_mode = s.execute(
            select(Bill.payment_mode, func.count(Bill.id), func.sum(net_expr))
            .where(paid).group_by(Bill.payment_mode)
        ).all()
        daily_bills = s.execute(
            select(bill_day, func.count(Bill.id), func.sum(net_expr)).where(paid).group_by(bill_day)
        ).all()
        daily_cogs = s.execute(
            select(bill_day, func.sum(line_cost))
            .select_from(OrderItem).join(Bill, Bill.order_id == OrderItem.order_id)
            .where(paid, OrderItem.status != "cancelled").group_by(bill_day)
        ).all()
        sold = (select(OrderItem.menu_item_id.label("menu_item_id"),
                       func.sum(OrderItem.qty).label("qty"),
                       func.sum(line_rev).label("revenue"),
                       func.sum(line_cost).label("cost"))
                .join(Bill, Bill.order_id == OrderItem.order_id)
                .where(paid, OrderItem.status != "cancelled")
                .group_by(OrderItem.menu_item_id)).subquery()
        # Every current dish (unsold ones too: they are the real "bottom sellers"), plus
        # archived dishes that still sold something in the period
        dishes = s.execute(
            select(MenuItem.id, MenuItem.name, MenuItem.category, MenuItem.archived,
                   func.coalesce(sold.c.qty, zero), func.coalesce(sold.c.revenue, zero),
                   func.coalesce(sold.c.cost, zero))
            .outerjoin(sold, sold.c.menu_item_id == MenuItem.id)
            .where((MenuItem.archived.is_(False)) | (sold.c.qty.is_not(None)))
            .order_by(MenuItem.category, MenuItem.name)
        ).all()
        expenses_by_cat = dict(s.execute(
            select(Expense.category, func.sum(Expense.amount_paise))
            .where(Expense.spent_on >= start, Expense.spent_on <= end).group_by(Expense.category)
        ).all())
        daily_exp = dict(s.execute(
            select(Expense.spent_on, func.sum(Expense.amount_paise))
            .where(Expense.spent_on >= start, Expense.spent_on <= end).group_by(Expense.spent_on)
        ).all())
        # Peak hours use the hour guests were SEATED (order created), not when they paid
        hour = cast(func.strftime("%H", Order.created_at), Integer).label("hour")
        by_hour = s.execute(
            select(hour, func.count(Bill.id), func.sum(net_expr))
            .select_from(Bill).join(Order, Order.id == Bill.order_id).where(paid).group_by(hour)
        ).all()

    # ----- totals
    net = t.gross - t.discounts
    cogs = sum(d[6] for d in dishes)
    gross_profit = net - cogs
    expenses_total = sum(expenses_by_cat.values())
    days = (end - start).days + 1
    totals = {
        "gross_sales_paise": t.gross, "discounts_paise": t.discounts, "net_sales_paise": net,
        "gst_paise": t.gst, "collected_paise": t.collected,
        "cost_of_goods_paise": cogs, "gross_profit_paise": gross_profit,
        "gross_margin_percent": _pct(gross_profit, net),
        "expenses_by_category_paise": {c: expenses_by_cat.get(c, 0) for c in EXPENSE_CATEGORIES},
        "expenses_paise": expenses_total,
        "net_profit_paise": net - expenses_total,  # NOT minus cost of goods as well
        "bill_count": t.bills, "average_bill_paise": _div(net, t.bills),
        "days": days, "average_daily_net_sales_paise": _div(net, days),
        "guests": t.guests, "average_spend_per_guest_paise": _div(net, t.guests),
    }

    # ----- daily trend (every business day in range, zeros included)
    bills_by_day = {d: (n, v) for d, n, v in daily_bills}
    cogs_by_day = dict(daily_cogs)
    daily = []
    for i in range(days):
        d = start + timedelta(days=i)
        key = d.isoformat()
        n, v = bills_by_day.get(key, (0, 0))
        daily.append({"day": d, "bills": n, "net_sales_paise": v,
                      "gross_profit_paise": v - (cogs_by_day.get(key) or 0),
                      "expenses_paise": daily_exp.get(d, 0)})

    # ----- menu-wise, at list price
    menu_rows = []
    for dish_id, name, category, archived, qty, revenue, cost in dishes:
        profit = revenue - cost
        menu_rows.append({"menu_item_id": dish_id, "name": name, "category": category, "archived": archived,
                          "qty": qty, "revenue_paise": revenue, "cost_paise": cost, "profit_paise": profit,
                          "margin_percent": _pct(profit, revenue), "share_percent": _pct(revenue, t.gross),
                          "top_profit": False, "low_qty": False})
    for r in sorted((r for r in menu_rows if r["qty"]), key=lambda r: (-r["profit_paise"], r["name"]))[:TOP_N]:
        r["top_profit"] = True
    for r in sorted((r for r in menu_rows if not r["archived"]), key=lambda r: (r["qty"], r["name"]))[:TOP_N]:
        r["low_qty"] = True
    categories: dict[str, dict] = {}
    for r in menu_rows:
        c = categories.setdefault(r["category"], {"category": r["category"], "qty": 0, "revenue_paise": 0,
                                                  "cost_paise": 0, "profit_paise": 0})
        for k in ("qty", "revenue_paise", "cost_paise", "profit_paise"):
            c[k] += r[k]
    for c in categories.values():
        c["margin_percent"] = _pct(c["profit_paise"], c["revenue_paise"])
        c["share_percent"] = _pct(c["revenue_paise"], t.gross)

    # ----- payment modes and peak hours
    modes = {m: {"bills": 0, "net_sales_paise": 0, "share_percent": None} for m in PAYMENT_MODES}
    for mode, n, v in by_mode:
        modes[mode] = {"bills": n, "net_sales_paise": v, "share_percent": _pct(v, net)}
    hours = {h: {"hour": h, "bills": 0, "net_sales_paise": 0} for h in range(24)}
    for h, n, v in by_hour:
        hours[int(h)] = {"hour": int(h), "bills": n, "net_sales_paise": v}

    return {
        "start": start, "end": end, "totals": totals, "daily": daily,
        "menu": menu_rows, "categories": list(categories.values()),
        "menu_revenue_paise": sum(r["revenue_paise"] for r in menu_rows),  # == gross sales
        "payment_modes": modes, "hours": list(hours.values()),
    }


def resolve_range(preset: str | None, start: date | None, end: date | None) -> tuple[date, date, str]:
    """(start, end, preset) from the page's query: explicit dates mean "custom",
    otherwise a preset (default: today)."""
    if start or end:
        start = start or end
        end = end or start
        validate_range(start, end)
        return start, end, "custom"
    preset = preset or "today"
    s, e = preset_range(preset)
    return s, e, preset


def _rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    return f"{sign}{abs(paise) // 100}.{abs(paise) % 100:02d}"


def menu_csv_rows(summary: dict) -> list[list[str]]:
    """Menu-wise export: one row per dish, category subtotals, then the discount line and totals.
    Amounts are plain rupees with 2 decimals (spreadsheet friendly)."""
    t = summary["totals"]
    rows = [["Menu-wise sales", f"{summary['start'].isoformat()} to {summary['end'].isoformat()}"],
            ["Category", "Dish", "Qty sold", "Revenue (list price)", "Cost", "Gross profit",
             "Margin %", "Share of sales %"]]
    pct = lambda v: "" if v is None else f"{v:.1f}"  # noqa: E731
    for r in summary["menu"]:
        rows.append([r["category"], r["name"], str(r["qty"]), _rupees(r["revenue_paise"]), _rupees(r["cost_paise"]),
                     _rupees(r["profit_paise"]), pct(r["margin_percent"]), pct(r["share_percent"])])
    rows.append([])
    rows.append(["Category subtotals"])
    for c in summary["categories"]:
        rows.append([c["category"], "", str(c["qty"]), _rupees(c["revenue_paise"]), _rupees(c["cost_paise"]),
                     _rupees(c["profit_paise"]), pct(c["margin_percent"]), pct(c["share_percent"])])
    rows.append([])
    rows += [
        ["Menu revenue (gross sales)", "", "", _rupees(summary["menu_revenue_paise"])],
        ["Bill discounts", "", "", _rupees(-t["discounts_paise"])],
        ["Net sales", "", "", _rupees(t["net_sales_paise"])],
        ["Cost of goods", "", "", _rupees(t["cost_of_goods_paise"])],
        ["Gross profit", "", "", _rupees(t["gross_profit_paise"])],
        ["GST collected (not revenue)", "", "", _rupees(t["gst_paise"])],
    ]
    return rows
