"""Insight cards: MANAGER ONLY (uses cost-based profit). Plain rules on existing figures, no AI.

Every card is one sentence plus the number it rests on, computed from sales.sales_summary()
and one grouped query for cancellations and kitchen times. Fixed query count.
"""
from datetime import date, timedelta

from sqlalchemy import func, select

from app.db import read_session
from app.models import STATIONS, OrderItem
from app.services import business_day_bounds, validate_range
from app.services.sales import sales_summary
from app.text import plural, whole_rupees

TOP_N = 3
STANDARD_REASONS = ("Customer changed mind", "Wrong item entered", "Out of stock")


def operations(start: date, end: date) -> dict:
    """Items ordered in the business-day range: cancellation counts by reason and average
    kitchen minutes (created -> ready) per station. Aggregates only."""
    validate_range(start, end)
    lo, hi = business_day_bounds(start)[0], business_day_bounds(end)[1]
    in_range = (OrderItem.created_at >= lo, OrderItem.created_at < hi)
    with read_session() as s:
        total_qty = s.scalar(select(func.coalesce(func.sum(OrderItem.qty), 0)).where(*in_range))
        reasons = s.execute(
            select(OrderItem.cancel_reason, func.sum(OrderItem.qty))
            .where(*in_range, OrderItem.status == "cancelled").group_by(OrderItem.cancel_reason)
        ).all()
        timings = s.execute(
            select(OrderItem.station, OrderItem.created_at, OrderItem.ready_at)
            .where(*in_range, OrderItem.ready_at.is_not(None))
        ).all()
    by_reason: dict[str, int] = {}
    for reason, qty in reasons:
        by_reason[reason or "No reason"] = by_reason.get(reason or "No reason", 0) + int(qty)
    minutes: dict[str, list[float]] = {st: [] for st in STATIONS}
    for station, created, ready in timings:
        minutes[station].append((ready - created).total_seconds() / 60)
    return {
        "items_ordered": int(total_qty), "items_cancelled": sum(by_reason.values()),
        "cancel_reasons": dict(sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))),
        "kitchen_minutes": {st: (round(sum(v) / len(v), 1) if v else None) for st, v in minutes.items()},
    }


def _and(parts: list[str]) -> str:
    """'A', 'A and B', 'A, B and C'."""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _card(key: str, title: str, figure: str, sentence: str) -> dict:
    return {"key": key, "title": title, "figure": figure, "sentence": sentence}


def insight_cards(start: date, end: date) -> list[dict]:
    """The Insights page cards for business days start..end (each: title, figure, sentence)."""
    report = sales_summary(start, end)
    ops = operations(start, end)
    t = report["totals"]
    sold = [m for m in report["menu"] if m["qty"]]
    cards = []

    # Top 3 dishes by gross profit
    top = sorted(sold, key=lambda m: (-m["profit_paise"], m["name"]))[:TOP_N]
    if top:
        names = _and([f"{m['name']} ({whole_rupees(m['profit_paise'])})" for m in top])
        cards.append(_card("top_profit", "Most profitable dishes", whole_rupees(sum(m["profit_paise"] for m in top)),
                           f"Your top {len(top)} dishes by gross profit were {names}."))
    else:
        cards.append(_card("top_profit", "Most profitable dishes", "—", "No dishes were sold in this period."))

    # Lowest-margin dish (among dishes that sold)
    with_margin = [m for m in sold if m["margin_percent"] is not None]
    if with_margin:
        low = min(with_margin, key=lambda m: (m["margin_percent"], m["name"]))
        cards.append(_card("lowest_margin", "Lowest margin", f"{low['margin_percent']}%",
                           f"{low['name']} had the lowest margin at {low['margin_percent']}% "
                           f"({low['qty']} sold)."))

    # Slowest sellers (current dishes, unsold ones included)
    slow = sorted((m for m in report["menu"] if not m["archived"]), key=lambda m: (m["qty"], m["name"]))[:TOP_N]
    if slow:
        names = _and([f"{m['name']} ({m['qty']:,} sold)" for m in slow])
        cards.append(_card("slowest", "Slowest sellers", f"{slow[0]['qty']:,} sold",
                           f"The slowest sellers were {names}."))

    # Weekday vs weekend: average net sales per calendar day of each kind
    weekday = [d["net_sales_paise"] for d in report["daily"] if d["day"].weekday() < 5]
    weekend = [d["net_sales_paise"] for d in report["daily"] if d["day"].weekday() >= 5]
    if weekday and weekend and sum(weekday):
        wd, we = sum(weekday) / len(weekday), sum(weekend) / len(weekend)
        change = round((we - wd) * 100 / wd, 1)
        direction = "more" if change >= 0 else "less"
        cards.append(_card("weekend", "Weekend vs weekday", f"{change:+}%",
                           f"Weekends averaged {whole_rupees(round(we))} a day, {abs(change)}% {direction} "
                           f"than weekdays ({whole_rupees(round(wd))})."))
    else:
        cards.append(_card("weekend", "Weekend vs weekday", "—",
                           "This range needs sales on both weekdays and weekend days to compare."))

    # Peak hour (by seating time)
    peak = max(report["hours"], key=lambda h: (h["net_sales_paise"], -h["hour"]))
    if peak["net_sales_paise"]:
        cards.append(_card("peak_hour", "Peak hour", f"{peak['hour']:02d}:00",
                           f"The busiest hour was {peak['hour']:02d}:00–{(peak['hour'] + 1) % 24:02d}:00 "
                           f"(by seating time) with {whole_rupees(peak['net_sales_paise'])} from "
                           f"{plural(peak['bills'], 'bill')}."))

    # Busiest day
    best = max(report["daily"], key=lambda d: (d["net_sales_paise"], d["day"]))
    if best["net_sales_paise"]:
        cards.append(_card("busiest_day", "Busiest day", whole_rupees(best["net_sales_paise"]),
                           f"The busiest day was {best['day']:%a %d %b} with "
                           f"{whole_rupees(best['net_sales_paise'])} net sales from {plural(best['bills'], 'bill')}."))

    # Cancellation rate and top reason
    if ops["items_ordered"]:
        rate = round(ops["items_cancelled"] * 100 / ops["items_ordered"], 1)
        top_reason = next(iter(ops["cancel_reasons"].items()), None)
        reason_text = f" Top reason: {top_reason[0]} ({top_reason[1]})." if top_reason else ""
        cards.append(_card("cancellations", "Cancellations", f"{rate}%",
                           f"{rate}% of items ordered were cancelled ({ops['items_cancelled']:,} of "
                           f"{ops['items_ordered']:,}).{reason_text}"))

    # Discount share of sales
    if t["gross_sales_paise"]:
        share = round(t["discounts_paise"] * 100 / t["gross_sales_paise"], 1)
        cards.append(_card("discounts", "Discounts", f"{share}%",
                           f"Discounts were {share}% of sales ({whole_rupees(t['discounts_paise'])} of "
                           f"{whole_rupees(t['gross_sales_paise'])})."))

    # Kitchen time by station and the slowest
    times = {st: m for st, m in ops["kitchen_minutes"].items() if m is not None}
    if times:
        slowest = max(times, key=lambda st: (times[st], st))
        listing = _and([f"{st} {m} min" for st, m in times.items()])
        cards.append(_card("kitchen_time", "Kitchen time", f"{times[slowest]} min",
                           f"Average time from order to ready: {listing}. The slowest station is {slowest}."))
    return cards


def page(preset: str | None, start: date | None, end: date | None) -> dict:
    """Everything the /insights page shows apart from the chat: range and cards."""
    from app.services.sales import PRESETS, resolve_range

    start, end, preset = resolve_range(preset, start, end)
    return {"start": start, "end": end, "preset": preset, "presets": PRESETS, "cards": insight_cards(start, end)}
