"""Kitchen board: one station's live items, tap to start / tap when ready."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.auth import CurrentStaff, require_role
from app.models import STATIONS
from app.services import kitchen
from app.web import KITCHEN_ROLES, is_htmx, partial, publish, render, see_other

router = APIRouter()
kitchen_staff = require_role(*KITCHEN_ROLES)


def _station(staff: CurrentStaff, requested: str | None) -> str:
    """Chefs always get their own station; a manager may pick any."""
    if staff.role == "chef":
        if not staff.station:
            raise HTTPException(status_code=403, detail="No station assigned")
        return staff.station
    return requested if requested in STATIONS else STATIONS[0]


@router.get("/kitchen")
def kitchen_page(request: Request, station: str | None = None,
                 staff: CurrentStaff = Depends(kitchen_staff)):
    st = _station(staff, station)
    return render(request, "kitchen.html", staff, station=st, stations=STATIONS,
                  items=kitchen.live_items(st), stream_url="/stream")


@router.get("/kitchen/board")
def kitchen_board(request: Request, station: str | None = None,
                  staff: CurrentStaff = Depends(kitchen_staff)):
    st = _station(staff, station)
    return partial(request, "_kitchen_cards.html", items=kitchen.live_items(st), station=st)


@router.get("/kitchen/items/{item_id}/card")
def item_card(request: Request, item_id: int, station: str | None = None,
              staff: CurrentStaff = Depends(kitchen_staff)):
    """One card, or an empty body once the item has left the board (served/cancelled)."""
    st = _station(staff, station)
    rows = kitchen.live_items(st, item_id=item_id)
    if not rows:
        return HTMLResponse("")
    return partial(request, "_kitchen_card.html", i=rows[0], station=st)


def _after_tap(request: Request, item_id: int, station: str):
    # HTMX follows the redirect and swaps in the fresh card
    if is_htmx(request):
        return see_other(f"/kitchen/items/{item_id}/card?station={station}")
    return see_other(f"/kitchen?station={station}")


@router.post("/kitchen/items/{item_id}/start")
def start(request: Request, item_id: int, station: str | None = Form(None),
          staff: CurrentStaff = Depends(kitchen_staff)):
    st = _station(staff, station)
    _, events = kitchen.start_item(item_id, st)
    publish(events)
    return _after_tap(request, item_id, st)


@router.post("/kitchen/items/{item_id}/ready")
def ready(request: Request, item_id: int, station: str | None = Form(None),
          staff: CurrentStaff = Depends(kitchen_staff)):
    st = _station(staff, station)
    _, events = kitchen.ready_item(item_id, st)
    publish(events)
    return _after_tap(request, item_id, st)
