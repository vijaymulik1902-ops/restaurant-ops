# Restaurant Ops

A real-time operations system for a busy Indian restaurant: tables, orders, kitchen tickets,
billing with GST, day close, menu and cost management, expenses, a profit report and an
append-only audit log. Waiters use phones, chefs use a tablet, the counter uses a laptop, and
every screen updates live.

Built with FastAPI, SQLite, Jinja2, HTMX and Server-Sent Events. No JavaScript framework, no
build step, no Redis. One process, one database file.

## The business problem

A 20-150 table restaurant running on paper KOTs (kitchen order tickets) and a billing PC loses
money in ways that are hard to see:

- **Orders get lost or doubled.** A waiter taps "send" twice on bad Wi-Fi and the kitchen cooks
  two plates, or a ticket falls off the rail and a table waits 40 minutes.
- **Nobody knows what's happening now.** Waiters walk to the pass to check whether food is ready.
  The counter doesn't know which tables are about to ask for the bill.
- **Leakage.** Items served but never billed, unexplained discounts, cancellations after the
  food was cooked. Without a record there's no way to tell mistakes from theft.
- **No idea of profit.** Sales are known roughly, but not the cost of each dish, the margin per
  dish, or whether the month made money after rent and salaries.

This system tackles each one. Kitchen tickets are sent from the phone and can't be doubled.
Every screen updates within a second. Day close lists any item that was ordered but never paid.
Every discount, cancellation and price change is audited. The manager gets menu margins and net
profit on one page.

## What it does

| Role | Screen | What they do |
|---|---|---|
| Waiter (phone) | Floor | Tables in their section, colour by state, live timers; red when a table waits too long for its order or its food |
| | Order | Menu with search and +/- steppers, notes, "Send to kitchen", "Mark served" when ready (phone buzzes), cancel with a reason |
| Chef (tablet) | Kitchen | Tickets for their station, oldest first: tap to start, tap when ready. Red after 20 min. Turn dishes off when they run out |
| Counter (laptop) | Counter | Every table live; bill preview, discount, GST bill, payment (cash / UPI / card), printable 80 mm bill, day close |
| Manager | Everything above, plus | Sales and profit report with charts and CSV, expenses, menu (price, cost, margin, rename, archive), staff PINs, audit log, database backup download |

## Architecture

```
   phones / tablet / laptop (browser: HTML + HTMX + ~20 KB of plain JS)
        |  HTTPS: page loads, form POSTs             ^  SSE: small events
        v                                            |  ("table 12 changed", "item 88 ready")
 +--------------------------------------------------------------------------+
 |  Uvicorn, ONE worker                                                     |
 |                                                                          |
 |  routers/   thin: parse request -> call ONE service -> render/redirect   |
 |     |          role check (dependency) + CSRF on every route             |
 |     v                                                                    |
 |  services/  all business rules; each public function = one transaction  |
 |     |          returns (result, events) -- events published only after   |
 |     |          the commit, so a rollback never announces anything        |
 |     v                                                                    |
 |  SQLAlchemy -> SQLite (WAL)          events.py: in-memory broadcaster -->+ SSE
 |     write engine: BEGIN IMMEDIATE    (per-channel queues; slow clients    |
 |     read engine: never blocks        are dropped and reload on reconnect) |
 +--------------------------------------------------------------------------+
        |
   /var/data/restaurant.db  (+ daily backups in /var/data/backups, last 7 kept)
```

Every screen renders its full state on the server, then listens on `/stream`. An event names
the one thing that changed, and the browser re-fetches just that card. On every reconnect
(phone wakes up, Wi-Fi drops) the page reloads its lists from the database, so a missed event
can never leave a stale screen.

Event channels: `station:{tandoor|kitchen|bar}`, `section:{A..G}`, `waiter:{id}`, `counter`.

## Profit definitions

The sales report follows standard accounting (also written into `CLAUDE.md`, and pinned by tests):

| Line | Definition |
|---|---|
| Gross sales | Sum of bill subtotals, **paid bills only**, by payment time, business days 04:00-04:00 |
| Net sales | Gross sales - discounts. **GST is not revenue** (reported separately) |
| Cost of goods sold | Sum of qty x unit cost of every non-cancelled item on those bills (cost snapshotted when the KOT was sent) |
| Gross profit | Net sales - cost of goods sold |
| Operating expenses | Expenses dated in the period: salaries, rent, utilities, equipment, other |
| **Net profit** | **Gross profit - operating expenses** |
| Ingredient purchases | Info line only, **not deducted**: the food is already counted via cost of goods |

So a period with no expenses shows net profit = gross profit, never net sales (unless every dish
cost nothing). Menu-wise revenue is at list price; bill discounts appear as one separate line.

## Access control

Enforced on the server for every route (role dependency) and again inside the services, never
only by hiding buttons. Tests request every screen and action as every role and check the 403s.

| Capability | Waiter | Chef | Counter | Manager |
|---|---|---|---|---|
| Floor, open table, send KOT, mark served | yes | no | yes | yes |
| Kitchen board, start/ready items | no | yes | no | yes |
| Turn a dish on/off ("86" it) | no | yes (own station) | no | yes |
| Generate bill, take payment, discount up to 10% | no | no | yes | yes |
| Discount above 10% | no | no | no | yes |
| Cancel pending item | own section | no | yes | yes |
| Cancel preparing/ready item | no | no | no | yes |
| Cancel order with no KOTs | own order | no | yes | yes |
| Cancel order with KOTs | no | no | no | yes |
| Day close | no | no | yes | yes |
| See menu prices | yes | no | yes | yes |
| Menu prices/costs, margins, sales report, expenses, staff, audit, backups | no | no | no | yes |

**Cost privacy.** Dish costs and profit are visible to the manager only. Non-manager services
never even SELECT the cost columns: the kitchen and cancel paths defer the column with
`raiseload`, so touching it raises an error. SQL-watching tests prove none of these queries
contain it. Tests also check that no waiter, chef or counter page, and no SSE event, ever
contains a cost or margin.

## Design decisions

- **SQLite in WAL mode, one process.** A restaurant is one building with a few dozen devices,
  well within SQLite's range, and it removes a whole class of operational problems. Writers use
  `BEGIN IMMEDIATE`, so two waiters never deadlock: the second waits a few milliseconds. Readers
  never block writers. The load test below shows write p95 under 10 ms.
- **Server-Sent Events, not WebSockets.** Updates only flow server -> device. SSE is plain HTTP,
  reconnects by itself and passes through proxies. Events are tiny ("this table changed"); the
  browser fetches the fresh card. One Uvicorn worker, because the broadcaster lives in memory.
- **Append-only KOTs.** Sending more items never edits what was sent. Each batch is a new KOT
  with its own snapshot of dish name, price and cost, so renaming a dish or changing its price
  never alters an order already in the kitchen or a bill already printed.
- **Idempotent `kot_id`.** The server puts a fresh UUID in the order form each time it renders.
  A double tap or a retry on flaky Wi-Fi posts the same id; the database primary key makes the
  second one a no-op that returns the first KOT. (`crypto.randomUUID` isn't available over
  plain HTTP on a LAN, so the id is generated on the server.)
- **Optimistic locking on edits, not appends.** Orders carry a `version`. Cancelling an order
  and generating a bill send the version the screen showed, and are refused ("Order changed,
  reload") if a KOT arrived since. Adding items never checks it: two waiters adding to the same
  table must both succeed.
- **Money in integer paise.** No floats anywhere. GST is rounded half up to the paisa and
  checked by a database constraint (`total = subtotal - discount + gst`).
- **Audit log with triggers.** Cancellations, discounts, bills, payments, price/cost changes,
  dish changes, expenses, PIN changes, staff activation and backup downloads each write an audit
  row in the same transaction as the change. A rolled-back change leaves no row; tests force a
  crash after the audit write to prove it. SQLite triggers make the database itself refuse any
  UPDATE or DELETE on the table. PINs never reach it.
- **Business day, 04:00 to 04:00.** A table that orders at 23:30 and pays at 00:15 belongs to
  that evening. KOT numbers, day close, the sales report and expenses all use the same boundary.
- **Tested rules, thin routers.** Every rule lives in `app/services` and is unit tested; routers
  only parse, call one service and render. 348 tests run in about 9 seconds.

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m app.seed --reset                  # 20 tables, menu, staff (PINs printed)
python -m app.seed --reset --history 30     # optional: 30 days of realistic demo history
uvicorn app.main:app --reload               # phones on the same Wi-Fi: add --host 0.0.0.0
pytest -q
```

Open http://localhost:8000. The default seed uses simple demo PINs for local use: waiter
Rahul 1111, chef Suresh 8181, counter 9090, manager 9191. For anything public, use
`--random-pins` (see Deploy).

## Demo walkthrough (3 minutes)

Setup: `python -m app.seed --reset --history 30`, then `uvicorn app.main:app --host 0.0.0.0`.
Open it on a phone as **Rahul / 1111** (waiter), on a tablet or second browser as
**Suresh / 8181** (tandoor chef), and on the laptop as **Counter / 9090**. Keep a manager tab
(**Manager / 9191**) for the last three steps.

1. **Wi-Fi drop mid-order (30 s).** On the phone, seat table 1 and pick two naans and a dal.
   Turn Wi-Fi off: an orange "Reconnecting…" bar appears. Turn it back on: the bar goes, the
   screen reloads its state from the server, and your picks are still there.
2. **Double-tap "Send to kitchen" (20 s).** Tap it twice quickly. The kitchen tablet gets each
   item **once**, and the phone's list shows each item once. The page ignores the second tap, and
   a resent form (flaky Wi-Fi, back button) carries the same `kot_id`, which the server turns
   into a no-op. The load test double-taps 534 times and verifies zero duplicates.
3. **Kitchen to floor (20 s).** On the tablet, tap the naan to start it, then again when ready.
   The phone buzzes and beeps with "Table 1: Butter Naan READY"; tap "Mark served".
4. **Counter can't over-discount (20 s).** When everything is served, open table 1 on the counter
   and type a discount above 10% of the subtotal: it's refused ("needs a manager"). Exactly 10%
   works. Take payment by UPI; table 1 turns green on the phone at once.
5. **Waiter can't see cost (15 s).** On the phone, open `/menu` or `/reports/sales`: "Not
   allowed". The order screen shows prices only, never cost or margin.
6. **Day close (20 s).** On the counter, open Day close for yesterday: **Mismatches (0)**, cash /
   UPI / card totals, cancelled items with reasons.
7. **Audit log (20 s).** On the phone, seat table 2, send a lassi, and cancel it with the ✕ and
   "Customer changed mind". In the manager tab, **Audit** shows the `item cancel` row with who,
   when, and the reason. A cancel after food was ready would be highlighted in red.
8. **Sales report (30 s).** Manager, **Sales report** (opens on Last 30 days; tap This month to
   compare). It shows:
   - net sales, gross profit (margin), operating expenses, net profit (net margin)
   - the daily trend and peak-hours charts
   - the menu table: sort by profit, top 5 starred

   Net profit = gross profit - operating expenses; ingredient purchases are shown for info only.
   Short ranges that include a 1st (e.g. This month early in the month) carry a note: salaries and
   rent post on the 1st, so they understate profit.

## Load test

`loadtest/locustfile.py` simulates a busy service on 150 tables. Waiters seat tables, send 2-4
KOTs and double-tap one of them; chefs start and ready items; the counter bills and takes
payment. Every simulated device also holds a live `/stream` open. `loadtest/verify.py` then
checks the database:
- no duplicate KOTs
- no day-close mismatch on a paid order
- every bill reconciles with its order lines and GST
- bill numbers have no gaps
- every table's state matches its order

```bash
DB_PATH=loadtest.db python -m app.seed --reset --tables 150 --loadtest-staff 30
DB_PATH=loadtest.db LOGIN_IP_LIMIT=1000 BACKUPS_ENABLED=0 uvicorn app.main:app --port 8000 --workers 1
# second terminal
locust -f loadtest/locustfile.py --headless -u 40 -r 4 -t 5m --host http://localhost:8000 --csv loadtest/run
DB_PATH=loadtest.db python loadtest/verify.py
```

(`LOGIN_IP_LIMIT` is raised because all 40 simulated users log in from 127.0.0.1.)

**Results: 40 users, 5 minutes, 150 tables** (server and Locust on one laptop, macOS, Python
3.14). Target: p95 under 300 ms.

| Request | Count | p50 | p95 |
|---|---|---|---|
| GET /floor | 609 | 5 ms | 10 ms |
| GET /orders/[id] | 1,834 | 4 ms | 9 ms |
| GET /kitchen | 1,606 | 4 ms | 10 ms |
| GET /counter | 594 | 12 ms | 24 ms |
| POST send KOT | 1,834 | 4 ms | 8 ms |
| POST send KOT (double-tap) | 534 | 2 ms | 5 ms |
| POST start / ready item | 7,580 | 2-3 ms | 4-6 ms |
| POST mark served | 3,616 | 2 ms | 5 ms |
| POST generate bill / pay | 1,181 | 2-3 ms | 4-5 ms |
| POST login (bcrypt, by design) | 40 | 200 ms | 200 ms |

24,868 requests with 0 failures, 40 live streams held for the whole run, and every
`verify.py` check passed: 1,834 KOTs, none duplicated, and 589 bills that all reconcile.

## Deploy (Render)

`render.yaml` defines everything:
- one web service on the Starter plan
- a 1 GB persistent disk at `/var/data` holding the database and backups
- `--workers 1`, and proxy headers so the per-IP login limit sees real client IPs
- secure cookies, and a generated `SECRET_KEY`
- a `/health` check

The app refuses to start with `COOKIE_SECURE=true` and the dev secret key. After the first
deploy, open the service's Shell and run:

```bash
python -m app.seed --random-pins                 # PINs printed ONCE: 4 digits, 6 for the manager
python -m app.seed --history 30 --reset --random-pins   # or: a demo restaurant with 30 days of history
```

Running `python -m app.seed --random-pins` again later issues new PINs for everyone (for
example after a lost manager PIN). The manager can also change any PIN on the Staff page.

**Backups.** A copy is made with SQLite's online backup API at startup (if today's is missing)
and every 24 hours after, into `/var/data/backups`, keeping the last 7. The manager can
download a fresh copy any time from `/admin/backup`; downloads are audited.

## Security summary

- PINs are bcrypt-hashed and never stored or logged in plain text.
- Wrong PINs: 5 per staff member per 5 minutes, and at most 20 login attempts per IP per
  10 minutes (`LOGIN_IP_LIMIT` / `LOGIN_IP_WINDOW_SEC`).
- Signed session cookie holding the staff id and a PIN version: 14-hour shift, `SameSite=Lax`,
  `Secure` in production. Staff are reloaded on every request, so deactivating someone or
  changing their PIN logs out their existing sessions on the next tap, and closes their open
  live stream within 30 seconds.
- CSRF token on every POST (form field or `X-CSRF-Token` header from HTMX).
- Friendly pages for bad input, a busy database or unexpected errors; never a stack trace.

## Known limits

- **One process.** Live updates are broadcast from memory, so the app can't run more than one
  Uvicorn worker or instance. That's comfortable for one restaurant; a chain would need Redis
  pub/sub and Postgres.
- **No per-device logout.** Sessions live in the signed cookie. To end one person's sessions,
  change their PIN or deactivate them; there's no "log out that one phone".
- **Rate limits and lockouts are in memory** and reset on restart or deploy.
- **Shared Wi-Fi counts as one IP.** Staff behind the restaurant's network share the per-IP
  login limit. 20 per 10 minutes covers a shift change; raise `LOGIN_IP_LIMIT` for a larger
  team.
- **Some manager changes need a reload elsewhere.** New dishes and price changes reach open
  order screens live, but the manager's own menu edits don't live-update other managers'
  screens.
- **No GST invoice format.** The bill is a simple 80 mm receipt: no GSTIN, HSN codes, or
  CGST/SGST split.
- **No printer integration.** KOTs live on screen only; there's no thermal printer output.
- **Backups stay on the same disk.** Copy them off-site (for example a weekly manual download)
  for real disaster recovery.

## How AI tools were used

This project was built with **Claude Code** (Anthropic's coding agent) working in the
repository, directed and reviewed by the author across a series of rounds.

- **The author** wrote `CLAUDE.md`, which fixed the stack, the architecture rules, the access
  matrix and the report definitions. They also wrote the foundation (`config.py`, `db.py`,
  `models.py`, `seed.py`), and the requirements for each round. They reviewed each round's
  summary and the decisions it flagged, ran the app on real phones, and chose which trade-offs
  to accept.
- **Claude Code** wrote most of the services, routers, templates, JavaScript and tests. It
  flagged decisions for review, and stopped to ask before adding dependencies or changing
  foundation files beyond what was agreed.
- **Verification was never "looks right."** Every round ended with `pytest -q`. The live paths
  were also run against a real server:
  - SSE streams checked with curl
  - the charts rendered in headless Chrome at phone width
  - the load test plus `verify.py`

  Several real bugs were found this way before they shipped: a detached ORM read, huge-number
  and huge-PIN crashes, an open redirect, a flawed load-test client, and an audit test that
  proved nothing on an empty table.
