"""The ONLY things the AI chat can do: six read-only functions over existing tested services.

The model never writes SQL and never sees raw rows. Every function returns aggregated figures
in rupees. Never sent: PINs, staff names or other personal details, order notes, or free-text
cancel reasons (bucketed to the standard reasons), since free text could carry injected
instructions. Dish names are fine.
"""
from datetime import date

from app.services import ServiceError, validate_range
from app.services.insights import STANDARD_REASONS, operations
from app.services.sales import OPERATING_CATEGORIES, sales_summary


class ToolArgError(Exception):
    """A tool was called with bad arguments; the message goes back to the model."""


def _rupees(paise: int | None) -> float | None:
    return None if paise is None else round(paise / 100, 2)


def _dates(args: dict) -> tuple[date, date]:
    """start/end as YYYY-MM-DD strings, end >= start, at most 366 days."""
    if not isinstance(args, dict) or set(args) - {"start", "end"}:
        raise ToolArgError("Arguments must be exactly: start, end (YYYY-MM-DD)")
    try:
        start, end = date.fromisoformat(str(args["start"])), date.fromisoformat(str(args["end"]))
    except (KeyError, ValueError):
        raise ToolArgError("start and end must be dates in YYYY-MM-DD format")
    try:
        validate_range(start, end)
    except ServiceError as e:
        raise ToolArgError(e.message)
    return start, end


def _range(start: date, end: date) -> dict:
    return {"start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days + 1}


def sales_summary_tool(start: date, end: date) -> dict:
    t = sales_summary(start, end)["totals"]
    return {"range": _range(start, end),
            "gross_sales_rupees": _rupees(t["gross_sales_paise"]), "discounts_rupees": _rupees(t["discounts_paise"]),
            "net_sales_rupees": _rupees(t["net_sales_paise"]), "gst_collected_rupees": _rupees(t["gst_paise"]),
            "cost_of_goods_rupees": _rupees(t["cost_of_goods_paise"]),
            "gross_profit_rupees": _rupees(t["gross_profit_paise"]), "gross_margin_percent": t["gross_margin_percent"],
            "operating_expenses_rupees": _rupees(t["operating_expenses_paise"]),
            "net_profit_rupees": _rupees(t["net_profit_paise"]), "net_margin_percent": t["net_margin_percent"],
            "bills": t["bill_count"], "average_bill_net_rupees": _rupees(t["average_bill_paise"]),
            "average_net_sales_per_day_rupees": _rupees(t["average_daily_net_sales_paise"]),
            "guests": t["guests"], "average_spend_per_guest_rupees": _rupees(t["average_spend_per_guest_paise"])}


def menu_performance_tool(start: date, end: date) -> dict:
    report = sales_summary(start, end)
    return {"range": _range(start, end),
            "dishes": [{"dish": m["name"], "category": m["category"], "qty_sold": m["qty"],
                        "revenue_rupees": _rupees(m["revenue_paise"]), "cost_rupees": _rupees(m["cost_paise"]),
                        "gross_profit_rupees": _rupees(m["profit_paise"]), "margin_percent": m["margin_percent"],
                        "share_of_sales_percent": m["share_percent"]} for m in report["menu"]]}


def peak_hours_tool(start: date, end: date) -> dict:
    hours = sales_summary(start, end)["hours"]
    return {"range": _range(start, end), "by": "hour guests were seated",
            "hours": [{"hour": h["hour"], "bills": h["bills"], "net_sales_rupees": _rupees(h["net_sales_paise"])}
                      for h in hours if h["bills"]]}


def expenses_by_category_tool(start: date, end: date) -> dict:
    t = sales_summary(start, end)["totals"]
    return {"range": _range(start, end),
            "operating_expenses_rupees": {c: _rupees(t["operating_expenses_by_category_paise"][c])
                                          for c in OPERATING_CATEGORIES},
            "operating_total_rupees": _rupees(t["operating_expenses_paise"]),
            "ingredient_purchases_rupees": _rupees(t["ingredient_purchases_paise"]),
            "note": "Ingredient purchases are not deducted in profit; food cost is counted via cost of goods."}


def cancellations_tool(start: date, end: date) -> dict:
    ops = operations(start, end)
    buckets: dict[str, int] = {}
    for reason, qty in ops["cancel_reasons"].items():  # free text never leaves the server
        key = reason if reason in STANDARD_REASONS else "Other (typed reason)"
        buckets[key] = buckets.get(key, 0) + qty
    rate = round(ops["items_cancelled"] * 100 / ops["items_ordered"], 1) if ops["items_ordered"] else None
    return {"range": _range(start, end), "items_ordered": ops["items_ordered"],
            "items_cancelled": ops["items_cancelled"], "cancel_rate_percent": rate, "by_reason": buckets}


def kitchen_times_tool(start: date, end: date) -> dict:
    return {"range": _range(start, end), "measure": "average minutes from order sent to ready",
            "minutes_by_station": operations(start, end)["kitchen_minutes"]}


_DATE_PARAMS = {"type": "object",
                "properties": {"start": {"type": "string", "description": "First business day, YYYY-MM-DD"},
                               "end": {"type": "string", "description": "Last business day, YYYY-MM-DD (inclusive)"}},
                "required": ["start", "end"]}

TOOLS = {
    "sales_summary": (sales_summary_tool, "Totals for paid bills: gross/net sales, discounts, GST, cost of goods, "
                                          "gross profit, operating expenses, net profit, bills, guests, averages."),
    "menu_performance": (menu_performance_tool, "Per dish: quantity sold, revenue, cost, gross profit, margin %, "
                                                "share of sales."),
    "peak_hours": (peak_hours_tool, "Bills and net sales per hour of day (by the hour guests were seated)."),
    "expenses_by_category": (expenses_by_category_tool, "Operating expenses by category, plus ingredient purchases."),
    "cancellations": (cancellations_tool, "Items ordered vs cancelled, cancel rate and counts by standard reason."),
    "kitchen_times": (kitchen_times_tool, "Average minutes from order to ready, per kitchen station."),
}


def declarations() -> list[dict]:
    """Gemini functionDeclarations for the six tools."""
    return [{"name": name, "description": desc + " Dates are restaurant business days (04:00 to 04:00).",
             "parameters": _DATE_PARAMS} for name, (_, desc) in TOOLS.items()]


def run_tool(name: str, args: dict) -> dict:
    """Validate and run one tool. Unknown names and bad arguments raise ToolArgError."""
    if name not in TOOLS:
        raise ToolArgError(f"Unknown function {name!r}")
    start, end = _dates(args)
    return TOOLS[name][0](start, end)
