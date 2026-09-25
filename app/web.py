"""Shared helpers for routers: templates, flash messages, redirects, event publishing."""
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Path as PathParam
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import config, events
from app.auth import CurrentStaff, csrf_token
from app.services import Event, ServiceError
from app.text import date_range, plural

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

ROLE_HOME = {"waiter": "/floor", "chef": "/kitchen", "counter": "/counter", "manager": "/counter"}
FLOOR_ROLES = ("waiter", "counter", "manager")
KITCHEN_ROLES = ("chef", "manager")
COUNTER_ROLES = ("counter", "manager")
CANCEL_REASONS = ("Customer changed mind", "Wrong item entered", "Out of stock")

# Path ids must fit SQLite's INTEGER; a huge number would otherwise crash the query
Id = Annotated[int, PathParam(ge=1, le=2**31 - 1)]


def rupees(paise: int | None) -> str:
    """Format integer paise as rupees with Indian digit grouping: 123456789 -> ₹12,34,567.89."""
    if paise is None:
        return ""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    return f"{sign}₹{digits}.{frac:02d}"


def table_alert_in(t: dict) -> int | None:
    """Seconds until a floor card should turn red (<= 0: red now; None: never)."""
    deadlines = []
    if t["status"] == "occupied" and not t["has_kot"]:
        deadlines.append(config.WARN_NO_ORDER_MIN * 60 - t["seconds_in_status"])
    if t["ready_waiting_seconds"] is not None:
        deadlines.append(config.WARN_FOOD_WAITING_MIN * 60 - t["ready_waiting_seconds"])
    return min(deadlines) if deadlines else None


templates.env.filters["rupees"] = rupees
templates.env.filters["plural"] = plural
templates.env.filters["date_range"] = date_range
templates.env.globals.update(
    RESTAURANT_NAME=config.RESTAURANT_NAME,
    BUSINESS_DAY_START_HOUR=config.BUSINESS_DAY_START_HOUR,
    WARN_KITCHEN_SEC=config.WARN_KITCHEN_MIN * 60,
    table_alert_in=table_alert_in,
    CANCEL_REASONS=CANCEL_REASONS,
    # Chart.js is vendored at app/static/chart.umd.min.js (no CDN); pages fall back to tables without it
    CHART_JS_AVAILABLE=(Path(__file__).resolve().parent / "static" / "chart.umd.min.js").is_file(),
)


def flash(request: Request, message: str, kind: str = "error") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def render(request: Request, name: str, staff: CurrentStaff | None = None,
           status_code: int = 200, **context):
    """Render a template with the logged-in staff and any pending flash message."""
    session_flash = request.session.pop("flash", None)
    context.update(staff=staff, flash=context.get("flash") or session_flash, csrf_token=csrf_token(request))
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def partial(request: Request, name: str, **context):
    """Render an HTMX fragment (no flash handling)."""
    context.update(csrf_token=csrf_token(request))
    return templates.TemplateResponse(request, name, context)


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def back_url(request: Request, fallback: str = "/") -> str:
    """Path of the page the request came from. Only the path is kept, so it stays on this site."""
    referer = request.headers.get("referer")
    if not referer:
        return fallback
    parts = urlsplit(referer)
    path = parts.path
    # Browsers treat "//x" and "/\\x" as another host; only plain site paths are allowed
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return fallback
    return path + (f"?{parts.query}" if parts.query else "")


def publish(events_to_send: list[Event]) -> None:
    """Send events to live screens. Call only after the service call has returned (committed)."""
    events.publish(events_to_send)


MAX_RUPEES = Decimal(10**8)  # parsing sanity bound; services apply the real limits


def parse_rupees(text: str | None, label: str) -> int:
    """'50' / '49.5' / '₹1,250' / '' -> paise. Parsing only; services validate the amount."""
    text = (text or "").strip().replace(",", "").lstrip("₹").strip()
    if not text:
        return 0
    bad = ServiceError(f"Enter the {label.lower()} in rupees, e.g. 50 or 49.50")
    try:
        value = Decimal(text)
        # adjusted() reads the exponent without arithmetic, so '1e1000000' can't overflow here
        if not value.is_finite() or value.adjusted() > MAX_RUPEES.adjusted():
            raise bad
        if value != value.quantize(Decimal("0.01")):
            raise ServiceError(f"{label} can have at most 2 decimal places")
        return int(value * 100)
    except DecimalException:
        raise bad
