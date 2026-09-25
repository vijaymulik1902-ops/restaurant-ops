"""Waiter order screen: send KOTs, mark items served, cancel items or the order."""
import uuid

from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool

from app.auth import CurrentStaff, require_role
from app.services import ServiceError, kitchen, orders
from app.web import FLOOR_ROLES, Id, back_url, flash, partial, publish, render, see_other

router = APIRouter()
floor_staff = require_role(*FLOOR_ROLES)

MAX_DB_ID = 2**31 - 1


def _new_kot_id() -> str:
    return str(uuid.uuid4())


@router.get("/orders/{order_id}")
def order_page(request: Request, order_id: Id, staff: CurrentStaff = Depends(floor_staff)):
    screen = orders.order_screen(order_id, staff.id, staff.role)
    # A fresh KOT id per render: a double-tap or retry of THIS form reuses it
    return render(request, "order.html", staff, **screen, kot_id=_new_kot_id(), selection={})


@router.get("/orders/{order_id}/items")
def order_items(request: Request, order_id: Id, staff: CurrentStaff = Depends(floor_staff)):
    screen = orders.order_screen(order_id, staff.id, staff.role, include_menu=False)
    return partial(request, "_order_items.html", order=screen["order"])


def _text(value) -> str:
    """A form value as text; anything else (e.g. an uploaded file) counts as empty."""
    return value if isinstance(value, str) else ""


def _selection(form) -> dict[int, dict]:
    """The waiter's raw picks, so a failed send can re-render them unchanged."""
    picked: dict[int, dict] = {}
    for key, value in form.multi_items():
        if key.startswith("qty_") and key[4:].isdigit() and len(key) < 16:
            menu_item_id = int(key[4:])
            picked[menu_item_id] = {"qty": _text(value).strip()[:4],
                                    "note": _text(form.get(f"note_{menu_item_id}"))[:500]}
    return picked


def _parse_lines(form) -> list[tuple[int, int, str | None]]:
    """Form fields qty_<menu_item_id> / note_<menu_item_id> -> (id, qty, note), qty != 0 only.

    Parsing only: quantity limits, note length and availability are checked by the service.
    """
    lines = []
    for key, value in form.multi_items():
        if not key.startswith("qty_"):
            continue
        text = _text(value).strip()
        if not text:
            continue
        try:
            menu_item_id, qty = int(key[4:]), int(text)
        except ValueError:
            raise ServiceError("Quantities must be whole numbers")
        if not 1 <= menu_item_id <= MAX_DB_ID:
            raise ServiceError("Unknown menu item")
        if qty == 0:
            continue
        lines.append((menu_item_id, qty, _text(form.get(f"note_{menu_item_id}")) or None))
    return lines


@router.post("/orders/{order_id}/kot")
async def send_kot(request: Request, order_id: Id, staff: CurrentStaff = Depends(floor_staff)):
    # Async only to read the dynamic form; blocking service calls run in the thread pool
    form = await request.form()
    try:
        lines = _parse_lines(form)
        result, events = await run_in_threadpool(
            orders.send_kot, order_id, _text(form.get("kot_id")), staff.id, lines
        )
    except ServiceError as e:
        # Re-render with the waiter's picks intact. Nothing was saved, so a fresh KOT id is safe.
        screen = await run_in_threadpool(orders.order_screen, order_id, staff.id, staff.role)
        return render(request, "order.html", staff, status_code=422, **screen,
                      kot_id=_new_kot_id(), selection=_selection(form),
                      flash={"message": e.message, "kind": "error"})
    publish(events)
    if not result["duplicate"]:
        flash(request, f"KOT #{result['kot_number']} sent to kitchen", kind="sent")
    return see_other(f"/orders/{order_id}")


def _version(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise ServiceError("Order changed, reload")


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: Id, reason: str = Form(""), version: str = Form(""),
                 staff: CurrentStaff = Depends(floor_staff)):
    _, events = orders.cancel_order(order_id, reason, staff.id, expected_version=_version(version))
    publish(events)
    return see_other("/floor")


@router.post("/items/{item_id}/serve")
def serve_item(request: Request, item_id: Id, staff: CurrentStaff = Depends(floor_staff)):
    data, events = kitchen.serve_item(item_id, staff.id)
    publish(events)
    # Back to the order screen or the counter's bill preview, whichever it came from
    return see_other(back_url(request, fallback=f"/orders/{data['order_id']}"))


@router.post("/items/{item_id}/cancel")
def cancel_item(request: Request, item_id: Id, reason: str = Form(""), reason_choice: str = Form(""),
                staff: CurrentStaff = Depends(floor_staff)):
    # Typed text wins over a quick-pick chip; the service requires one of them
    data, events = kitchen.cancel_item(item_id, reason.strip() or reason_choice, staff.id)
    publish(events)
    return see_other(back_url(request, fallback=f"/orders/{data['order_id']}"))
