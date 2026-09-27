"""Manager Home: today at a glance. Read-only, composed from existing tested services."""
from datetime import timedelta

from app.config import WARN_KITCHEN_MIN
from app.models import STATIONS
from app.services import kitchen
from app.services.insights import insight_cards
from app.services.sales import current_business_day, sales_summary
from app.services.tables import list_tables


def dashboard() -> dict:
    """Today's stat tiles, the floor map, and the top 3 insight cards (last 30 days)."""
    today = current_business_day()
    totals = sales_summary(today, today)["totals"]
    tables = list_tables()
    late = sum(1 for st in STATIONS for i in kitchen.live_items(st)
               if i["status"] != "ready" and i["age_seconds"] >= WARN_KITCHEN_MIN * 60)
    return {
        "today": today,
        "net_sales_paise": totals["net_sales_paise"],
        "bills": totals["bill_count"],
        "tables_total": len(tables),
        "tables_occupied": sum(1 for t in tables if t["status"] != "available"),
        "open_orders": sum(1 for t in tables if t["order_id"]),
        "items_late": late,
        "tables": tables,
        "insights": insight_cards(today - timedelta(days=29), today)[:3],
    }
