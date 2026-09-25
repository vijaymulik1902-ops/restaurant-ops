"""Load test: a busy dinner service on 150 tables.

Seed first (see README "Load test"):
    python -m app.seed --reset --tables 150 --loadtest-staff 30

Users (like real phones, each also keeps a live /stream connection open):
    WaiterUser  (weight 7): opens a free table in its section, sends 2-4 KOTs (one of them
                            double-tapped: same kot_id posted twice), marks ready items served
    ChefUser    (weight 3): loads /kitchen, starts and readies items for its station
    CounterUser (weight 1): bills orders whose items are all served, pays with a random mode

Request names group URLs by shape ("/orders/[id]") so the stats read as page types.
POSTs are measured on their own (redirects not followed), so a POST's time is the write.
"""
import itertools
import os
import random
import re

import time

import gevent
import requests
from locust import HttpUser, between, events, task

LT_STAFF = int(os.getenv("LT_STAFF", "30"))
LT_PIN = os.getenv("LT_PIN", "2468")
COUNTER_NAME = os.getenv("LT_COUNTER_NAME", "Counter")
COUNTER_PIN = os.getenv("LT_COUNTER_PIN", "9090")
HOLD_SSE = os.getenv("LT_SSE", "1") == "1"

_waiter_ids = itertools.count()
_chef_ids = itertools.count()

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
FREE_TABLE_RE = re.compile(r'id="table-(\d+)" href="/floor/tables/\d+/open"\s+class="tcard st-available')
KOT_ID_RE = re.compile(r'name="kot_id" value="([0-9a-f-]{36})"')
DISH_RE = re.compile(r'<input type="number" name="qty_(\d+)"[^>]*>')
SERVE_RE = re.compile(r'action="/items/(\d+)/serve"')
LIVE_BADGE_RE = re.compile(r'class="badge st-(pending|preparing|ready)"')
KITCHEN_CARD_RE = re.compile(r'id="item-(\d+)" class="kcard st-(pending|preparing)')
BILLABLE_RE = re.compile(r'href="/counter/orders/(\d+)"\s+class="tcard st-occupied')
VERSION_RE = re.compile(r'name="version" value="(\d+)"')


class Staff(HttpUser):
    abstract = True
    wait_time = between(1, 3)

    def login(self, name: str, pin: str) -> None:
        page = self.client.get("/login", name="/login")
        token = CSRF_RE.search(page.text).group(1)
        with self.client.post("/login", data={"name": name, "pin": pin, "csrf_token": token},
                              allow_redirects=False, name="POST /login", catch_response=True) as resp:
            if resp.status_code != 303:
                resp.failure(f"login {name} -> {resp.status_code}")
                return
        # The session keeps the same CSRF token after login; send it like HTMX does
        self.client.headers["X-CSRF-Token"] = token
        if HOLD_SSE:
            self._sse = gevent.spawn(self._hold_stream)

    def _hold_stream(self) -> None:
        """Keep a live /stream open, as every phone and tablet does, and read its events.

        Uses its OWN session (a browser also gives EventSource its own connection): a
        requests.Session must not be shared between two greenlets at once.
        """
        session = requests.Session()
        session.cookies.update(self.client.cookies)
        while True:
            started = time.perf_counter()
            try:
                with session.get(f"{self.host}/stream", stream=True, timeout=(5, 60)) as resp:
                    self.environment.events.request.fire(
                        request_type="SSE", name="/stream (connect)", response_length=0, exception=None,
                        response_time=(time.perf_counter() - started) * 1000, context={}, response=resp)
                    for _ in resp.iter_lines():
                        pass
            except Exception:  # noqa: BLE001 - reconnect like app.js does
                gevent.sleep(1)

    def on_stop(self) -> None:
        if getattr(self, "_sse", None):
            self._sse.kill(block=False)

    def post(self, url: str, data: dict, name: str, expect_prefix: str | None = None, **kw):
        """POST without following the redirect; the redirect target tells us if it worked."""
        with self.client.post(url, data=data, allow_redirects=False, name=name, catch_response=True, **kw) as resp:
            location = resp.headers.get("location", "")
            if resp.status_code not in (200, 303):
                resp.failure(f"{resp.status_code}")
            elif expect_prefix and not location.startswith(expect_prefix):
                # A rule said no (e.g. another waiter took the table first): expected under load
                resp.success()
            return resp


class WaiterUser(Staff):
    weight = 7

    def on_start(self) -> None:
        self.my_orders: list[int] = []
        self.login(f"LT Waiter {next(_waiter_ids) % LT_STAFF + 1:02d}", LT_PIN)

    @task(1)
    def seat_and_order(self) -> None:
        if len(self.my_orders) >= 3:
            return
        free = FREE_TABLE_RE.findall(self.client.get("/floor", name="/floor").text)
        if not free:
            return
        table_id = random.choice(free)
        resp = self.post(f"/floor/tables/{table_id}/open", {"guest_count": random.randint(2, 4)},
                         name="POST /floor/tables/[id]/open", expect_prefix="/orders/")
        location = resp.headers.get("location", "")
        if not location.startswith("/orders/"):
            return  # someone else seated it a moment earlier
        order_id = int(location.rsplit("/", 1)[1])
        double_tap = random.randint(1, random.randint(2, 4))
        kots = random.randint(2, 4)
        for n in range(1, kots + 1):
            page = self.client.get(f"/orders/{order_id}", name="/orders/[id]").text
            kot_id = KOT_ID_RE.search(page)
            dishes = [m.group(1) for m in DISH_RE.finditer(page) if "disabled" not in m.group(0)]
            if not kot_id or not dishes:
                break
            form = {"kot_id": kot_id.group(1)}
            picks = random.sample(dishes, k=min(len(dishes), random.randint(1, 3)))
            for dish in picks:
                form[f"qty_{dish}"] = str(random.randint(1, 2))
            # A tag unique to this form: if the double-tap below were ever saved twice,
            # verify.py would find two KOTs carrying the same tag
            form[f"note_{picks[0]}"] = f"lt-{kot_id.group(1)[:8]}"
            self.post(f"/orders/{order_id}/kot", form, name="POST /orders/[id]/kot", expect_prefix="/orders/")
            if n == double_tap:  # the same kot_id again: must not create a second KOT
                self.post(f"/orders/{order_id}/kot", form, name="POST /orders/[id]/kot (double-tap)",
                          expect_prefix="/orders/")
            gevent.sleep(random.uniform(0.5, 2))
        self.my_orders.append(order_id)

    @task(3)
    def serve_ready(self) -> None:
        for order_id in list(self.my_orders):
            page = self.client.get(f"/orders/{order_id}/items", name="/orders/[id]/items").text
            for item_id in SERVE_RE.findall(page):
                self.post(f"/items/{item_id}/serve", {}, name="POST /items/[id]/serve",
                          headers={"referer": f"{self.host}/orders/{order_id}"})
            if SERVE_RE.findall(page) or LIVE_BADGE_RE.search(page):
                continue
            self.my_orders.remove(order_id)  # everything served: the counter takes it from here


class ChefUser(Staff):
    weight = 3

    def on_start(self) -> None:
        self.login(f"LT Chef {next(_chef_ids) % LT_STAFF + 1:02d}", LT_PIN)

    @task
    def cook(self) -> None:
        page = self.client.get("/kitchen", name="/kitchen").text
        cards = KITCHEN_CARD_RE.findall(page)
        started = 0
        for item_id, status in cards:
            if status == "preparing":
                action = "ready"
            elif started < 4:
                action, started = "start", started + 1
            else:
                continue
            # Like a tap on the tablet: HTMX request, answered with a redirect to the fresh card
            self.post(f"/kitchen/items/{item_id}/{action}", {}, name=f"POST /kitchen/items/[id]/{action}",
                      headers={"HX-Request": "true"})


class CounterUser(Staff):
    weight = 1

    def on_start(self) -> None:
        self.login(COUNTER_NAME, COUNTER_PIN)

    @task
    def bill_and_pay(self) -> None:
        occupied = BILLABLE_RE.findall(self.client.get("/counter", name="/counter").text)
        random.shuffle(occupied)
        for order_id in occupied[:5]:
            page = self.client.get(f"/counter/orders/{order_id}", name="/counter/orders/[id]").text
            if LIVE_BADGE_RE.search(page) or "badge st-served" not in page or "Generate bill" not in page:
                continue  # still cooking / not served yet
            version = VERSION_RE.search(page)
            if not version:
                continue
            resp = self.post(f"/counter/orders/{order_id}/bill", {"discount": "", "version": version.group(1)},
                             name="POST /counter/orders/[id]/bill", expect_prefix="/counter/bills/",
                             headers={"referer": f"{self.host}/counter/orders/{order_id}"})
            location = resp.headers.get("location", "")
            if location.startswith("/counter/bills/"):
                self.client.get(location, name="/counter/bills/[id]")
                self.post(f"{location}/pay", {"payment_mode": random.choice(["cash", "upi", "card"])},
                          name="POST /counter/bills/[id]/pay", expect_prefix="/counter/bills/")


@events.quitting.add_listener
def _report_targets(environment, **_kw) -> None:
    """Print p50/p95 per request type against the 300 ms p95 target."""
    stats = environment.stats
    print("\nTarget: p95 < 300 ms for page loads and POSTs")
    worst = 0
    for entry in sorted(stats.entries.values(), key=lambda e: e.name):
        if entry.name.startswith("/stream") or entry.method == "SSE":
            continue  # long-lived by design
        p50, p95 = entry.get_response_time_percentile(0.5), entry.get_response_time_percentile(0.95)
        worst = max(worst, p95)
        flag = "OK " if p95 < 300 else "SLOW"
        print(f"  {flag} {entry.method or '':4} {entry.name:42} n={entry.num_requests:6}  p50={p50:5.0f} ms  "
              f"p95={p95:5.0f} ms  failures={entry.num_failures}")
    total = stats.total
    print(f"  ALL requests: n={total.num_requests} failures={total.num_failures} "
          f"({total.fail_ratio:.2%})  worst p95={worst:.0f} ms")
    if worst >= 300 or total.num_failures:
        environment.process_exit_code = 1
