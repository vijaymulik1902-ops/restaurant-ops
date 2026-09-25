"""Generate realistic past business days through the REAL service functions.

Used by `python -m app.seed --history N`. Every table visit is simulated as a series
of timed actions (seat, KOT, start, ready, serve, maybe cancel, bill, pay) processed in
time order with the clock set to that moment (app.clock_override, same as the tests),
so every rule, audit row, KOT/bill number and timestamp is produced exactly as in live use.
Deterministic for a given seed value.
"""
import heapq
import random
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select

from app.clock_override import override_now
from app.db import now as real_now
from app.db import read_session
from app.models import DiningTable, MenuItem, Order, Staff
from app.services import billing, business_day_of, expenses, kitchen, orders, tables
from app.services import sales

CANCEL_REASONS = ("Customer changed mind", "Wrong item entered", "Out of stock")
LATE_CANCEL_REASONS = ("Dropped while plating", "Burnt, remade not needed", "Guest left early")
PAYMENT_WEIGHTS = {"upi": 55, "cash": 30, "card": 15}
PREP_MINUTES = {"tandoor": (6, 12), "kitchen": (10, 20), "bar": (2, 6)}

# Volume per table per service (visits); weekends are ~40% busier
LUNCH_TURNS, DINNER_TURNS, WEEKEND_FACTOR = 0.5, 0.9, 1.4
# Monthly fixed costs, sized so a typical month is profitable but not absurdly so
SALARY_PER_STAFF_PAISE = 22_000_00
RENT_PER_TABLE_PAISE = 7_000_00
UTILITIES_PER_TABLE_PAISE = 2_500_00


class HistoryError(Exception):
    pass


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2000, 1, 1)

    def __call__(self) -> datetime:
        return self.current


@dataclass
class _Visit:
    table_id: int = 0
    section: str = ""
    waiter_id: int = 0
    guests: int = 2
    order_id: int = 0
    kots_left: int = 1
    items_open: int = 0
    late_night: bool = False
    last_done: datetime = datetime(2000, 1, 1)


@dataclass
class _Sim:
    rng: random.Random
    clock: _Clock
    table_rows: list
    waiters_by_section: dict
    any_waiter: int
    counter_id: int
    manager_id: int
    dishes_by_category: dict
    weights: dict
    cost_of: dict
    busy: set = field(default_factory=set)
    heap: list = field(default_factory=list)
    seq: int = 0
    stats: dict = field(default_factory=lambda: {"visits": 0, "turned_away": 0, "kots": 0, "items": 0,
                                                 "cancelled_items": 0, "discounts": 0, "big_discounts": 0,
                                                 "late_night": 0})
    cogs_since_purchase: int = 0

    # ---- scheduling
    def at(self, when: datetime, fn, *args) -> None:
        self.seq += 1
        heapq.heappush(self.heap, (when, self.seq, fn, args))

    def minutes(self, lo: float, hi: float) -> timedelta:
        return timedelta(minutes=self.rng.uniform(lo, hi))

    def run(self) -> None:
        while self.heap:
            when, _, fn, args = heapq.heappop(self.heap)
            self.clock.current = when
            fn(*args)

    # ---- a table visit, step by step
    def seat(self, guests: int, late_night: bool = False) -> None:
        free = [t for t in self.table_rows if t.id not in self.busy]
        if not free:
            self.stats["turned_away"] += 1
            return
        fitting = [t for t in free if t.capacity >= guests] or free
        table = self.rng.choice(fitting)
        v = _Visit(table_id=table.id, section=table.section, guests=guests, late_night=late_night,
                   waiter_id=self.waiters_by_section.get(table.section, self.any_waiter))
        opened, _ = tables.open_table(table.id, v.waiter_id, guests)
        v.order_id = opened["order_id"]
        self.busy.add(table.id)
        self.stats["visits"] += 1
        self.stats["late_night"] += late_night
        v.kots_left = 2 if self.rng.random() < 0.5 else 1
        now = self.clock.current
        self.at(now + self.minutes(3, 8), self.send_kot, v, True)
        if v.kots_left == 2:
            self.at(now + self.minutes(22, 38), self.send_kot, v, False)

    def _pick(self, category: str, n: int) -> list[MenuItem]:
        dishes = self.dishes_by_category.get(category) or []
        if not dishes or n <= 0:
            return []
        return self.rng.choices(dishes, weights=[self.weights[d.id] for d in dishes], k=n)

    def _lines(self, v: _Visit, first: bool) -> list[tuple[int, int, None]]:
        g, r = v.guests, self.rng
        if first:
            picks = (self._pick("Starters", r.randint(0, 1 + g // 3)) + self._pick("Mains", max(1, (g + 1) // 2))
                     + self._pick("Rice", r.randint(0, 1)) + self._pick("Breads", r.randint(1, g))
                     + self._pick("Beverages", r.randint(0, g)))
        else:
            picks = self._pick("Breads", r.randint(1, g)) + self._pick("Beverages", r.randint(0, 2))
        if not picks:  # menus without these categories: any dishes
            everything = [d for ds in self.dishes_by_category.values() for d in ds]
            picks = r.choices(everything, k=max(1, g))
        qty: dict[int, int] = {}
        for d in picks:
            qty[d.id] = qty.get(d.id, 0) + 1
        return [(dish_id, q, None) for dish_id, q in sorted(qty.items())]

    def send_kot(self, v: _Visit, first: bool) -> None:
        kot_id = str(uuid.UUID(int=self.rng.getrandbits(128), version=4))
        _, events = orders.send_kot(v.order_id, kot_id, v.waiter_id, self._lines(v, first))
        v.kots_left -= 1
        self.stats["kots"] += 1
        items = [(it["item_id"], ev.channel.split(":", 1)[1]) for ev in events if ev.type == "kot"
                 for it in ev.data["items"]]
        v.items_open += len(items)
        self.stats["items"] += len(items)
        now = self.clock.current
        for item_id, station in items:
            if self.rng.random() < 0.03:  # waiter cancels before the kitchen starts
                self.at(now + self.minutes(0.5, 1.5), self.cancel_pending, v, item_id)
            else:
                self.at(now + self.minutes(2, 5), self.start, v, item_id, station)

    def cancel_pending(self, v: _Visit, item_id: int) -> None:
        kitchen.cancel_item(item_id, self.rng.choice(CANCEL_REASONS), v.waiter_id)
        self.stats["cancelled_items"] += 1
        self._item_done(v)

    def start(self, v: _Visit, item_id: int, station: str) -> None:
        kitchen.start_item(item_id, station)
        if self.rng.random() < 0.005:  # rare: manager cancels something already cooking
            self.at(self.clock.current + self.minutes(2, 4), self.cancel_started, v, item_id)
            return
        lo, hi = PREP_MINUTES.get(station, (5, 15))
        self.at(self.clock.current + self.minutes(lo, hi), self.ready, v, item_id, station)

    def cancel_started(self, v: _Visit, item_id: int) -> None:
        kitchen.cancel_item(item_id, self.rng.choice(LATE_CANCEL_REASONS), self.manager_id)
        self.stats["cancelled_items"] += 1
        self._item_done(v)

    def ready(self, v: _Visit, item_id: int, station: str) -> None:
        kitchen.ready_item(item_id, station)
        self.at(self.clock.current + self.minutes(1, 4), self.serve, v, item_id)

    def serve(self, v: _Visit, item_id: int) -> None:
        kitchen.serve_item(item_id, v.waiter_id)
        self._item_done(v)

    def _item_done(self, v: _Visit) -> None:
        v.items_open -= 1
        v.last_done = self.clock.current
        if v.items_open == 0 and v.kots_left == 0:
            linger = (35, 50) if v.late_night else (15, 35)
            self.at(self.clock.current + self.minutes(*linger), self.bill, v)

    def bill(self, v: _Visit) -> None:
        order = orders.get_order(v.order_id)
        if order["in_kitchen"] or order["status"] != "open":
            return  # a second KOT is still cooking; its last item will schedule the bill
        subtotal = order["total_paise"]
        if subtotal == 0:  # everything was cancelled: nothing to bill
            orders.cancel_order(v.order_id, "All items cancelled", self.manager_id)
            self.busy.discard(v.table_id)
            return
        discount, by = 0, self.counter_id
        if self.rng.random() < 0.12:
            if self.rng.random() < 0.15:  # manager-approved larger discount
                discount, by = subtotal * self.rng.randint(12, 20) // 100, self.manager_id
                self.stats["big_discounts"] += 1
            else:
                discount = subtotal * self.rng.randint(3, 10) // 100
            discount -= discount % 100  # whole rupees
            self.stats["discounts"] += discount > 0
        bill, _ = billing.generate_bill(v.order_id, discount, by)
        for item in order["items"]:
            if item["status"] != "cancelled":
                self.cogs_since_purchase += self.cost_of[item["name"]] * item["qty"]
        self.at(self.clock.current + self.minutes(3, 8), self.pay, v, bill["bill_id"])

    def pay(self, v: _Visit, bill_id: int) -> None:
        mode = self.rng.choices(list(PAYMENT_WEIGHTS), weights=list(PAYMENT_WEIGHTS.values()))[0]
        billing.pay_bill(bill_id, mode, self.counter_id)
        self.busy.discard(v.table_id)

    # ---- expenses
    def buy_ingredients(self, day: date) -> None:
        if self.cogs_since_purchase <= 0:
            return
        amount = int(self.cogs_since_purchase * self.rng.uniform(1.02, 1.12))
        amount -= amount % 1000  # round to ₹10
        expenses.add_expense(day, "ingredients", amount, "Vegetables, dairy, meat, dry stock", self.manager_id)
        self.cogs_since_purchase = 0


def _at(day: date, hours: float) -> datetime:
    return datetime.combine(day, time()) + timedelta(hours=hours)


def generate_history(days: int, seed_value: int = 42, today: date | None = None) -> dict:
    """Simulate `days` past business days (ending yesterday). Requires a restaurant with no orders yet."""
    if days < 1:
        raise HistoryError("--history must be at least 1")
    rng = random.Random(seed_value)
    today = today or business_day_of(real_now())
    first = today - timedelta(days=days)

    with read_session() as s:
        if s.scalar(select(func.count(Order.id))):
            raise HistoryError("History needs an empty restaurant: run with --reset")
        table_rows = s.execute(select(DiningTable.id, DiningTable.section, DiningTable.capacity)).all()
        staff = s.execute(select(Staff.id, Staff.role, Staff.section).where(Staff.active.is_(True))).all()
        dishes = list(s.scalars(select(MenuItem).where(MenuItem.available.is_(True), MenuItem.archived.is_(False))
                                .order_by(MenuItem.id)))
        s.expunge_all()
    waiters = [x for x in staff if x.role == "waiter"]
    managers = [x.id for x in staff if x.role == "manager"]
    counters = [x.id for x in staff if x.role == "counter"] or managers
    if not table_rows or not waiters or not managers or not dishes:
        raise HistoryError("Seed tables, waiters, a manager and a menu first")

    by_cat: dict[str, list] = {}
    for d in dishes:
        by_cat.setdefault(d.category, []).append(d)
    sim = _Sim(
        rng=rng, clock=_Clock(), table_rows=table_rows,
        waiters_by_section={w.section: w.id for w in waiters if w.section},
        any_waiter=waiters[0].id, counter_id=counters[0], manager_id=managers[0],
        dishes_by_category=by_cat, weights={d.id: rng.uniform(0.4, 3.0) for d in dishes},
        cost_of={d.name: d.cost_paise for d in dishes},
    )

    n_tables = len(table_rows)
    late_days = set(rng.sample(range(days), k=min(days, 2 if days >= 5 else 1)))
    for i in range(days):
        day = first + timedelta(days=i)
        factor = WEEKEND_FACTOR if day.weekday() >= 5 else 1.0
        lunch = round(n_tables * LUNCH_TURNS * factor * rng.uniform(0.85, 1.15))
        dinner = round(n_tables * DINNER_TURNS * factor * rng.uniform(0.85, 1.15))
        for _ in range(lunch):  # 12:00-15:00, busiest around 13:00
            sim.at(_at(day, rng.triangular(12.0, 14.75, 13.0)), sim.seat, rng.randint(2, 4))
        for _ in range(dinner):  # 19:00-22:00 seating (out by ~23:30), busiest around 20:30
            sim.at(_at(day, rng.triangular(19.0, 22.0, 20.5)), sim.seat, rng.randint(2, 4))
        if i in late_days:  # a late table that pays after midnight (same business day)
            sim.at(_at(day, 23.5) + timedelta(minutes=rng.randint(0, 10)), sim.seat, rng.randint(2, 4), True)

    clock = sim.clock
    with override_now(clock):
        sim.run()

        n_staff = len(staff)
        for i in range(days):  # expenses, entered at 11:00 on their business day
            day = first + timedelta(days=i)
            clock.current = _at(day, 11)
            if day.weekday() in (0, 3):  # Monday and Thursday market runs
                sim.buy_ingredients(day)
            if day.day == 1:  # monthly: salaries and rent on the 1st
                month = day.strftime("%B")
                expenses.add_expense(day, "salaries", n_staff * SALARY_PER_STAFF_PAISE,
                                     f"Staff salaries, {month}", sim.manager_id)
                expenses.add_expense(day, "rent", n_tables * RENT_PER_TABLE_PAISE, f"Rent, {month}", sim.manager_id)
            if day.day == 5:  # monthly: utilities on the 5th
                amount = int(n_tables * UTILITIES_PER_TABLE_PAISE * rng.uniform(0.9, 1.1))
                expenses.add_expense(day, "utilities", amount - amount % 100,
                                     "Electricity, water, gas", sim.manager_id)
        last = first + timedelta(days=days - 1)
        clock.current = _at(last, 11)
        sim.buy_ingredients(last)  # settle the final stretch of ingredient use

    summary = sales.sales_summary(first, first + timedelta(days=days - 1))
    return {"first": first, "last": first + timedelta(days=days - 1), **sim.stats,
            "totals": summary["totals"]}


def print_summary(result: dict) -> None:
    t = result["totals"]
    rupees = lambda p: f"₹{p / 100:,.0f}"  # noqa: E731
    print(f"\nHistory: {result['first']} to {result['last']} "
          f"({t['days']} business days, {result['visits']} table visits, {result['turned_away']} turned away)")
    print(f"  Bills {t['bill_count']}, guests {t['guests']}, KOTs {result['kots']}, items {result['items']}, "
          f"cancelled items {result['cancelled_items']}, late-night tables {result['late_night']}")
    print(f"  Discounts on {result['discounts']} bills ({result['big_discounts']} manager-approved >10%)")
    print(f"  Net sales {rupees(t['net_sales_paise'])}  |  cost of goods {rupees(t['cost_of_goods_paise'])}  |  "
          f"gross profit {rupees(t['gross_profit_paise'])} ({t['gross_margin_percent']}%)")
    print(f"  Expenses {rupees(t['expenses_paise'])}  |  net profit {rupees(t['net_profit_paise'])}  |  "
          f"GST collected {rupees(t['gst_paise'])}")
