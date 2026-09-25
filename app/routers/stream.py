"""GET /stream: Server-Sent Events, channels chosen by role."""
import json

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from app import events
from app.auth import CurrentStaff, current_staff
from app.models import STATIONS
from app.services.tables import list_sections

router = APIRouter()

PING_SECONDS = 15


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
async def stream(all: bool = False, staff: CurrentStaff = Depends(current_staff)):
    channels = channels_for(staff, await run_in_threadpool(list_sections), all_sections=all)

    async def gen():
        queue = events.subscribe(channels)
        try:
            while True:
                ev = await queue.get()
                if ev is events.CLOSE:
                    return  # dropped for falling behind; the browser reconnects and reloads
                yield {"event": ev.type,
                       "data": json.dumps({"channel": ev.channel, **ev.data}, default=str)}
        finally:
            events.unsubscribe(queue)

    return EventSourceResponse(gen(), ping=PING_SECONDS)
