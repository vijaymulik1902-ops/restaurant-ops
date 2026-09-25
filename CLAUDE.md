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
- Order has optimistic locking (`version`). Stale writes raise StaleDataError.

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

## Event channels
`station:{tandoor|kitchen|bar}`, `section:{A..G}`, `waiter:{staff_id}`, `counter`

## Conventions
- Type hints everywhere, small functions, docstrings on service functions.
- Mobile-first UI: big tap targets (min 48px), readable on a 5.5" phone, works in dark kitchens.
- Tests with pytest in tests/, each test uses a fresh temp DB (set DB_PATH env before importing app).
- After each task: run `pytest -q` and fix failures before reporting done.
- Run locally: `uvicorn app.main:app --reload` ; phones on same Wi-Fi: add `--host 0.0.0.0`.
- Ask before adding any dependency.
