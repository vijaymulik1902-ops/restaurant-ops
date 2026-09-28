"""The floor "hold": a table with a booked reservation starting soon is shown as reserved.

Derived at read time from bookings, never stored in DiningTable.status. Used by the floor
(tables.list_tables), by open_table (walk-ins) and by the bookings service. One query per
call whatever the number of tables. Never reads phone numbers.
"""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking

HOLD_MINUTES = 45      # a booking holds its table from 45 minutes before it starts...
LATE_MINUTES = 15      # ...is "late" 15 minutes after its start if nobody has been seated...
MAX_DURATION_MIN = 300  # ...and holds until its slot ends (or it is seated/cancelled/no-show)
LOOKAHEAD = timedelta(hours=12)  # far enough to tell each card when its next change is due


def hold_window(starts_at: datetime, duration_min: int) -> tuple[datetime, datetime]:
    """[from, until) during which a still-booked reservation holds its table."""
    return starts_at - timedelta(minutes=HOLD_MINUTES), starts_at + timedelta(minutes=duration_min)


def is_late(status: str, starts_at: datetime, current: datetime) -> bool:
    """Booked (not seated) and 15+ minutes past its start."""
    return status == "booked" and current >= starts_at + timedelta(minutes=LATE_MINUTES)


def table_holds(s: Session, current: datetime, table_id: int | None = None) -> dict[int, dict]:
    """Per table id: {"hold": {...} or None, "refresh_in": seconds until the hold state changes}.

    Tables without upcoming bookings are absent from the dict.
    """
    stmt = (
        select(Booking.id, Booking.table_id, Booking.guest_name, Booking.party_size, Booking.starts_at,
               Booking.duration_min, Booking.status)
        .where(Booking.status == "booked", Booking.table_id.is_not(None),
               Booking.starts_at > current - timedelta(minutes=MAX_DURATION_MIN),
               Booking.starts_at <= current + LOOKAHEAD)
        .order_by(Booking.starts_at)
    )
    if table_id is not None:
        stmt = stmt.where(Booking.table_id == table_id)
    out: dict[int, dict] = {}
    for b in s.execute(stmt).all():
        start, until = hold_window(b.starts_at, b.duration_min)
        if current >= until:
            continue
        entry = out.setdefault(b.table_id, {"hold": None, "refresh_in": None})
        if start <= current and entry["hold"] is None:
            entry["hold"] = {"booking_id": b.id, "starts_at": b.starts_at, "guest_name": b.guest_name,
                             "party_size": b.party_size, "late": is_late(b.status, b.starts_at, current)}
        # next moment this card must re-render: hold starts, turns late, or ends
        for moment in (start, b.starts_at + timedelta(minutes=LATE_MINUTES), until):
            if moment > current:
                secs = int((moment - current).total_seconds()) + 1
                if entry["refresh_in"] is None or secs < entry["refresh_in"]:
                    entry["refresh_in"] = secs
                break
    return out


def active_hold(s: Session, table_id: int, current: datetime) -> dict | None:
    """The reservation holding this table right now, or None."""
    return table_holds(s, current, table_id).get(table_id, {}).get("hold")
