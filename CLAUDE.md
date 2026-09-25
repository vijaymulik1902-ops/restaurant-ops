# Restaurant Operations Manager

Real-time restaurant ops system: tables, orders, kitchen processing, billing, table availability.
Used live by ~7 waiters (phones), 2-3 chefs (tablet/phone), 1 counter (laptop). Must scale to 150+ tables.
Priority order: correctness > reliability > speed > looks. Keep it simple, no new frameworks.

## Stack (do not change)
- FastAPI, single Uvicorn worker (SSE broadcaster lives in process memory)
- SQLite (WAL) via SQLAlchemy 2.x, Jinja2 templates, HTMX, Pico CSS (local files in app/static)
- Server-Sent Events via sse-starlette for live updates
- Session auth: Starlette SessionMiddleware (signed cookie) + bcrypt PINs
- No React, no build step, no Redis, no Postgres, no Docker for local dev

## Foundation files: already written and tested, DO NOT modify without asking
- app/config.py, app/db.py, app/models.py, app/seed.py
- Use `write_session()` for any change, `read_session()` for reads. Never create sessions another way.
- Use `now()` from app.db for all timestamps.
- Money is integer paise everywhere. Format as rupees only in templates.
- Order has optimistic locking (`version`) for EDITS and CANCELS only (cancel_order, generate_bill):
  the form carries the version it showed and the service rejects a mismatch ("Order changed, reload").
  APPENDS never check it: send_kot is append-only, so two waiters adding to the same order both succeed
  (kot_id alone guards against duplicates). send_kot still bumps `version` so a pending cancel/bill
  made from a screen that hasn't seen the new items is rejected. Stale ORM writes raise StaleDataError.
- MenuItem.cost_paise = approx ingredient cost per plate. send_kot must snapshot it into
  OrderItem.unit_cost_paise, same as unit_price_paise.
- Expense table = money spent ("amount invested"), entered by manager only.

## Architecture rules
1. Routers are thin: parse request -> call ONE service function -> render/redirect. No SQL in routers.
2. ALL business rules live in app/services/*. Each public service function = one transaction
   (opens its own write_session). Raise `ServiceError(message)` (app/services/__init__.py) for rule violations.
3. Services return the list of events to publish. Routers publish events ONLY AFTER the transaction
   commits (never inside the session block). A rollback must never send an event.
4. State transitions are validated in services, never trusted from the client:
   - Table: available -> occupied -> billing -> available
   - Order: open -> billed -> paid ; open -> cancelled (only if no non-cancelled items)
   - Item: pending -> preparing -> ready -> served ; any non-served -> cancelled (reason required)
5. The client never sends prices or totals. Only ids, quantities, notes, discount, payment mode.
6. KOT ids are UUIDs generated SERVER-SIDE when rendering the order form and put in a hidden field
   (crypto.randomUUID is unavailable over plain HTTP on LAN). Same kot_id resubmitted = return the
   existing KOT, no error, no duplicate.
7. Every screen: on (re)connect, load full current state from DB via HTMX, then apply SSE events.
   SSE sends small events (one item/table), never whole boards.
8. Role checks via FastAPI dependency on every route. Waiter sees own section by default (toggle all).
   Chef sees own station. Counter/manager see everything.

## Access control matrix (enforced server-side on EVERY route, never only by hiding UI)
| Capability                                   | waiter | chef | counter | manager |
|----------------------------------------------|--------|------|---------|---------|
| Floor, open table, send KOT, mark served     |  yes   |  no  |   yes   |   yes   |
| Kitchen board, start/ready items             |  no    |  yes |   no    |   yes   |
| Toggle item availability (86 an item)        |  no    |  yes |   no    |   yes   |
| Generate bill, take payment                  |  no    |  no  |   yes   |   yes   |
| Discount up to 10%                           |  no    |  no  |   yes   |   yes   |
| Discount > 10%                               |  no    |  no  |   no    |   yes   |
| Cancel pending item (not started)            | own section | no |  yes  |   yes   |
| Cancel preparing/ready item                  |  no    |  no  |   no    |   yes   |
| Cancel order with no KOTs                    | own order | no  |   yes   |   yes   |
| Cancel order with KOTs                       |  no    |  no  |   no    |   yes   |
| Day close (cash/UPI/card totals, mismatches) |  no    |  no  |   yes   |   yes   |
| Menu prices: view                            |  yes   |  no  |   yes   |   yes   |
| Menu prices / costs: edit, add dishes        |  no    |  no  |   no    |   yes   |
| Item cost, cost of goods, profit, margins    |  no    |  no  |   no    |   yes   |
| Sales summary report, CSV export             |  no    |  no  |   no    |   yes   |
| Expenses: view, add, delete                  |  no    |  no  |   no    |   yes   |

Rules that keep cost data private and separate from prices:
- Billing and every non-manager service read ONLY price fields. They must never SELECT
  cost_paise / unit_cost_paise. Cost is read only inside manager-only report/menu services.
- Cost/profit/expense values never appear in SSE events, HTMX partials, bill print view,
  day close, or any template rendered for waiter/chef/counter.
- Price and cost are independent columns edited through separate form fields; editing one
  must never modify the other. Edits apply to future orders only (order lines hold snapshots).
- Every menu price/cost edit and every expense add/delete is logged in an audit table
  (who, when, old value, new value) — ask before adding the table.

## Sales report definitions (never mix these up)
- Only PAID bills count, filtered by Bill.paid_at within the selected range (inclusive business days).
- Gross sales = sum of bill subtotals. Net sales = gross sales - discounts. GST is NOT revenue.
- Cost of goods sold = sum(qty * unit_cost_paise) of non-cancelled items on paid bills.
- Gross profit = net sales - cost of goods sold (per dish: item revenue - item cost).
- Operating expenses = sum of Expense.amount_paise with spent_on in range, in categories
  salaries, rent, utilities, equipment, other (every category except "ingredients").
- Net profit = gross profit - operating expenses.
- Ingredient purchases (category "ingredients") are NOT deducted in profit: the food is already
  counted via cost of goods. Show them only as an info line
  ("Ingredient purchases ₹X (already counted via cost of goods)").
- Menu-wise revenue is at list price; bill discounts are shown as one separate line.

## Event channels
`station:{tandoor|kitchen|bar}`, `section:{A..G}`, `waiter:{staff_id}`, `counter`

## Conventions
- Type hints everywhere, small functions, docstrings on service functions.
- Mobile-first UI: big tap targets (min 48px), readable on a 5.5" phone, works in dark kitchens.
- Tests with pytest in tests/, each test uses a fresh temp DB (set DB_PATH env before importing app).
- After each task: run `pytest -q` and fix failures before reporting done.
- Run locally: `uvicorn app.main:app --reload` ; phones on same Wi-Fi: add `--host 0.0.0.0`.
- Ask before adding any dependency.
- CSRF: every POST form includes `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`;
  HTMX sends it as X-CSRF-Token via hx-headers on <body>. The app-wide dependency returns 403 otherwise.
- Path ids use `Id` from app.web (bounded int) so bad ids give a friendly message, never a 500.
