"""Check the database after a load test. Exits 1 if any invariant is broken.

    DB_PATH=/path/to/loadtest.db python loadtest/verify.py

Checks:
  1. No duplicate KOTs: KOT numbers are unique within each business day, and no order has two
     KOTs with identical lines sent within 5 s of each other (a double-tap that got through).
  2. No day-close mismatches for paid orders: every business day's day_close lists no paid
     order, and no paid order still has items pending/preparing/ready. (Orders still in
     progress when the test stopped are reported separately, not as failures.)
  3. Bills reconcile with order lines: subtotal = non-cancelled lines, GST = half-up
     percentage, total = subtotal - discount + GST, bill numbers 1..N with no gaps or repeats.
  4. Tables agree with orders: free tables have no live order, occupied/billing tables have
     exactly one in the matching state.
"""
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import read_session  # noqa: E402
from app.models import Bill, DiningTable, Kot, Order, OrderItem  # noqa: E402
from app.services import business_day_of, reports  # noqa: E402
from app.services.billing import gst_for  # noqa: E402

failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("  ok    " if ok else "  FAIL  ") + message)
    if not ok:
        failures.append(message)


def main() -> int:
    with read_session() as s:
        kots = s.execute(select(Kot.id, Kot.order_id, Kot.number, Kot.created_at)).all()
        items = s.execute(select(OrderItem.id, OrderItem.order_id, OrderItem.kot_id, OrderItem.menu_item_id,
                                 OrderItem.qty, OrderItem.note, OrderItem.status, OrderItem.unit_price_paise)).all()
        orders = {o.id: o for o in s.execute(select(Order.id, Order.status, Order.table_id)).all()}
        bills = s.execute(select(Bill.id, Bill.bill_no, Bill.order_id, Bill.subtotal_paise, Bill.discount_paise,
                                 Bill.gst_percent, Bill.gst_paise, Bill.total_paise, Bill.paid_at)).all()
        tables = s.execute(select(DiningTable.id, DiningTable.number, DiningTable.status)).all()

    print(f"Data: {len(orders)} orders, {len(kots)} KOTs, {len(items)} items, {len(bills)} bills, {len(tables)} tables")

    # 1. duplicate KOTs
    print("1. Duplicate KOTs")
    numbers = Counter((business_day_of(k.created_at), k.number) for k in kots)
    dup_numbers = [key for key, n in numbers.items() if n > 1]
    check(not dup_numbers, f"KOT numbers unique per business day ({len(dup_numbers)} repeats)")
    lines_by_kot: dict[str, tuple] = defaultdict(tuple)
    for it in items:
        lines_by_kot[it.kot_id] += ((it.menu_item_id, it.qty, it.note or ""),)
    by_order = defaultdict(list)
    for k in kots:
        by_order[k.order_id].append(k)
    double_taps = 0
    for order_kots in by_order.values():
        order_kots.sort(key=lambda k: k.created_at)
        for a, b in zip(order_kots, order_kots[1:]):
            same_lines = sorted(lines_by_kot[a.id]) == sorted(lines_by_kot[b.id])
            if same_lines and b.created_at - a.created_at < timedelta(seconds=5):
                double_taps += 1
    check(double_taps == 0, f"no two KOTs with identical lines within 5 s on one order ({double_taps} found)")
    # Load-test KOTs carry a note tag unique to the form ("lt-xxxxxxxx"): one tag, one KOT
    tags = Counter()
    for kot_id, lines in lines_by_kot.items():
        for _, _, note in lines:
            if note.startswith("lt-"):
                tags[note] += 1
    reused = sum(1 for n in tags.values() if n > 1)
    check(reused == 0, f"each double-tapped form saved once ({len(tags)} tagged KOTs, {reused} saved twice)")
    check(all(lines_by_kot[k.id] for k in kots), "every KOT has at least one item")

    # 2. day close
    print("2. Day close")
    days = sorted({business_day_of(b.paid_at) for b in bills if b.paid_at})
    paid_mismatches, in_flight = 0, 0
    for day in days:
        for m in reports.day_close(day)["mismatches"]:
            if m["order_status"] == "paid":
                paid_mismatches += 1
            else:
                in_flight += 1
    check(paid_mismatches == 0, f"no day-close mismatch on a paid order across {len(days)} business day(s)")
    unserved_paid = sum(1 for it in items if orders[it.order_id].status == "paid"
                        and it.status in ("pending", "preparing", "ready"))
    check(unserved_paid == 0, f"no paid order has unserved items ({unserved_paid} found)")
    print(f"  info  {in_flight} item(s) on orders still in progress when the test stopped")

    # 3. bills
    print("3. Bills reconcile with order lines")
    line_totals = defaultdict(int)
    for it in items:
        if it.status != "cancelled":
            line_totals[it.order_id] += it.qty * it.unit_price_paise
    bad_subtotal = [b.bill_no for b in bills if b.subtotal_paise != line_totals[b.order_id]]
    bad_gst = [b.bill_no for b in bills if b.gst_paise != gst_for(b.subtotal_paise - b.discount_paise, b.gst_percent)]
    bad_total = [b.bill_no for b in bills if b.total_paise != b.subtotal_paise - b.discount_paise + b.gst_paise]
    check(not bad_subtotal, f"subtotal = sum of non-cancelled lines ({len(bad_subtotal)} wrong)")
    check(not bad_gst, f"GST = half-up percentage of (subtotal - discount) ({len(bad_gst)} wrong)")
    check(not bad_total, f"total = subtotal - discount + GST ({len(bad_total)} wrong)")
    nos = sorted(b.bill_no for b in bills)
    check(nos == list(range(1, len(nos) + 1)), "bill numbers run 1..N with no gaps or repeats")
    check(len({b.order_id for b in bills}) == len(bills), "at most one bill per order")
    paid_status = [b.bill_no for b in bills if (b.paid_at is not None) != (orders[b.order_id].status == "paid")]
    check(not paid_status, f"paid bills <-> paid orders ({len(paid_status)} disagree)")

    # 4. tables
    print("4. Tables agree with orders")
    live = defaultdict(list)
    for o in orders.values():
        if o.status in ("open", "billed"):
            live[o.table_id].append(o.status)
    expected = {"available": [], "occupied": ["open"], "billing": ["billed"]}
    wrong = [t.number for t in tables if live[t.id] != expected[t.status]]
    check(not wrong, f"every table's status matches its live order ({len(wrong)} wrong: {wrong[:10]})")

    print("\nRESULT:", "PASS" if not failures else f"FAIL ({len(failures)} check(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
