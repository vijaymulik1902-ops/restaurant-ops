"""Table bookings. View: waiter (today, read-only, no phones), counter, manager. Change: counter,
manager. No-show: manager. Chefs: no access. Rules live in app.services.bookings."""
from datetime import date, time

from fastapi import APIRouter, Depends, Form, Request

from app.auth import CurrentStaff, require_role
from app.services import ServiceError, bookings, business_day_of
from app.web import (BOOKING_EDIT_ROLES, BOOKING_VIEW_ROLES, Id, OptionalDate, OptionalId, back_url,
                     business_datetime, flash, partial, publish, render, see_other)

router = APIRouter()
viewers = require_role(*BOOKING_VIEW_ROLES)
editors = require_role(*BOOKING_EDIT_ROLES)
manager_only = require_role("manager")


def _day_for(staff: CurrentStaff, day: date | None) -> date | None:
    """Waiters only ever see today (None); others pick a day (default today)."""
    return None if staff.role == "waiter" else day


def _can_edit(staff: CurrentStaff) -> bool:
    return staff.role in BOOKING_EDIT_ROLES


@router.get("/bookings")
def bookings_page(request: Request, day: OptionalDate = None, staff: CurrentStaff = Depends(viewers)):
    view = bookings.day_view(_day_for(staff, day), include_phone=_can_edit(staff))
    return render(request, "bookings.html", staff, **view, can_edit=_can_edit(staff),
                  duration_default=bookings.DEFAULT_DURATION,
                  stream_url="/stream?all=1")


@router.get("/bookings/day")
def bookings_day(request: Request, day: OptionalDate = None, staff: CurrentStaff = Depends(viewers)):
    """The timeline + list, reloaded on live booking events and on reconnect."""
    view = bookings.day_view(_day_for(staff, day), include_phone=_can_edit(staff))
    return partial(request, "_bookings_day.html", **view, can_edit=_can_edit(staff), staff=staff)


@router.get("/bookings/upcoming")
def bookings_upcoming(request: Request, staff: CurrentStaff = Depends(editors)):
    """Counter screen / Manager Home: still-booked reservations in the next 3 hours (and late ones)."""
    return partial(request, "_upcoming.html", upcoming=bookings.upcoming(include_phone=True), staff=staff)


@router.get("/bookings/suggest")
def bookings_suggest(request: Request, day: OptionalDate = None, at: str = "", duration_min: str = "",
                     party_size: str = "", booking_id: OptionalId = None, table_id: OptionalId = None,
                     staff: CurrentStaff = Depends(editors)):
    """Free tables for the slot being typed into the booking form (HTMX, on every change)."""
    try:
        starts = business_datetime(day, time.fromisoformat(at)) if day else None
        tables = bookings.suggest_tables(starts, int(duration_min), int(party_size), booking_id) if starts else None
        hint = None if starts else "Pick a date and time to see free tables."
    except ValueError:
        tables, hint = None, "Fill in date, time, party size and duration to see free tables."
    except ServiceError as e:
        tables, hint = None, e.message
    return partial(request, "_booking_suggest.html", tables=tables, hint=hint, selected=table_id)


@router.post("/bookings")
def create_booking(request: Request, guest_name: str = Form(""), phone: str = Form(""),
                   party_size: int = Form(...), day: date = Form(...), at: time = Form(...),
                   duration_min: int = Form(bookings.DEFAULT_DURATION), table_id: OptionalId = Form(None),
                   note: str = Form(""), staff: CurrentStaff = Depends(editors)):
    result, events = bookings.create_booking(guest_name, phone, party_size, business_datetime(day, at),
                                             duration_min, table_id, note, staff.id)
    publish(events)
    flash(request, f"Booked {guest_name.strip()}: table {result['table_number']} at {result['starts_at']:%H:%M}",
          kind="ok")
    return see_other(f"/bookings?day={business_day_of(result['starts_at'])}")


@router.get("/bookings/{booking_id}")
def booking_page(request: Request, booking_id: Id, staff: CurrentStaff = Depends(editors)):
    b = bookings.get_booking(booking_id, include_phone=True)
    return render(request, "booking.html", staff, b=b, day=business_day_of(b["starts_at"]),
                  stream_url="/stream")


@router.post("/bookings/{booking_id}")
def update_booking(request: Request, booking_id: Id, guest_name: str = Form(""), phone: str = Form(""),
                   party_size: int = Form(...), day: date = Form(...), at: time = Form(...),
                   duration_min: int = Form(bookings.DEFAULT_DURATION), table_id: OptionalId = Form(None),
                   note: str = Form(""), staff: CurrentStaff = Depends(editors)):
    result, events = bookings.update_booking(booking_id, guest_name, phone, party_size,
                                             business_datetime(day, at), duration_min, table_id, note, staff.id)
    publish(events)
    flash(request, f"Booking saved: table {result['table_number']} at {result['starts_at']:%H:%M}", kind="ok")
    return see_other(f"/bookings?day={business_day_of(result['starts_at'])}")


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(request: Request, booking_id: Id, reason: str = Form(""), reason_choice: str = Form(""),
                   staff: CurrentStaff = Depends(editors)):
    result, events = bookings.cancel_booking(booking_id, reason.strip() or reason_choice, staff.id)
    publish(events)
    flash(request, f"Booking for {result['guest_name']} cancelled", kind="ok")
    return see_other(_after(request))


@router.post("/bookings/{booking_id}/no-show")
def no_show(request: Request, booking_id: Id, staff: CurrentStaff = Depends(manager_only)):
    result, events = bookings.mark_no_show(booking_id, staff.id)
    publish(events)
    flash(request, f"{result['guest_name']} marked as a no-show", kind="ok")
    return see_other(_after(request))


@router.post("/bookings/{booking_id}/seat")
def seat(request: Request, booking_id: Id, guest_count: OptionalId = Form(None),
         staff: CurrentStaff = Depends(editors)):
    result, events = bookings.seat_booking(booking_id, staff.id, guest_count)
    publish(events)
    flash(request, f"{result['guest_name']} seated at table {result['table_number']}", kind="ok")
    return see_other(_after(request))


def _after(request: Request) -> str:
    """Back to the list the action came from; the edit page itself goes back to /bookings."""
    url = back_url(request, fallback="/bookings")
    path = url.split("?", 1)[0]
    if path.startswith("/bookings/") or path.startswith("/floor/tables/"):
        return "/bookings" if path.startswith("/bookings/") else "/floor"
    return url
