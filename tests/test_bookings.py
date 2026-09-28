"""Table bookings: slot rules, auto-assign, floor hold, seating, completion, no-show,
privacy of phone numbers, access, and a fixed query count on /bookings."""
import json
from datetime import datetime

import pytest
from sqlalchemy import event, select

from app.db import read_engine, read_session, write_engine, write_session
from app.models import AuditLog, Booking, DiningTable
from app.services import ServiceError, audit, billing, bookings, orders, tables
from app.services.ai_tools import run_tool
from conftest import new_kot_id, serve_all
from test_routes import login

PHONE = "+919812345678"
T19 = datetime(2026, 9, 25, 19, 0)  # the clock fixture starts at 2026-09-25 12:00


def _book(db, when=T19, party=2, table_index: int | None = 0, name="Asha Rao", duration=90, phone=PHONE,
          by="counter"):
    table_id = db["tables"][table_index] if table_index is not None else None
    made, events = bookings.create_booking(name, phone, party, when, duration, table_id, None, db["staff"][by])
    return made, events


def _add_tables(*caps_and_numbers: tuple[int, int], section: str = "B") -> dict[int, int]:
    """{table number: id} for extra tables."""
    with write_session() as s:
        rows = [DiningTable(number=n, capacity=c, section=section) for c, n in caps_and_numbers]
        s.add_all(rows)
        s.flush()
        return {r.number: r.id for r in rows}


# ---------- slot rules ----------

def test_overlap_on_the_same_table_is_rejected(db, clock):
    _book(db)                                                     # table 1, 19:00-20:30
    with pytest.raises(ServiceError, match="Table 1 is already booked 19:00–20:30"):
        _book(db, when=datetime(2026, 9, 25, 20, 0), name="Bina")
    with pytest.raises(ServiceError, match="already booked"):
        _book(db, when=datetime(2026, 9, 25, 18, 0), name="Bina")  # 18:00-19:30 overlaps too
    touching, _ = _book(db, when=datetime(2026, 9, 25, 20, 30), name="Bina")  # ends meet: fine
    assert touching["table_number"] == 1
    other, _ = _book(db, when=datetime(2026, 9, 25, 19, 30), table_index=1, name="Chetan")  # other table: fine
    assert other["table_number"] == 2


def test_cancelled_booking_frees_its_slot(db, clock):
    made, _ = _book(db)
    with pytest.raises(ServiceError, match="reason is required"):
        bookings.cancel_booking(made["booking_id"], "  ", db["staff"]["counter"])
    bookings.cancel_booking(made["booking_id"], "Guest cancelled", db["staff"]["counter"])
    again, _ = _book(db, name="Bina")
    assert again["table_number"] == 1


def test_capacity_is_checked(db, clock):
    with pytest.raises(ServiceError, match="Table 1 seats 4; the party is 6"):
        _book(db, party=6)
    with pytest.raises(ServiceError, match="No free table fits a party of 6 at 19:00"):
        _book(db, party=6, table_index=None)


def test_auto_assign_picks_the_smallest_free_table_that_fits(db, clock):
    ids = _add_tables((2, 4), (6, 5))
    two, _ = _book(db, party=2, table_index=None)
    assert two["table_number"] == 4                            # the 2-seater
    five, _ = _book(db, party=5, table_index=None, name="Big")
    assert five["table_number"] == 5
    three, _ = _book(db, party=3, table_index=None, name="C")
    assert three["table_number"] == 1                          # smallest 4-seater, lowest number
    three_b, _ = _book(db, party=3, table_index=None, name="D")
    assert three_b["table_number"] == 2                        # table 1 is taken at 19:00
    suggestions = bookings.suggest_tables(T19, 90, 2)
    assert [t["number"] for t in suggestions] == [3]           # 4 (2-seat), 1, 2 and 5 are booked then
    assert ids[4] not in [t["table_id"] for t in suggestions]


def test_past_times_and_bad_fields_are_rejected(db, clock):
    with pytest.raises(ServiceError, match="already passed"):
        _book(db, when=datetime(2026, 9, 25, 11, 0))
    _book(db, when=datetime(2026, 9, 25, 11, 57), name="Just now")  # typed a few minutes ago: fine
    for kwargs, message in [({"name": ""}, "Guest name is required"), ({"phone": "98-AB"}, "Phone"),
                            ({"phone": "+" + "9" * 15}, "max 15"), ({"party": 0}, "Party size"),
                            ({"duration": 20}, "Duration")]:
        with pytest.raises(ServiceError, match=message):
            _book(db, when=datetime(2026, 9, 25, 22, 0), **kwargs)


def test_update_keeps_its_table_and_checks_the_slot(db, clock):
    made, _ = _book(db)
    _book(db, when=datetime(2026, 9, 25, 21, 0), table_index=1, name="Bina")
    moved, _ = bookings.update_booking(made["booking_id"], "Asha Rao", PHONE, 3, datetime(2026, 9, 25, 19, 30),
                                       90, None, "window seat", db["staff"]["counter"])
    assert moved["table_number"] == 1                           # auto keeps its own table when it can
    with pytest.raises(ServiceError, match="Table 2 is already booked 21:00"):
        bookings.update_booking(made["booking_id"], "Asha Rao", PHONE, 3, datetime(2026, 9, 25, 20, 0), 90,
                                db["tables"][1], None, db["staff"]["counter"])


# ---------- floor hold ----------

def test_hold_window_44_vs_46_minutes(db, clock):
    _book(db)                                                   # table 1 at 19:00
    table_1 = db["tables"][0]
    clock.current = datetime(2026, 9, 25, 18, 14)               # 46 minutes before: not held yet
    row = tables.get_table(table_1)
    assert row["hold"] is None and 0 < row["refresh_in"] <= 61  # the card re-renders when the hold starts
    clock.current = datetime(2026, 9, 25, 18, 16)               # 44 minutes before: held
    hold = tables.get_table(table_1)["hold"]
    assert hold["guest_name"] == "Asha Rao" and hold["party_size"] == 2 and not hold["late"]
    assert "phone" not in hold
    clock.current = datetime(2026, 9, 25, 19, 15)
    assert tables.get_table(table_1)["hold"]["late"] is True    # 15 minutes past: late


def test_waiter_is_blocked_but_counter_can_override(db, clock):
    made, _ = _book(db)
    clock.current = datetime(2026, 9, 25, 18, 30)
    waiter, counter, table_1 = db["staff"]["waiter"], db["staff"]["counter"], db["tables"][0]
    with pytest.raises(ServiceError, match="Table 1 is reserved for 19:00 \\(Asha Rao, 2\\). Ask the counter"):
        tables.open_table(table_1, waiter, 2)
    with pytest.raises(ServiceError, match="Only the counter or a manager"):
        tables.open_table(table_1, waiter, 2, override_hold=True)
    with pytest.raises(ServiceError, match="Seat walk-in anyway"):
        tables.open_table(table_1, counter, 2)
    opened, _ = tables.open_table(table_1, counter, 2, override_hold=True)
    assert tables.get_table(table_1)["status"] == "occupied" and opened["order_id"]
    row = audit.list_audit(action="booking_override")["rows"][0]
    assert row["entity_id"] == made["booking_id"] and row["new"]["walk_in_guests"] == 2
    # a table held with no reservation nearby is untouched: table 2 opens normally for the waiter
    tables.open_table(db["tables"][1], waiter, 2)


def test_floor_card_shows_the_hold_and_guest_arrived_for_counter_only(db, clock):
    _book(db)
    clock.current = datetime(2026, 9, 25, 18, 30)
    counter_html = login("counter").get("/floor").text
    assert "Reserved" in counter_html and "19:00 · Asha Rao · 2" in counter_html
    assert "Guest arrived" in counter_html and "tcard st-available held" in counter_html
    waiter_html = login("waiter").get("/floor").text
    assert "19:00 · Asha Rao · 2" in waiter_html and "Guest arrived" not in waiter_html
    open_page = login("waiter").get(f"/floor/tables/{db['tables'][0]}/open").text
    assert "Walk-ins can&#39;t be seated here" in open_page or "Walk-ins can't be seated here" in open_page
    assert 'name="guest_count"' not in open_page                 # no walk-in buttons for the waiter
    counter_open = login("counter").get(f"/floor/tables/{db['tables'][0]}/open").text
    assert "Guest arrived: seat Asha Rao" in counter_open and 'name="override_hold" value="1"' in counter_open


# ---------- seat, complete, no-show ----------

def test_seat_opens_the_table_and_links_the_order(db, clock):
    made, _ = _book(db)
    clock.current = datetime(2026, 9, 25, 18, 55)
    seated, events = bookings.seat_booking(made["booking_id"], db["staff"]["counter"])
    row = tables.get_table(db["tables"][0])
    assert row["status"] == "occupied" and row["order_id"] == seated["order_id"] and row["guest_count"] == 2
    with read_session() as s:
        b = s.get(Booking, made["booking_id"])
        assert (b.status, b.order_id) == ("seated", seated["order_id"])
    assert {e.type for e in events} == {"table", "booking"}
    with pytest.raises(ServiceError, match="it can't be seated"):
        bookings.seat_booking(made["booking_id"], db["staff"]["counter"])


def test_seat_refuses_an_occupied_table_clearly(db, clock):
    made, _ = _book(db, when=datetime(2026, 9, 25, 13, 0))
    tables.open_table(db["tables"][0], db["staff"]["waiter"], 2)   # 12:00: walk-ins, before the hold
    clock.current = datetime(2026, 9, 25, 13, 0)
    with pytest.raises(ServiceError, match="Table 1 is occupied. Edit the booking to move it"):
        bookings.seat_booking(made["booking_id"], db["staff"]["counter"])


def test_booking_completes_when_its_order_is_paid(db, clock):
    made, _ = _book(db)
    clock.current = datetime(2026, 9, 25, 19, 0)
    seated, _ = bookings.seat_booking(made["booking_id"], db["staff"]["counter"])
    orders.send_kot(seated["order_id"], new_kot_id(), db["staff"]["waiter"], [(db["menu"]["dal"], 1, None)])
    serve_all(db, seated["order_id"])
    bill, _ = billing.generate_bill(seated["order_id"], 0, db["staff"]["counter"])
    _, events = billing.pay_bill(bill["bill_id"], "upi", db["staff"]["counter"])
    assert bookings.get_booking(made["booking_id"], include_phone=False)["status"] == "completed"
    assert any(e.type == "booking" and e.data["status"] == "completed" for e in events)


def test_booking_is_cancelled_not_completed_when_its_order_is_cancelled(db, clock):
    made, _ = _book(db)
    clock.current = datetime(2026, 9, 25, 19, 0)
    seated, _ = bookings.seat_booking(made["booking_id"], db["staff"]["counter"])
    _, events = orders.cancel_order(seated["order_id"], "Guests left", db["staff"]["counter"])
    assert bookings.get_booking(made["booking_id"], include_phone=False)["status"] == "cancelled"
    assert any(e.type == "booking" and e.data["status"] == "cancelled" for e in events)
    row = audit.list_audit(action="booking_cancelled")["rows"][0]
    assert (row["entity_id"], row["reason"], row["new"]) == (made["booking_id"], "order cancelled",
                                                             {"status": "cancelled"})
    assert bookings.summary(T19.date(), T19.date())["by_status"]["completed"] == 0


def test_no_show_is_manager_only_and_after_15_minutes(db, clock):
    made, _ = _book(db)
    clock.current = datetime(2026, 9, 25, 19, 14)
    with pytest.raises(ServiceError, match="Only a manager"):
        bookings.mark_no_show(made["booking_id"], db["staff"]["counter"])
    with pytest.raises(ServiceError, match="from 19:15"):
        bookings.mark_no_show(made["booking_id"], db["staff"]["manager"])
    clock.current = datetime(2026, 9, 25, 19, 15)
    bookings.mark_no_show(made["booking_id"], db["staff"]["manager"])
    assert bookings.get_booking(made["booking_id"], include_phone=False)["status"] == "no_show"
    assert tables.get_table(db["tables"][0])["hold"] is None      # the table is free for walk-ins again
    assert login("counter").post(f"/bookings/{made['booking_id']}/no-show").status_code == 403


# ---------- privacy ----------

def test_phone_never_reaches_waiters_events_ai_or_audit(db, clock):
    made, created = _book(db)
    clock.current = datetime(2026, 9, 25, 18, 30)
    b2, created2 = _book(db, when=datetime(2026, 9, 25, 21, 0), table_index=1, name="Bina")
    _, updated = bookings.update_booking(b2["booking_id"], "Bina", PHONE, 3, datetime(2026, 9, 25, 21, 0), 90,
                                         None, None, db["staff"]["counter"])
    _, cancelled = bookings.cancel_booking(b2["booking_id"], "Plans changed", db["staff"]["counter"])
    _, seated = bookings.seat_booking(made["booking_id"], db["staff"]["counter"])
    digits = PHONE.lstrip("+")
    for ev in created + created2 + updated + cancelled + seated:  # events: ids and status only
        payload = json.dumps(ev.data)
        assert digits not in payload and "Asha" not in payload and "Bina" not in payload

    waiter = login("waiter")
    for url in ("/bookings", "/bookings/day", "/floor", "/floor?all=1", f"/floor/tables/{db['tables'][1]}/open"):
        assert digits not in waiter.get(url).text, url
    assert digits in login("counter").get("/bookings").text      # the counter does see it

    for tool_result in (run_tool("bookings_summary", {"start": "2026-09-25", "end": "2026-09-25"}),):
        text = json.dumps(tool_result)
        assert digits not in text and "Asha" not in text and tool_result["bookings"] == 2
    with read_session() as s:
        audit_json = " ".join(f"{r.old_value} {r.new_value}" for r in s.scalars(select(AuditLog)))
    assert digits not in audit_json and "booking_created" not in audit_json  # actions aren't in the JSON
    with write_session() as s, pytest.raises(ValueError, match="phone"):
        audit.record(s, db["staff"]["manager"], audit.BOOKING_UPDATED, "booking", 1, new={"phone": PHONE})


# ---------- access ----------

def test_booking_routes_enforce_roles(db, clock):
    made, _ = _book(db)
    bid = made["booking_id"]
    chef = login("chef")
    for url in ("/bookings", "/bookings/day", "/bookings/upcoming", f"/bookings/{bid}"):
        assert chef.get(url).status_code == 403, url
    waiter = login("waiter")
    assert waiter.get("/bookings").status_code == 200
    for url in ("/bookings/upcoming", f"/bookings/{bid}", "/bookings/suggest"):
        assert waiter.get(url).status_code == 403, url
    form = {"guest_name": "X", "party_size": "2", "day": "2026-09-25", "at": "20:00", "duration_min": "90"}
    for client in (waiter, chef):
        assert client.post("/bookings", data=form).status_code == 403
        assert client.post(f"/bookings/{bid}/seat").status_code == 403
        assert client.post(f"/bookings/{bid}/cancel", data={"reason": "x"}).status_code == 403
    counter = login("counter")
    assert counter.post("/bookings", data=form).status_code == 303
    assert "Upcoming" not in waiter.get("/floor").text


def test_waiters_only_see_today(db, clock):
    _book(db, when=datetime(2026, 9, 26, 19, 0), name="Tomorrow Guest")
    _book(db, name="Tonight Guest")
    page = login("waiter").get("/bookings?day=2026-09-26").text
    assert "Tonight Guest" in page and "Tomorrow Guest" not in page and "read-only" in page
    assert "Tomorrow Guest" in login("counter").get("/bookings?day=2026-09-26").text


def test_new_booking_form_and_suggestions(db, clock):
    c = login("counter")
    page = c.get("/bookings").text
    assert 'id="sheet-new-booking"' in page and 'hx-get="/bookings/suggest"' in page
    assert 'value="12:15"' in page                               # next quarter hour, today
    html = c.get("/bookings/suggest?day=2026-09-25&at=19:00&duration_min=90&party_size=2").text
    assert "Auto: table 1 (4 seats, smallest free)" in html and "Table 3 · 4 seats" in html
    assert "Pick a date" in c.get("/bookings/suggest?day=&at=&duration_min=90&party_size=2").text
    resp = c.post("/bookings", data={"guest_name": "Meera", "phone": "+91 98123 45678", "party_size": "3",
                                     "day": "2026-09-25", "at": "20:00", "duration_min": "120", "note": "cake"})
    assert resp.status_code == 303 and resp.headers["location"] == "/bookings?day=2026-09-25"
    b = bookings.day_view(None, include_phone=True)["bookings"][0]
    assert (b["guest_name"], b["phone"], b["party_size"], b["duration_min"]) == ("Meera", "+919812345678", 3, 120)


def test_upcoming_on_counter_and_home(db, clock):
    _book(db, when=datetime(2026, 9, 25, 13, 0))
    _book(db, when=datetime(2026, 9, 25, 18, 0), table_index=1, name="Later")  # beyond 3 hours
    clock.current = datetime(2026, 9, 25, 13, 20)
    html = login("counter").get("/bookings/upcoming").text
    assert "Asha Rao" in html and "Later" not in html and "Late" in html and "Seat" in html
    assert 'hx-get="/bookings/upcoming"' in login("counter").get("/counter").text
    assert 'hx-get="/bookings/upcoming"' in login("manager").get("/home").text


# ---------- scale ----------

def _count_queries(fn) -> int:
    n = {"q": 0}

    def on_exec(*_a):
        n["q"] += 1

    for engine in (read_engine, write_engine):
        event.listen(engine, "before_cursor_execute", on_exec)
    try:
        fn()
    finally:
        for engine in (read_engine, write_engine):
            event.remove(engine, "before_cursor_execute", on_exec)
    return n["q"]


def test_bookings_page_query_count_is_constant(db, clock):
    c = login("counter")

    def grow_to(n_tables: int) -> None:
        with write_session() as s:
            have = len(s.scalars(select(DiningTable.id)).all())
            s.add_all(DiningTable(number=100 + i, capacity=4, section="C") for i in range(have, n_tables))
        with read_session() as s:
            ids = s.scalars(select(DiningTable.id).order_by(DiningTable.id)).all()
        for i, tid in enumerate(ids[::4]):  # a booking on every 4th table
            try:
                bookings.create_booking(f"Guest {tid}", PHONE, 2, datetime(2026, 9, 25, 19 + i % 3, 0), 90, tid,
                                        None, db["staff"]["counter"])
            except ServiceError:
                pass  # already booked on the previous round

    pages = ("/bookings", "/bookings/day", "/bookings/upcoming", "/floor", "/counter")
    grow_to(20)
    small = {url: _count_queries(lambda: c.get(url)) for url in pages}
    grow_to(150)
    big = {url: _count_queries(lambda: c.get(url)) for url in pages}
    assert small == big, (small, big)


# ---------- insights ----------

def test_booking_insight_cards(db, clock):
    from app.services.insights import insight_cards

    a, _ = _book(db, when=datetime(2026, 9, 25, 13, 0))
    b, _ = _book(db, when=datetime(2026, 9, 25, 13, 0), table_index=1, name="Bina")
    clock.current = datetime(2026, 9, 25, 13, 0)
    bookings.seat_booking(a["booking_id"], db["staff"]["counter"])
    tables.open_table(db["tables"][2], db["staff"]["waiter2"], 2)  # a walk-in
    clock.current = datetime(2026, 9, 25, 13, 20)
    bookings.mark_no_show(b["booking_id"], db["staff"]["manager"])
    cards = {c["key"]: c for c in insight_cards(T19.date(), T19.date())}
    assert cards["no_shows"]["figure"] == "50.0%"
    assert cards["no_shows"]["sentence"] == "50.0% of bookings were no-shows (1 of 2 bookings that were due)."
    assert cards["bookings_vs_walkins"]["sentence"] == ("1 of 2 table visits came from bookings (50.0%); "
                                                                "the rest (1 walk-in) came in without one.")


def test_timeline_is_12_to_24_and_widens_for_early_bookings(db, clock):
    assert bookings.day_view(None, include_phone=False)["hours"] == list(range(12, 24))
    _book(db, when=datetime(2026, 9, 25, 22, 0))
    assert bookings.day_view(None, include_phone=False)["hours"] == list(range(12, 24))
    clock.current = datetime(2026, 9, 25, 5, 0)
    _book(db, when=datetime(2026, 9, 25, 9, 30), table_index=1, name="Breakfast")
    view = bookings.day_view(None, include_phone=False)
    assert view["hours"] == list(range(9, 24))
    block = next(b for t in view["tables"] for b in t["bookings"] if b["guest_name"] == "Breakfast")
    assert block["left"] == pytest.approx(30 * 100 / (15 * 60), abs=0.01) and not block["outside"]


def test_edit_form_suggestions_keep_the_current_table(db, clock):
    made, _ = _book(db, table_index=1)
    html = login("counter").get("/bookings/suggest", params={
        "day": "2026-09-25", "at": "19:00", "duration_min": "90", "party_size": "2",
        "booking_id": made["booking_id"], "table_id": db["tables"][1]}).text
    assert f'<option value="{db["tables"][1]}" selected>Table 2' in html   # its own slot doesn't block it



# ---------- seed --demo-bookings ----------

def test_demo_bookings_for_today_are_idempotent(db):
    """Real time on purpose: the demo is relative to 'now'."""
    from app.history import DEMO_TAG, add_demo_bookings

    _add_tables((6, 4))                        # the birthday party needs a 6-seater
    first = add_demo_bookings()
    assert len(first) == 4 and not any("skipped" in line for line in first)
    view = {b["guest_name"]: b for b in bookings.upcoming(include_phone=False, hours=5)}
    assert set(view) == {"Rhea Kapoor", "Imran Shaikh", "Mehta family", "Joshi party"}
    assert view["Joshi party"]["late"] and not view["Rhea Kapoor"]["late"]
    assert view["Mehta family"]["note"] == "birthday" and view["Rhea Kapoor"]["note"] is None  # marker hidden
    with read_session() as s:
        assert all(DEMO_TAG in n for n in s.scalars(select(Booking.note)))               # but stored
    held = {t["hold"]["guest_name"] for t in tables.list_tables() if t["hold"]}
    assert {"Rhea Kapoor", "Joshi party"} <= held and "Imran Shaikh" not in held  # +30 min holds now; +2 h not yet
    again = add_demo_bookings()
    assert all("already booked, skipped" in line for line in again)
    with read_session() as s:
        assert len(s.scalars(select(Booking.id)).all()) == 4                        # no duplicates



def test_demo_marker_is_never_displayed(db):
    """Stored only as an internal marker: stripped from every screen, partial and service read,
    can't be typed in by staff, and survives an edit so the seeder still finds it."""
    from datetime import date, timedelta

    from app.history import DEMO_TAG, add_demo_bookings
    from app.services.ai_tools import run_tool

    _add_tables((6, 4))
    add_demo_bookings()
    with read_session() as s:
        ids = s.scalars(select(Booking.id).order_by(Booking.id)).all()
    counter, manager, waiter = login("counter"), login("manager"), login("waiter")
    pages = {counter: ["/bookings", "/bookings/day", "/bookings/upcoming", "/floor", "/counter"]
                      + [f"/bookings/{i}" for i in ids]
                      + [f"/floor/tables/{t['table_id']}/open" for t in tables.list_tables()],
             manager: ["/home", "/bookings", "/bookings/upcoming", "/insights", "/audit", "/reports/sales",
                       "/reports/sales.csv?preset=today"],
             waiter: ["/bookings", "/bookings/day", "/floor", "/floor?all=1"]}
    seen_birthday = False
    for client, urls in pages.items():
        for url in urls:
            resp = client.get(url)
            assert resp.status_code == 200, url
            assert DEMO_TAG not in resp.text and "[demo" not in resp.text, url
            seen_birthday |= "birthday" in resp.text
    assert seen_birthday  # the real note text is still shown
    reads = (bookings.day_view(None, include_phone=True)["bookings"] + bookings.upcoming(include_phone=True, hours=6)
             + [bookings.get_booking(i, include_phone=True) for i in ids])
    assert all(DEMO_TAG not in (b["note"] or "") for b in reads)
    today = date.today().isoformat()
    assert DEMO_TAG not in json.dumps(run_tool("bookings_summary", {"start": today, "end": today}))

    # staff can't type the marker; an edit keeps it stored (and hidden) so re-runs still skip
    b = bookings.get_booking(ids[1], include_phone=True)
    bookings.update_booking(ids[1], b["guest_name"], None, b["party_size"], b["starts_at"], b["duration_min"], None,
                            "window seat [demo]", db["staff"]["counter"])
    assert bookings.get_booking(ids[1], include_phone=False)["note"] == "window seat"
    with read_session() as s:
        assert s.get(Booking, ids[1]).note == f"window seat {DEMO_TAG}"
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    for typed, stored in (("[demo]", None), ("cake [demo] please", "cake please")):  # through the real form
        counter.post("/bookings", data={"guest_name": f"Typed {stored}", "party_size": "2", "day": tomorrow,
                                        "at": "20:00", "duration_min": "30", "note": typed})
        with read_session() as s:
            assert s.scalar(select(Booking.note).where(Booking.guest_name == f"Typed {stored}")) == stored
    assert all("already booked, skipped" in line for line in add_demo_bookings())
