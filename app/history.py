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
from app.models import Booking, DiningTable, MenuItem, Order, Staff
from app.services import ServiceError, billing, bookings, business_day_of, expenses, kitchen, orders, tables
from app.services import sales

CANCEL_REASONS = ("Customer changed mind", "Wrong item entered", "Out of stock")
LATE_CANCEL_REASONS = ("Dropped while plating", "Burnt, remade not needed", "Guest left early")
PAYMENT_WEIGHTS = {"upi": 55, "cash": 30, "card": 15}
PREP_MINUTES = {"tandoor": (6, 12), "kitchen": (10, 20), "bar": (2, 6)}

# Weekend dinners: about a quarter of the tables are booked ahead; ~8% of bookings don't show.
# No-shows are chosen deterministically (evenly spaced), at least 3 once there are 24+ bookings,
# so a 30-day demo always has a non-zero no-show insight.
BOOKED_SHARE, NO_SHOW_RATE, MIN_NO_SHOWS = 0.25, 0.08, 3
GUEST_NAMES = ("Aarav Mehta", "Priya Nair", "Rohit Kulkarni", "Ananya Iyer", "Vikram Joshi", "Sana Shaikh",
               "Karan Malhotra", "Neha Deshpande", "Arjun Rao", "Meera Pillai", "Farhan Qureshi", "Isha Patil",
               "Dev Sharma", "Kavya Menon", "Nikhil Gupta", "Tara Bhosale")

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
                                                 "late_night": 0, "bookings": 0, "no_shows": 0,
                                                 "booking_full": 0, "booking_gave_up": 0})
    section_of: dict = field(default_factory=dict)
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
        """A walk-in: the waiter tries free tables (fitting ones first). Tables held for a
        booking refuse walk-ins, exactly as on the live floor."""
        free = [t for t in self.table_rows if t.id not in self.busy]
        fitting = [t for t in free if t.capacity >= guests] or free
        self.rng.shuffle(fitting)
        for table in fitting:
            waiter_id = self.waiters_by_section.get(table.section, self.any_waiter)
            try:
                opened, _ = tables.open_table(table.id, waiter_id, guests)
            except ServiceError:
                continue  # reserved soon: try another table
            self._visit(table.id, table.section, waiter_id, guests, opened["order_id"], late_night)
            return
        self.stats["turned_away"] += 1

    def _visit(self, table_id: int, section: str, waiter_id: int, guests: int, order_id: int,
               late_night: bool = False) -> None:
        v = _Visit(table_id=table_id, section=section, guests=guests, late_night=late_night,
                   waiter_id=waiter_id, order_id=order_id)
        self.busy.add(table_id)
        self.stats["visits"] += 1
        self.stats["late_night"] += late_night
        v.kots_left = 2 if self.rng.random() < 0.5 else 1
        now = self.clock.current
        self.at(now + self.minutes(3, 8), self.send_kot, v, True)
        if v.kots_left == 2:
            self.at(now + self.minutes(22, 38), self.send_kot, v, False)

    # ---- bookings (weekend dinners)
    def book(self, party: int, starts_at: datetime, no_show: bool) -> None:
        """The counter takes a phone booking in the morning for tonight (auto-assigned table)."""
        booking_id = self.book_only(party, starts_at)
        if booking_id is None:
            return
        if no_show:
            self.at(starts_at + self.minutes(16, 30), self.no_show, booking_id)
        else:
            self.at(starts_at + self.minutes(-8, 12), self.arrive, booking_id, 0)

    def arrive(self, booking_id: int, tries: int) -> None:
        try:
            seated, _ = bookings.seat_booking(booking_id, self.counter_id)
        except ServiceError:  # walk-ins still at the table: wait a little, then give up
            if tries < 6:
                self.at(self.clock.current + timedelta(minutes=5), self.arrive, booking_id, tries + 1)
            else:
                bookings.cancel_booking(booking_id, "Table not ready, guests left", self.counter_id)
                self.stats["booking_gave_up"] += 1
            return
        b = bookings.get_booking(booking_id, include_phone=False)
        section = self.section_of[b["table_id"]]
        waiter_id = self.waiters_by_section.get(section, self.any_waiter)
        self._visit(b["table_id"], section, waiter_id, b["party_size"], seated["order_id"])

    def book_only(self, party: int, starts_at: datetime) -> int | None:
        """Create a booking with a made-up demo phone number; None if no table fits."""
        phone = "+9198" + "".join(str(self.rng.randint(0, 9)) for _ in range(8))
        try:
            made, _ = bookings.create_booking(self.rng.choice(GUEST_NAMES), phone, party, starts_at, 90, None,
                                              None, self.counter_id)
        except ServiceError:
            self.stats["booking_full"] += 1
            return None
        self.stats["bookings"] += 1
        return made["booking_id"]

    def no_show(self, booking_id: int) -> None:
        bookings.mark_no_show(booking_id, self.manager_id)
        self.stats["no_shows"] += 1

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
    """Simulate `days` past business days (ending yesterday). Requires a restaurant with no orders yet.
    Bookings for today come from add_demo_bookings() (`seed --demo-bookings`), not from here."""
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
        section_of={t.id: t.section for t in table_rows},
    )
    max_capacity = max(t.capacity for t in table_rows)
    planned: list[tuple[datetime, int, datetime]] = []  # weekend bookings: (taken at, party, starts at)

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
        if day.weekday() >= 5:  # weekend dinner bookings, taken at 11:00 that morning
            for _ in range(round(n_tables * BOOKED_SHARE)):
                start = _at(day, rng.triangular(19.0, 21.5, 20.0))
                start = start.replace(minute=start.minute - start.minute % 15)
                planned.append((_at(day, 11) + timedelta(minutes=rng.randint(0, 60)),
                                min(rng.randint(2, 6), max_capacity), start))
        if i in late_days:  # a late table that pays after midnight (same business day)
            sim.at(_at(day, 23.5) + timedelta(minutes=rng.randint(0, 10)), sim.seat, rng.randint(2, 4), True)

    no_shows = _no_show_picks(len(planned))
    for i, (taken_at, party, start) in enumerate(planned):
        sim.at(taken_at, sim.book, party, start, i in no_shows)

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


def _no_show_picks(total: int) -> set[int]:
    """Which of `total` planned bookings don't show: ~8%, evenly spaced (deterministic),
    at least MIN_NO_SHOWS once there are 8 x MIN_NO_SHOWS bookings."""
    k = max(round(total * NO_SHOW_RATE), min(MIN_NO_SHOWS, total // 8))
    return {int((j + 0.5) * total / k) for j in range(k)} if k else set()


# ---- `seed --demo-bookings`: today's reservations for a live demo
DEMO_TAG = bookings.DEMO_TAG  # hidden marker; the service strips it from every display
# (minutes from now, party, guest, note): Reserved hold now, later tonight, a birthday, and a late one
DEMO_PLAN = ((30, 2, "Rhea Kapoor", ""), (120, 4, "Imran Shaikh", ""), (240, 6, "Mehta family", "birthday"),
             (-20, 4, "Joshi party", ""))


def add_demo_bookings() -> list[str]:
    """Book tables for TODAY relative to the current time, through the real service. Adds to the
    existing database, never resets it. Idempotent: a guest from DEMO_PLAN that already has a
    [demo]-tagged booking in the next/last 12 hours is skipped. Returns one line per booking."""
    current = real_now().replace(second=0, microsecond=0)
    window = (current - timedelta(hours=12), current + timedelta(hours=12))
    with read_session() as s:
        staff_id = s.scalar(select(Staff.id).where(Staff.role.in_(("counter", "manager")), Staff.active.is_(True))
                            .order_by(Staff.role.desc(), Staff.id))  # the counter if there is one
        existing = set(s.scalars(select(Booking.guest_name).where(
            Booking.note.contains(DEMO_TAG), Booking.starts_at >= window[0], Booking.starts_at <= window[1])))
    if staff_id is None:
        raise HistoryError("Seed staff first (python -m app.seed)")
    free_now = {t["table_id"] for t in tables.list_tables() if t["status"] == "available" and not t["hold"]}
    lines, clock = [], _Clock()
    for minutes, party, guest, note in DEMO_PLAN:
        start = current + timedelta(minutes=minutes)
        if guest in existing:
            lines.append(f"  {guest}: already booked, skipped")
            continue
        options = bookings.suggest_tables(start, bookings.DEFAULT_DURATION, party)
        table = next((t for t in options if t["table_id"] in free_now), options[0] if options else None)
        if table is None:
            lines.append(f"  {guest}: no free table fits {party} at {start:%H:%M}, skipped")
            continue
        # a booking that started 20 minutes ago was taken earlier in the day: create it "then";
        # the others are created now
        clock.current = current if minutes >= 0 else start - timedelta(hours=2)
        with override_now(clock):
            made, _ = bookings.create_booking(guest, None, party, start, bookings.DEFAULT_DURATION, table["table_id"],
                                              note or None, staff_id, demo=True)
        free_now.discard(table["table_id"])
        when = "started 20 min ago, shows as late" if minutes < 0 else f"in {minutes} min"
        lines.append(f"  {guest}, party of {party}: table {made['table_number']} at {start:%H:%M} ({when})")
    return lines


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
    print(f"  Operating expenses {rupees(t['operating_expenses_paise'])}  |  net profit {rupees(t['net_profit_paise'])} "
          f"({t['net_margin_percent']}%)  |  ingredient purchases {rupees(t['ingredient_purchases_paise'])} (in COGS)")
    print(f"  GST collected {rupees(t['gst_paise'])} (not revenue)")
    print(f"  Bookings {result['bookings']} ({result['no_shows']} no-shows, {result['booking_gave_up']} gave up "
          f"waiting, {result['booking_full']} couldn't be placed)")
