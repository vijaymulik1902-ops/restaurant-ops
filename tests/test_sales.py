"""Sales summary: follows CLAUDE.md "Sales report definitions" exactly."""
import csv
import io
from datetime import date, datetime

import pytest

from app.services import ServiceError, billing, expenses, kitchen, orders, sales
from conftest import open_and_order, serve_all
from test_query_counts import count_queries
from test_routes import login

DAY = date(2026, 9, 25)  # the clock fixture starts at 12:00 on this (Friday) business day


def _paid(db, lines, table_index=0, discount=0, mode="cash", by="manager", cancel=None):
    """Seat, order, (optionally cancel one dish), serve, bill and pay, all at the clock's time."""
    kot = open_and_order(db, lines, table_index)
    if cancel:
        item = next(i for i in orders.get_order(kot["order_id"])["items"] if i["name"] == cancel)
        kitchen.cancel_item(item["item_id"], "Out of stock", db["staff"]["manager"])
    serve_all(db, kot["order_id"])
    bill, _ = billing.generate_bill(kot["order_id"], discount, db["staff"][by])
    paid, _ = billing.pay_bill(bill["bill_id"], mode, db["staff"]["counter"])
    return paid


def test_definitions_net_profit_is_net_sales_minus_expenses_only(db, clock):
    # Bill A: 2 naan (2x4500) + 1 dal (18000) = 27000, discount 2000 -> net 25000, GST 1250
    a = _paid(db, [("naan", 2), ("dal", 1)], discount=2000, mode="upi")
    # Bill B: 2 lassi (2x7000) = 14000 -> net 14000, GST 700
    b = _paid(db, [("lassi", 2)], table_index=1, mode="cash")
    expenses.add_expense(DAY, "rent", 10000, None, db["staff"]["manager"])

    t = sales.sales_summary(DAY, DAY)["totals"]
    assert t["gross_sales_paise"] == 41000
    assert t["discounts_paise"] == 2000
    assert t["net_sales_paise"] == 39000
    assert t["gst_paise"] == 1950 == a["gst_paise"] + b["gst_paise"]  # reported separately
    assert t["cost_of_goods_paise"] == 2 * 1200 + 4500 + 2 * 2200      # 11300
    assert t["gross_profit_paise"] == 39000 - 11300
    assert t["gross_margin_percent"] == round(27700 * 100 / 39000, 1)
    assert t["expenses_paise"] == 10000 and t["expenses_by_category_paise"]["rent"] == 10000
    assert t["net_profit_paise"] == 39000 - 10000                      # NOT also minus cost of goods
    assert t["net_profit_paise"] != 39000 - 11300 - 10000
    assert (t["bill_count"], t["average_bill_paise"]) == (2, 19500)
    assert (t["guests"], t["average_spend_per_guest_paise"]) == (4, 9750)
    assert t["collected_paise"] == 39000 + 1950                         # what the till took in, incl. GST


def test_gst_is_not_revenue(db, clock):
    _paid(db, [("dal", 1)])  # 18000 + 900 GST
    r = sales.sales_summary(DAY, DAY)
    assert r["totals"]["net_sales_paise"] == 18000
    assert r["menu_revenue_paise"] == 18000
    assert r["payment_modes"]["cash"]["net_sales_paise"] == 18000
    assert r["daily"][0]["net_sales_paise"] == 18000
    assert r["hours"][12]["net_sales_paise"] == 18000


def test_unpaid_cancelled_orders_and_cancelled_items_are_excluded(db, clock):
    _paid(db, [("naan", 1), ("dal", 1)], cancel="Dal Tadka")          # dal cancelled on a paid bill
    unpaid = open_and_order(db, [("lassi", 3)], table_index=1)          # billed, never paid
    serve_all(db, unpaid["order_id"])
    billing.generate_bill(unpaid["order_id"], 0, db["staff"]["counter"])
    dropped = open_and_order(db, [("dal", 5)], table_index=2)           # whole order cancelled
    orders.cancel_order(dropped["order_id"], "Guests left", db["staff"]["manager"])

    r = sales.sales_summary(DAY, DAY)
    assert r["totals"]["bill_count"] == 1
    assert r["totals"]["gross_sales_paise"] == 4500
    assert r["totals"]["cost_of_goods_paise"] == 1200
    by_name = {m["name"]: m for m in r["menu"]}
    assert by_name["Dal Tadka"]["qty"] == 0 and by_name["Sweet Lassi"]["qty"] == 0
    assert by_name["Butter Naan"]["qty"] == 1


def test_business_day_boundaries_are_inclusive(db, clock):
    def pay_at(ts: datetime):
        clock.current = ts
        return _paid(db, [("naan", 1)])

    pay_at(datetime(2026, 9, 25, 3, 59))   # before 04:00 -> business day 24 Sep: OUT
    pay_at(datetime(2026, 9, 25, 4, 0))    # start of 25 Sep: IN
    pay_at(datetime(2026, 9, 26, 3, 59))   # still business day 25 Sep (late night): IN
    pay_at(datetime(2026, 9, 26, 4, 0))    # business day 26 Sep: OUT
    clock.current = datetime(2026, 9, 26, 12, 0)
    expenses.add_expense(date(2026, 9, 25), "utilities", 500, None, db["staff"]["manager"])  # end date: IN
    expenses.add_expense(date(2026, 9, 26), "utilities", 700, None, db["staff"]["manager"])  # OUT

    t = sales.sales_summary(DAY, DAY)["totals"]
    assert t["bill_count"] == 2 and t["net_sales_paise"] == 9000
    assert t["expenses_paise"] == 500
    two_days = sales.sales_summary(date(2026, 9, 24), date(2026, 9, 26))
    assert two_days["totals"]["bill_count"] == 4
    assert [d["bills"] for d in two_days["daily"]] == [1, 2, 1]


def test_margins_when_revenue_is_zero(db):
    r = sales.sales_summary(date(2026, 1, 1), date(2026, 1, 1))
    t = r["totals"]
    assert t["net_sales_paise"] == 0 and t["gross_margin_percent"] is None
    assert t["average_bill_paise"] == 0 and t["average_spend_per_guest_paise"] == 0
    assert all(m["margin_percent"] is None and m["share_percent"] is None for m in r["menu"])
    assert r["payment_modes"]["upi"]["share_percent"] is None
    assert all(c["margin_percent"] is None for c in r["categories"])


def test_trend_menu_and_modes_add_up_to_totals(db, clock):
    _paid(db, [("naan", 3), ("dal", 1)], discount=1000, mode="upi")
    clock.advance(hours=7)  # 19:00
    _paid(db, [("lassi", 1), ("dal", 2)], table_index=1, mode="card")
    r = sales.sales_summary(date(2026, 9, 20), DAY)
    t = r["totals"]
    assert sum(d["net_sales_paise"] for d in r["daily"]) == t["net_sales_paise"]
    assert sum(d["gross_profit_paise"] for d in r["daily"]) == t["gross_profit_paise"]
    assert r["menu_revenue_paise"] == t["gross_sales_paise"] == sum(m["revenue_paise"] for m in r["menu"])
    assert sum(m["cost_paise"] for m in r["menu"]) == t["cost_of_goods_paise"]
    assert sum(c["revenue_paise"] for c in r["categories"]) == t["gross_sales_paise"]
    assert sum(m["net_sales_paise"] for m in r["payment_modes"].values()) == t["net_sales_paise"]
    assert sum(h["bills"] for h in r["hours"]) == t["bill_count"]
    assert (r["hours"][12]["bills"], r["hours"][19]["bills"]) == (1, 1)
    assert t["days"] == 6 and t["average_daily_net_sales_paise"] == (2 * t["net_sales_paise"] + 6) // 12
    assert sum(m["top_profit"] for m in r["menu"]) <= 5 and sum(m["low_qty"] for m in r["menu"]) <= 5
    assert next(m for m in r["menu"] if m["name"] == "Dal Tadka")["top_profit"]


@pytest.mark.parametrize("start, end", [(date(2026, 9, 25), date(2026, 9, 24)),
                                        (date(2025, 9, 24), date(2026, 9, 25))])  # 367 days
def test_bad_ranges_rejected(db, start, end):
    with pytest.raises(ServiceError):
        sales.sales_summary(start, end)


def test_366_day_range_is_allowed(db):
    sales.sales_summary(date(2025, 9, 25), date(2026, 9, 25))


def test_presets():
    today = date(2026, 9, 25)  # Friday
    assert sales.preset_range("today", today) == (today, today)
    assert sales.preset_range("yesterday", today) == (date(2026, 9, 24),) * 2
    assert sales.preset_range("this_week", today) == (date(2026, 9, 21), today)
    assert sales.preset_range("this_month", today) == (date(2026, 9, 1), today)
    assert sales.preset_range("last_month", today) == (date(2026, 8, 1), date(2026, 8, 31))
    assert sales.preset_range("last_month", date(2026, 1, 10)) == (date(2025, 12, 1), date(2025, 12, 31))


def test_query_count_does_not_grow_with_bills(db, clock):
    def run() -> int:
        with count_queries() as statements:
            sales.sales_summary(date(2026, 9, 1), date(2026, 9, 30))
        return len(statements)

    _paid(db, [("naan", 1)])
    few = run()
    for i in range(12):
        clock.advance(minutes=30)
        _paid(db, [("naan", 1), ("dal", 1), ("lassi", 2)], table_index=i % 3, mode=("cash", "upi", "card")[i % 3])
    assert run() == few
    assert few <= 10


# ---------- routes ----------

def _csv_totals(text: str) -> dict[str, str]:
    return {row[0]: row[3] for row in csv.reader(io.StringIO(text)) if len(row) >= 4 and row[1] == ""}


def test_csv_matches_page_totals(db, clock):
    _paid(db, [("naan", 2), ("dal", 1)], discount=2000)
    _paid(db, [("lassi", 1)], table_index=1)
    c = login("manager")
    q = "start=2026-09-25&end=2026-09-25"
    page = c.get(f"/reports/sales?{q}").text
    resp = c.get(f"/reports/sales.csv?{q}")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    totals = _csv_totals(resp.text)
    t = sales.sales_summary(DAY, DAY)["totals"]
    as_rupees = lambda p: f"{p // 100}.{p % 100:02d}"  # noqa: E731
    assert totals["Net sales"] == as_rupees(t["net_sales_paise"]) == "320.00"
    assert totals["Gross profit"] == as_rupees(t["gross_profit_paise"])
    assert totals["Bill discounts"] == "-20.00"
    for label in ("Net sales", "Gross profit"):  # the same figures appear on the page
        paise = int(totals[label].replace(".", ""))
        from app.web import rupees
        assert rupees(paise) in page
    dish_rows = [r for r in csv.reader(io.StringIO(resp.text)) if len(r) == 8 and r[2].isdigit() and r[1]]
    assert sum(int(r[2]) for r in dish_rows) == 4  # 2 naan + 1 dal + 1 lassi


def test_sales_page_renders_presets_cards_and_note(db, clock):
    _paid(db, [("dal", 1)])
    c = login("manager")
    for preset in sales.PRESETS:
        assert c.get(f"/reports/sales?preset={preset}").status_code == 200
    html = c.get("/reports/sales?preset=today").text
    assert "Net profit = net sales - expenses recorded in this period." in html
    assert 'class="profit"' in html and 'id="menu-table"' in html and 'id="sales-data"' in html
    expenses.add_expense(DAY, "salaries", 10_000_000, None, db["staff"]["manager"])
    assert 'class="loss"' in c.get("/reports/sales?preset=today").text
    bad = c.get("/reports/sales?start=2026-09-25&end=2026-09-01")
    assert bad.status_code == 303


def test_expenses_page_add_list_delete(db, clock):
    c = login("manager")
    assert c.post("/expenses", data={"spent_on": "2026-09-25", "category": "ingredients",
                                     "amount": "1,250.50", "note": "Veg market"}).status_code == 303
    html = c.get("/expenses?start=2026-09-01&end=2026-09-30").text
    assert "₹1,250.50" in html and "Veg market" in html
    listed = expenses.list_expenses(date(2026, 9, 1), date(2026, 9, 30))
    assert listed["by_category"]["ingredients"] == 125050 and listed["total_paise"] == 125050
    # Future date refused (today is 25 Sep on the test clock)
    c.post("/expenses", data={"spent_on": "2026-09-26", "category": "rent", "amount": "100"},
           headers={"referer": "http://testserver/expenses"})
    assert "future" in c.get("/expenses").text
    # Delete (with its confirm form)
    eid = listed["rows"][0]["expense_id"]
    assert c.post(f"/expenses/{eid}/delete", data={"reason": "duplicate"}).status_code == 303
    assert expenses.list_expenses(date(2026, 9, 1), date(2026, 9, 30))["total_paise"] == 0


# ---------- Round 7, Part A: unambiguous labels ----------

def test_average_labels_are_unambiguous_with_tooltips(db, clock):
    _paid(db, [("dal", 1)])
    counter_page = login("counter").get("/reports/day-close?day=2026-09-25").text
    assert "Average bill (incl. GST)" in counter_page and 'class="tip"' in counter_page
    page = login("manager").get("/reports/sales?preset=today").text
    for label in ("Average bill (net sales, excl. GST)", "Average net sales per calendar day",
                  "Peak hours (by seating time)"):
        assert label in page
    assert page.count('class="tip"') >= 3 and page.count("data-tip=") >= 3


def test_chart_js_is_local_and_charts_never_stack():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "app" / "static"
    assert "chart.js@4.4.4" in (static / "chart.umd.min.js").read_text()[:400]
    js = (static / "sales.js").read_text()
    assert "Chart.getChart(canvas)" in js and "existing.destroy()" in js
    assert "maintainAspectRatio: false" in js and ".innerHTML" not in js
