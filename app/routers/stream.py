"""GET /stream: Server-Sent Events, channels chosen by role."""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sse_starlette.event import ServerSentEvent
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from app import events
from app.auth import CurrentStaff, load_staff, optional_staff
from app.models import STATIONS
from app.services.tables import list_sections

router = APIRouter()

PING_SECONDS = 15
RECHECK_SECONDS = 30  # a deactivated staff member's open stream ends within this time


def channels_for(staff: CurrentStaff, sections: list[str], all_sections: bool = False) -> list[str]:
    """Channels a staff member listens to.

    waiter: own section (or every section with the "All tables" toggle) + own waiter channel
    chef: own station
    counter: counter + all sections
    manager: counter + all sections + all stations (a manager may run the kitchen board)
    """
    if staff.role == "waiter":
        chosen = sections if all_sections or not staff.section else [staff.section]
        return [f"section:{s}" for s in chosen] + [f"waiter:{staff.id}"]
    if staff.role == "chef":
        return [f"station:{staff.station}"] if staff.station else []
    chans = ["counter"] + [f"section:{s}" for s in sections]
    if staff.role == "manager":
        chans += [f"station:{st}" for st in STATIONS]
    return chans


@router.get("/stream")
async def stream(request: Request, all: bool = False):
    # 401, not a redirect: EventSource can't follow a login page, and app.js uses this
    # (via /auth/check) to stop reconnecting and send the user to /login
    staff = await run_in_threadpool(optional_staff, request)
    if staff is None:
        return Response(status_code=401)
    channels = channels_for(staff, await run_in_threadpool(list_sections), all_sections=all)

    async def gen():
        queue = events.subscribe(channels)
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=RECHECK_SECONDS)
                except TimeoutError:
                    current = await run_in_threadpool(load_staff, staff.id)
                    if current is None or current.pin_version != staff.pin_version:
                        return  # deactivated, or PIN changed, while connected
                    continue
                if ev is events.CLOSE:
                    return  # dropped for falling behind; the browser reconnects and reloads
                yield {"event": ev.type,
                       "data": json.dumps({"channel": ev.channel, **ev.data}, default=str)}
        finally:
            events.unsubscribe(queue)

    # The heartbeat is a real named "ping" event (not an invisible ":" comment), so the page
    # can tell a live stream from one a proxy is buffering (e.g. Cloudflare quick tunnels hold
    # the whole response) and fall back to polling. Exactly "text/event-stream" (Starlette would
    # append "; charset=utf-8"; SSE is UTF-8 by definition) so proxies recognise the stream.
    return EventSourceResponse(gen(), ping=PING_SECONDS, headers={"Content-Type": "text/event-stream"},
                               ping_message_factory=lambda: ServerSentEvent(data="", event="ping"))
