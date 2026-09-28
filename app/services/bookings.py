"""Table bookings: create, change, cancel, no-show, seat, and the read views.

Rules (see CLAUDE.md):
- Every public write is one transaction and is audited. Audit rows never carry the phone.
- A booking occupies its table from starts_at to starts_at + duration_min. Two booked/seated
  bookings on one table never overlap. No table given -> the smallest free table that fits.
- Seating opens the table through tables.seat_guests() in the SAME transaction.
- When the linked order is paid the booking becomes completed; when that order is cancelled
  the booking is cancelled (reason "order cancelled", audited).
- Phone numbers are read only when include_phone=True (counter/manager screens). They never
  go into events, audit rows or AI tools.
"""
import re
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import now, read_session, write_session
from app.models import Booking, DiningTable, Order
from app.services import (Event, ServiceError, audit, business_day_bounds, business_day_of, clean_reason,
                          validate_range)
from app.services.holds import LATE_MINUTES, MAX_DURATION_MIN, is_late
from app.services.tables import check_guest_count, get_active_staff, seat_guests, table_events

ACTIVE = ("booked", "seated")          # these block their table's time slot
EDIT_ROLES = ("counter", "manager")
NAME_MAX, PHONE_MAX, NOTE_MAX = 60, 15, 120
MIN_PARTY, MAX_PARTY = 1, 50
MIN_DURATION, MAX_DURATION, DEFAULT_DURATION = 30, MAX_DURATION_MIN, 90
PAST_GRACE = timedelta(minutes=5)      # "now" typed into the form a minute ago is still fine
UPCOMING_HOURS = 3
TIMELINE_START_HOUR, TIMELINE_END_HOUR = 12, 24
_PHONE_RE = re.compile(r"\+?[0-9]+")
# Internal marker on bookings made by `seed --demo-bookings` (kept in the note column so re-runs
# can find them). NEVER displayed: every read strips it, and nobody can type it in.
DEMO_TAG = "[demo]"


NOTE_INPUT_MAX = NOTE_MAX - len(DEMO_TAG) - 1  # room for the marker inside the 120-character column


def _without_tag(note: str | None) -> str:
    return " ".join((note or "").replace(DEMO_TAG, " ").split())


def display_note(note: str | None) -> str | None:
    """A booking note as staff see it: the internal demo marker removed."""
    return _without_tag(note) or None


def _with_tag(note: str | None) -> str:
    return f"{_without_tag(note)} {DEMO_TAG}".strip()


# ---------- validation ----------

def _clean(guest_name: str, phone: str | None, party_size: int, starts_at: datetime,
           duration_min: int, note: str | None) -> dict:
    name = (guest_name or "").strip()
    if not name:
        raise ServiceError("Guest name is required")
    if len(name) > NAME_MAX:
        raise ServiceError(f"Guest name is too long (max {NAME_MAX} characters)")
    phone = re.sub(r"[\s-]", "", phone or "")
    if phone and (not _PHONE_RE.fullmatch(phone) or len(phone) > PHONE_MAX):
        raise ServiceError(f"Phone: digits and a leading + only (max {PHONE_MAX})")
    _check_int(party_size, MIN_PARTY, MAX_PARTY, "Party size")
    _check_int(duration_min, MIN_DURATION, MAX_DURATION, "Duration (minutes)")
    if not isinstance(starts_at, datetime):
        raise ServiceError("Pick a date and time")
    note = _without_tag(note)  # staff can't type the internal demo marker
    if len(note) > NOTE_INPUT_MAX:
        raise ServiceError(f"Note is too long (max {NOTE_INPUT_MAX} characters)")
    return {"guest_name": name, "phone": phone or None, "party_size": party_size,
            "starts_at": starts_at.replace(second=0, microsecond=0), "duration_min": duration_min,
            "note": note or None}


def _check_int(value, lo: int, hi: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ServiceError(f"{label} must be between {lo} and {hi}")


def _editor(s: Session, staff_id: int, roles: tuple = EDIT_ROLES):
    staff = get_active_staff(s, staff_id)
    if staff.role not in roles:
        raise ServiceError("Only the counter or a manager can change bookings"
                           if roles == EDIT_ROLES else "Only a manager can do that")
    return staff


def _end(b) -> datetime:
    return b.starts_at + timedelta(minutes=b.duration_min)


# ---------- table choice ----------

def _conflicts(s: Session, start: datetime, end: datetime, exclude_id: int | None = None,
               table_id: int | None = None) -> list:
    """Booked/seated bookings overlapping [start, end) (on one table, or on any table)."""
    stmt = (select(Booking.id, Booking.table_id, Booking.starts_at, Booking.duration_min)
            .where(Booking.status.in_(ACTIVE), Booking.table_id.is_not(None), Booking.starts_at < end,
                   Booking.starts_at > start - timedelta(minutes=MAX_DURATION)))
    if exclude_id is not None:
        stmt = stmt.where(Booking.id != exclude_id)
    if table_id is not None:
        stmt = stmt.where(Booking.table_id == table_id)
    return [b for b in s.execute(stmt).all() if _end(b) > start]


def _free_tables(s: Session, start: datetime, end: datetime, party: int,
                 exclude_id: int | None = None) -> list:
    """Tables that seat the party and have no overlapping booking: smallest first. Two queries."""
    busy = {b.table_id for b in _conflicts(s, start, end, exclude_id)}
    fitting = s.execute(select(DiningTable.id, DiningTable.number, DiningTable.capacity, DiningTable.section)
                        .where(DiningTable.capacity >= party)
                        .order_by(DiningTable.capacity, DiningTable.number)).all()
    return [t for t in fitting if t.id not in busy]


def _choose_table(s: Session, table_id: int | None, f: dict, exclude_id: int | None = None,
                  keep_table_id: int | None = None) -> DiningTable:
    start = f["starts_at"]
    end = start + timedelta(minutes=f["duration_min"])
    if table_id:
        table = s.get(DiningTable, table_id)
        if table is None:
            raise ServiceError("Table not found")
        if f["party_size"] > table.capacity:
            raise ServiceError(f"Table {table.number} seats {table.capacity}; the party is {f['party_size']}")
        clash = _conflicts(s, start, end, exclude_id, table.id)
        if clash:
            c = clash[0]
            raise ServiceError(f"Table {table.number} is already booked {c.starts_at:%H:%M}–{_end(c):%H:%M}")
        return table
    free = _free_tables(s, start, end, f["party_size"], exclude_id)
    if not free:
        raise ServiceError(f"No free table fits a party of {f['party_size']} at {start:%H:%M}")
    chosen = next((t for t in free if t.id == keep_table_id), free[0])  # an edit keeps its table if it can
    return s.get(DiningTable, chosen.id)


def suggest_tables(starts_at: datetime, duration_min: int, party_size: int,
                   exclude_booking_id: int | None = None) -> list[dict]:
    """Free tables that fit the party for that slot, smallest first."""
    _check_int(party_size, MIN_PARTY, MAX_PARTY, "Party size")
    _check_int(duration_min, MIN_DURATION, MAX_DURATION, "Duration (minutes)")
    end = starts_at + timedelta(minutes=duration_min)
    with read_session() as s:
        rows = _free_tables(s, starts_at, end, party_size, exclude_booking_id)
    return [{"table_id": t.id, "number": t.number, "capacity": t.capacity, "section": t.section} for t in rows]


# ---------- events / audit shapes (never the phone) ----------

def booking_events(b: Booking, *tables: DiningTable | None) -> list[Event]:
    """Small live updates for the counter and each affected section. Ids and status only."""
    data = {"booking_id": b.id, "table_id": b.table_id, "status": b.status}
    out = [Event("counter", "booking", data)]
    for sec in dict.fromkeys(t.section for t in tables if t is not None):
        out.append(Event(f"section:{sec}", "booking", data))
    return out


def _audit_view(b: Booking, table: DiningTable | None) -> dict:
    return {"guest_name": b.guest_name, "party_size": b.party_size, "starts_at": f"{b.starts_at:%Y-%m-%d %H:%M}",
            "duration_min": b.duration_min, "table_number": table.number if table else None,
            "status": b.status}


def _load(s: Session, booking_id: int) -> Booking:
    b = s.get(Booking, booking_id)
    if b is None:
        raise ServiceError("Booking not found")
    return b


def _require_booked(b: Booking, doing: str) -> None:
    if b.status != "booked":
        raise ServiceError(f"This booking is {b.status.replace('_', '-')}, it can't be {doing}")


# ---------- writes ----------

def create_booking(guest_name: str, phone: str | None, party_size: int, starts_at: datetime,
                   duration_min: int = DEFAULT_DURATION, table_id: int | None = None, note: str | None = None,
                   by_staff_id: int = 0, demo: bool = False) -> tuple[dict, list[Event]]:
    """Reserve a table (counter/manager). No table given: smallest free table that fits.
    demo=True (seed --demo-bookings only, never from a form) stores the hidden demo marker."""
    f = _clean(guest_name, phone, party_size, starts_at, duration_min, note)
    if demo:
        f["note"] = _with_tag(f["note"])
    with write_session() as s:
        _editor(s, by_staff_id)
        ts = now()
        if f["starts_at"] < ts - PAST_GRACE:
            raise ServiceError("That time has already passed")
        table = _choose_table(s, table_id, f)
        b = Booking(**f, table_id=table.id, status="booked", created_by=by_staff_id, created_at=ts, updated_at=ts)
        s.add(b)
        s.flush()
        audit.record(s, by_staff_id, audit.BOOKING_CREATED, "booking", b.id, new=_audit_view(b, table))
        result = {"booking_id": b.id, "table_number": table.number, "starts_at": b.starts_at}
        events = booking_events(b, table)
    return result, events


def update_booking(booking_id: int, guest_name: str, phone: str | None, party_size: int, starts_at: datetime,
                   duration_min: int, table_id: int | None, note: str | None,
                   by_staff_id: int) -> tuple[dict, list[Event]]:
    """Change a booked reservation. table_id None keeps its table if it still fits, else the
    smallest free one. A new time must not be in the past."""
    f = _clean(guest_name, phone, party_size, starts_at, duration_min, note)
    with write_session() as s:
        _editor(s, by_staff_id)
        b = _load(s, booking_id)
        _require_booked(b, "changed")
        ts = now()
        if f["starts_at"] != b.starts_at and f["starts_at"] < ts - PAST_GRACE:
            raise ServiceError("That time has already passed")
        if b.note and DEMO_TAG in b.note:  # an edited demo booking stays findable by the seeder
            f["note"] = _with_tag(f["note"])
        old_table = s.get(DiningTable, b.table_id) if b.table_id else None
        old = _audit_view(b, old_table)
        table = _choose_table(s, table_id, f, exclude_id=b.id, keep_table_id=b.table_id)
        for k, v in f.items():
            setattr(b, k, v)
        b.table_id, b.updated_at = table.id, ts
        audit.record(s, by_staff_id, audit.BOOKING_UPDATED, "booking", b.id, old=old, new=_audit_view(b, table))
        result = {"booking_id": b.id, "table_number": table.number, "starts_at": b.starts_at}
        events = booking_events(b, old_table, table)
    return result, events


def cancel_booking(booking_id: int, reason: str, by_staff_id: int) -> tuple[dict, list[Event]]:
    """Cancel a booked reservation (counter/manager, reason required)."""
    reason = clean_reason(reason)
    with write_session() as s:
        _editor(s, by_staff_id)
        b = _load(s, booking_id)
        _require_booked(b, "cancelled")
        table = s.get(DiningTable, b.table_id) if b.table_id else None
        old = _audit_view(b, table)
        b.status, b.updated_at = "cancelled", now()
        audit.record(s, by_staff_id, audit.BOOKING_CANCELLED, "booking", b.id, old=old,
                     new={"status": "cancelled"}, reason=reason)
        result = {"booking_id": b.id, "guest_name": b.guest_name}
        events = booking_events(b, table)
    return result, events


def mark_no_show(booking_id: int, by_staff_id: int) -> tuple[dict, list[Event]]:
    """Manager only, and only from 15 minutes after the booking time."""
    with write_session() as s:
        _editor(s, by_staff_id, roles=("manager",))
        b = _load(s, booking_id)
        _require_booked(b, "marked as a no-show")
        ts = now()
        allowed_from = b.starts_at + timedelta(minutes=LATE_MINUTES)
        if ts < allowed_from:
            raise ServiceError(f"A no-show can be marked from {allowed_from:%H:%M} "
                               f"({LATE_MINUTES} minutes after the booking time)")
        table = s.get(DiningTable, b.table_id) if b.table_id else None
        old = _audit_view(b, table)
        b.status, b.updated_at = "no_show", ts
        audit.record(s, by_staff_id, audit.BOOKING_NO_SHOW, "booking", b.id, old=old, new={"status": "no_show"})
        result = {"booking_id": b.id, "guest_name": b.guest_name}
        events = booking_events(b, table)
    return result, events


def seat_booking(booking_id: int, by_staff_id: int, guest_count: int | None = None) -> tuple[dict, list[Event]]:
    """Guests arrived: open their table (same transaction), mark seated, link the order."""
    with write_session() as s:
        staff = _editor(s, by_staff_id)
        b = _load(s, booking_id)
        _require_booked(b, "seated")
        ts = now()
        if business_day_of(b.starts_at) != business_day_of(ts):
            raise ServiceError(f"This booking is for {b.starts_at:%a %d %b}, not today")
        table = s.get(DiningTable, b.table_id) if b.table_id else None
        if table is None:
            raise ServiceError("This booking has no table yet. Edit it to pick one")
        if table.status != "available":
            raise ServiceError(f"Table {table.number} is {table.status}. Edit the booking to move it "
                               f"to a free table, or seat them when it is free")
        guests = guest_count or b.party_size
        check_guest_count(guests)
        order = seat_guests(s, table, staff.id, guests, ts)
        b.status, b.order_id, b.updated_at = "seated", order.id, ts
        audit.record(s, staff.id, audit.BOOKING_SEATED, "booking", b.id,
                     old={"status": "booked"},
                     new={"status": "seated", "table_number": table.number, "order_id": order.id, "guests": guests})
        result = {"booking_id": b.id, "order_id": order.id, "table_number": table.number,
                  "guest_name": b.guest_name}
        events = table_events(table, order.id) + booking_events(b, table)
    return result, events


def _seated_for_order(s: Session, order_id: int) -> Booking | None:
    return s.scalar(select(Booking).where(Booking.order_id == order_id, Booking.status == "seated"))


def complete_for_order(s: Session, order_id: int, ts: datetime) -> list[Event]:
    """Inside billing.pay_bill: the guests were served and paid, so their booking is completed."""
    b = _seated_for_order(s, order_id)
    if b is None:
        return []
    b.status, b.updated_at = "completed", ts
    table = s.get(DiningTable, b.table_id) if b.table_id else None
    return booking_events(b, table)


ORDER_CANCELLED_REASON = "order cancelled"


def cancel_for_order(s: Session, order_id: int, ts: datetime, by_staff_id: int) -> list[Event]:
    """Inside orders.cancel_order: the visit never reached a paid bill, so the booking is
    cancelled (not completed), audited with reason "order cancelled"."""
    b = _seated_for_order(s, order_id)
    if b is None:
        return []
    table = s.get(DiningTable, b.table_id) if b.table_id else None
    old = _audit_view(b, table)
    b.status, b.updated_at = "cancelled", ts
    audit.record(s, by_staff_id, audit.BOOKING_CANCELLED, "booking", b.id, old=old,
                 new={"status": "cancelled"}, reason=ORDER_CANCELLED_REASON)
    return booking_events(b, table)


# ---------- reads ----------

def _columns(include_phone: bool) -> list:
    cols = [Booking.id, Booking.guest_name, Booking.party_size, Booking.starts_at, Booking.duration_min,
            Booking.table_id, Booking.status, Booking.note, Booking.order_id,
            DiningTable.number.label("table_number"), DiningTable.section, DiningTable.capacity]
    if include_phone:  # waiter screens never even SELECT the phone column
        cols.append(Booking.phone)
    return cols


def _row(r, current: datetime, include_phone: bool) -> dict:
    ends = r.starts_at + timedelta(minutes=r.duration_min)
    no_show_from = r.starts_at + timedelta(minutes=LATE_MINUTES)
    row = {"booking_id": r.id, "guest_name": r.guest_name, "party_size": r.party_size,
           "starts_at": r.starts_at, "ends_at": ends, "duration_min": r.duration_min,
           "table_id": r.table_id, "table_number": r.table_number, "section": r.section,
           "capacity": r.capacity, "status": r.status, "note": display_note(r.note), "order_id": r.order_id,
           "late": is_late(r.status, r.starts_at, current), "no_show_from": no_show_from,
           "can_no_show": r.status == "booked" and current >= no_show_from,
           "can_seat": r.status == "booked" and business_day_of(r.starts_at) == business_day_of(current)}
    if include_phone:
        row["phone"] = r.phone
    return row


def _timeline_hours(bookings: list[dict], day: date) -> tuple[int, int]:
    """12:00-24:00 by default, widened to whole hours covering the day's bookings (within the
    business day, 04:00 to 04:00)."""
    base = datetime.combine(day, datetime.min.time())
    lo, hi = TIMELINE_START_HOUR, TIMELINE_END_HOUR
    for b in bookings:
        if b["status"] == "cancelled":
            continue
        start_h = int((b["starts_at"] - base).total_seconds() // 3600)
        end_h = -int(-(b["ends_at"] - base).total_seconds() // 3600)  # ceiling
        lo, hi = min(lo, start_h), max(hi, end_h)
    first, last = business_day_bounds(day)
    return max(lo, first.hour), min(hi, 24 + first.hour)


def _timeline_spot(b: dict, day: date, hours: tuple[int, int]) -> dict:
    """left/width in % of the laptop timeline; outside=True if it can't be drawn."""
    lo = datetime.combine(day, datetime.min.time()) + timedelta(hours=hours[0])
    span = (hours[1] - hours[0]) * 60
    start = (b["starts_at"] - lo).total_seconds() / 60
    end = (b["ends_at"] - lo).total_seconds() / 60
    if end <= 0 or start >= span:
        return {"outside": True, "left": 0, "width": 0}
    a, z = max(0.0, start), min(float(span), end)
    return {"outside": False, "left": round(a * 100 / span, 3), "width": round((z - a) * 100 / span, 3)}


def day_view(day: date | None, include_phone: bool) -> dict:
    """/bookings for one business day (None = today): every booking (by time), every table
    (timeline rows) and counts. Fixed number of queries whatever the number of tables or bookings."""
    current = now()
    today = business_day_of(current)
    day = day or today
    lo, hi = business_day_bounds(day)
    with read_session() as s:
        rows = s.execute(select(*_columns(include_phone))
                         .outerjoin(DiningTable, DiningTable.id == Booking.table_id)
                         .where(Booking.starts_at >= lo, Booking.starts_at < hi)
                         .order_by(Booking.starts_at, Booking.id)).all()
        tables = s.execute(select(DiningTable.id, DiningTable.number, DiningTable.capacity, DiningTable.section)
                           .order_by(DiningTable.number)).all()
    bookings = [_row(r, current, include_phone) for r in rows]
    hours = _timeline_hours(bookings, day)
    by_table: dict[int, list] = {}
    outside = []
    for b in bookings:
        if b["status"] == "cancelled":
            continue
        b.update(_timeline_spot(b, day, hours))
        if b["outside"] or b["table_id"] is None:
            outside.append(b)
        else:
            by_table.setdefault(b["table_id"], []).append(b)
    counts = {st: sum(1 for b in bookings if b["status"] == st)
              for st in ("booked", "seated", "completed", "cancelled", "no_show")}
    counts["late"] = sum(1 for b in bookings if b["late"])
    counts["covers"] = sum(b["party_size"] for b in bookings if b["status"] in ("booked", "seated", "completed"))
    now_spot = _timeline_spot({"starts_at": current, "ends_at": current + timedelta(minutes=1)}, day, hours)
    return {"day": day, "is_today": day == today, "default_time": _default_time(day, today, current),
            "bookings": bookings,
            "tables": [{"table_id": t.id, "number": t.number, "capacity": t.capacity, "section": t.section,
                        "bookings": by_table.get(t.id, [])} for t in tables],
            "outside": outside, "counts": counts,
            "hours": list(range(*hours)),
            "now_left": None if now_spot["outside"] else now_spot["left"]}


def _default_time(day: date, today: date, current: datetime) -> str:
    """The New booking form starts at the next quarter hour today, 19:00 on other days."""
    if day != today:
        return "19:00"
    nxt = current.replace(second=0, microsecond=0) + timedelta(minutes=15 - current.minute % 15)
    return f"{nxt:%H:%M}"


def upcoming(include_phone: bool, hours: int = UPCOMING_HOURS) -> list[dict]:
    """Still-booked reservations starting in the next `hours` (plus late ones still inside
    their slot), soonest first. One query."""
    current = now()
    with read_session() as s:
        rows = s.execute(select(*_columns(include_phone))
                         .outerjoin(DiningTable, DiningTable.id == Booking.table_id)
                         .where(Booking.status == "booked",
                                Booking.starts_at > current - timedelta(minutes=MAX_DURATION),
                                Booking.starts_at <= current + timedelta(hours=hours))
                         .order_by(Booking.starts_at, Booking.id)).all()
    return [_row(r, current, include_phone) for r in rows
            if r.starts_at + timedelta(minutes=r.duration_min) > current]


def get_booking(booking_id: int, include_phone: bool) -> dict:
    """One booking for the edit page."""
    current = now()
    with read_session() as s:
        r = s.execute(select(*_columns(include_phone))
                      .outerjoin(DiningTable, DiningTable.id == Booking.table_id)
                      .where(Booking.id == booking_id)).first()
    if r is None:
        raise ServiceError("Booking not found")
    return _row(r, current, include_phone)


def summary(start: date, end: date) -> dict:
    """Counts only (no names, no phones): bookings by status, no-show rate, and table visits
    from bookings vs walk-ins, for business days start..end."""
    validate_range(start, end)
    lo, hi = business_day_bounds(start)[0], business_day_bounds(end)[1]
    with read_session() as s:
        by_status = dict(s.execute(select(Booking.status, func.count(Booking.id))
                                   .where(Booking.starts_at >= lo, Booking.starts_at < hi)
                                   .group_by(Booking.status)).all())
        visits = s.scalar(select(func.count(Order.id))
                          .where(Order.created_at >= lo, Order.created_at < hi, Order.status != "cancelled"))
        from_bookings = s.scalar(select(func.count(Order.id)).join(Booking, Booking.order_id == Order.id)
                                 .where(Order.created_at >= lo, Order.created_at < hi, Order.status != "cancelled"))
    counts = {st: int(by_status.get(st, 0)) for st in ("booked", "seated", "completed", "cancelled", "no_show")}
    arrived = counts["seated"] + counts["completed"]
    due = arrived + counts["no_show"]
    return {"range": {"start": start.isoformat(), "end": end.isoformat()},
            "bookings": sum(counts.values()), "by_status": counts, "arrived": arrived,
            "no_shows": counts["no_show"],
            "no_show_rate_percent": round(counts["no_show"] * 100 / due, 1) if due else None,
            "table_visits": int(visits), "visits_from_bookings": int(from_bookings),
            "walk_ins": int(visits) - int(from_bookings),
            "booked_share_percent": round(from_bookings * 100 / visits, 1) if visits else None}
