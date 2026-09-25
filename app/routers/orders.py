"""Waiter order screen: send KOTs, mark items served, cancel the order."""
import uuid

from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool

from app.auth import CurrentStaff, require_role
from app.services import ServiceError, kitchen, orders
from app.web import FLOOR_ROLES, back_url, partial, publish, render, see_other

router = APIRouter()
floor_staff = require_role(*FLOOR_ROLES)


@router.get("/orders/{order_id}")
def order_page(request: Request, order_id: int, staff: CurrentStaff = Depends(floor_staff)):
    screen = orders.order_screen(order_id, staff.id, staff.role)
    # A fresh KOT id per render: a double-tap or retry of THIS form reuses it
    return render(request, "order.html", staff, **screen, kot_id=str(uuid.uuid4()))


@router.get("/orders/{order_id}/items")
def order_items(request: Request, order_id: int, staff: CurrentStaff = Depends(floor_staff)):
    return partial(request, "_order_items.html", order=orders.get_order(order_id))


def _parse_lines(form) -> list[tuple[int, int, str | None]]:
    """Form fields qty_<menu_item_id> / note_<menu_item_id> -> (id, qty, note), qty > 0 only."""
    lines = []
    for key, value in form.multi_items():
        if not key.startswith("qty_") or not str(value).strip():
            continue
        try:
            menu_item_id, qty = int(key[4:]), int(str(value))
        except ValueError:
            raise ServiceError("Quantities must be whole numbers")
        if qty == 0:
            continue
        lines.append((menu_item_id, qty, form.get(f"note_{menu_item_id}")))
    return lines


@router.post("/orders/{order_id}/kot")
async def send_kot(request: Request, order_id: int, staff: CurrentStaff = Depends(floor_staff)):
    form = await request.form()
    try:
        version = int(form.get("version", ""))
    except ValueError:
        raise ServiceError("Order changed, reload")
    lines = _parse_lines(form)
    # Async only to read the dynamic form; the blocking service call runs in the thread pool
    _, events = await run_in_threadpool(
        orders.send_kot, order_id, str(form.get("kot_id", "")), staff.id, lines, version
    )
    publish(events)
    return see_other(f"/orders/{order_id}")


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, reason: str = Form(""), staff: CurrentStaff = Depends(floor_staff)):
    _, events = orders.cancel_order(order_id, reason, staff.id)
    publish(events)
    return see_other("/floor")


@router.post("/items/{item_id}/serve")
def serve_item(request: Request, item_id: int, staff: CurrentStaff = Depends(floor_staff)):
    data, events = kitchen.serve_item(item_id, staff.id)
    publish(events)
    # Back to the order screen or the counter's bill preview, whichever it came from
    return see_other(back_url(request, fallback=f"/orders/{data['order_id']}"))

