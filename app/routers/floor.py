"""Floor: table grid, seating guests."""
from fastapi import APIRouter, Depends, Form, Request

from app.auth import CurrentStaff, require_role
from app.services import tables
from app.web import FLOOR_ROLES, Id, partial, publish, render, see_other

router = APIRouter()
floor_staff = require_role(*FLOOR_ROLES)

MAX_GUESTS_BUTTONS = 12


def _section_for(staff: CurrentStaff, all_tables: bool) -> str | None:
    """Waiters see their own section unless they toggle "All tables"."""
    if staff.role == "waiter" and staff.section and not all_tables:
        return staff.section
    return None


@router.get("/floor")
def floor_page(request: Request, all: bool = False, staff: CurrentStaff = Depends(floor_staff)):
    section = _section_for(staff, all)
    return render(
        request, "floor.html", staff,
        tables=tables.list_tables(section), view="floor", all_tables=section is None,
        stream_url="/stream?all=1" if section is None else "/stream",
    )


@router.get("/floor/board")
def floor_board(request: Request, all: bool = False, view: str = "floor",
                staff: CurrentStaff = Depends(floor_staff)):
    """Full grid of cards, used after (re)connecting the live stream."""
    rows = tables.list_tables(_section_for(staff, all))
    return partial(request, "_table_cards.html", tables=rows, view=_view(view), staff=staff)


@router.get("/floor/tables/{table_id}/card")
def table_card(request: Request, table_id: Id, view: str = "floor",
               staff: CurrentStaff = Depends(floor_staff)):
    return partial(request, "_table_card.html", t=tables.get_table(table_id), view=_view(view), staff=staff)


@router.get("/floor/tables/{table_id}/open")
def open_table_page(request: Request, table_id: Id, staff: CurrentStaff = Depends(floor_staff)):
    t = tables.get_table(table_id)
    if t["status"] != "available" and t["order_id"]:
        return see_other(f"/orders/{t['order_id']}")
    return render(request, "open_table.html", staff, t=t, guest_options=range(1, MAX_GUESTS_BUTTONS + 1))


@router.post("/floor/tables/{table_id}/open")
def open_table_submit(table_id: Id, guest_count: int = Form(0), override_hold: bool = Form(False),
                      staff: CurrentStaff = Depends(floor_staff)):
    """Seat walk-ins. override_hold (counter/manager only, checked in the service) seats them at a
    table held for a reservation; it is audited as booking_override."""
    result, events = tables.open_table(table_id, staff.id, guest_count, override_hold=override_hold)
    publish(events)
    return see_other(f"/orders/{result['order_id']}")


def _view(view: str) -> str:
    return "counter" if view == "counter" else "floor"
