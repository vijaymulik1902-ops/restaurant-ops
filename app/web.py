"""Shared helpers for routers: templates, flash messages, redirects, event publishing."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Path as PathParam
from fastapi import Request
from pydantic import BeforeValidator, Field
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import config, events
from app.auth import CurrentStaff, csrf_token
from app.services import Event, ServiceError
from app.text import date_range, initials, plural, role_label

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

ROLE_HOME = {"waiter": "/floor", "chef": "/kitchen", "counter": "/counter", "manager": "/home"}
FLOOR_ROLES = ("waiter", "counter", "manager")
KITCHEN_ROLES = ("chef", "manager")
COUNTER_ROLES = ("counter", "manager")
BOOKING_VIEW_ROLES = ("waiter", "counter", "manager")  # waiters: today's bookings, read-only, no phones
BOOKING_EDIT_ROLES = ("counter", "manager")
CANCEL_REASONS = ("Customer changed mind", "Wrong item entered", "Out of stock")
BOOKING_CANCEL_REASONS = ("Guest cancelled", "Plans changed", "Booked by mistake")

# ---------- navigation (one definition drives the phone tab bar, the More sheet and the sidebar) ----------
# Every link points at a route the role can already open (see CLAUDE.md access matrix); items
# with a "sheet" open an in-page panel instead of navigating. Tests check both.
NAV_ITEMS = {
    "home": {"label": "Home", "href": "/home", "icon": "home"},
    "floor": {"label": "Floor", "href": "/floor", "icon": "floor"},
    "orders": {"label": "Orders", "href": "/floor#orders", "icon": "orders"},
    "alerts": {"label": "Alerts", "sheet": "alerts", "icon": "bell"},
    "kitchen": {"label": "Kitchen", "href": "/kitchen", "icon": "flame"},
    "summary": {"label": "Summary", "href": "/kitchen#cook-summary", "icon": "clipboard"},
    "availability": {"label": "Availability", "href": "/kitchen/availability", "icon": "toggle"},
    "counter": {"label": "Counter", "href": "/counter", "icon": "receipt"},
    "reports": {"label": "Reports", "href": "/reports/sales", "icon": "chart"},
    "dayclose": {"label": "Day close", "href": "/reports/day-close", "icon": "calendar"},
    "insights": {"label": "Insights", "href": "/insights", "icon": "bulb"},
    "menu": {"label": "Menu", "href": "/menu", "icon": "book"},
    "staff": {"label": "Staff", "href": "/staff", "icon": "users"},
    "expenses": {"label": "Expenses", "href": "/expenses", "icon": "wallet"},
    "audit": {"label": "Audit", "href": "/audit", "icon": "shield"},
    "bookings": {"label": "Bookings", "href": "/bookings", "icon": "bookings"},
    "me": {"label": "Me", "sheet": "me", "icon": "user"},
    "more": {"label": "More", "sheet": "more", "icon": "dots"},
}
PHONE_TABS = {
    "waiter": ["floor", "orders", "bookings", "alerts", "me"],
    "chef": ["kitchen", "summary", "availability", "me"],
    "counter": ["counter", "floor", "bookings", "me"],
    "manager": ["home", "floor", "kitchen", "reports", "more"],
}
MORE_SHEET = ["bookings", "menu", "staff", "expenses", "audit", "dayclose", "insights"]
SIDEBAR = {
    "waiter": [("Operations", ["floor", "orders", "bookings", "alerts"])],
    "chef": [("Kitchen", ["kitchen", "summary", "availability"])],
    "counter": [("Operations", ["counter", "floor", "bookings"])],
    "manager": [("Operations", ["home", "floor", "kitchen", "counter", "bookings"]),
                ("Reports", ["reports", "dayclose", "insights"]),
                ("Admin", ["menu", "staff", "expenses", "audit"])],
}


def nav_for(role: str, path: str = "") -> dict:
    """Role-filtered navigation with the active item marked."""
    def item(key: str) -> dict:
        it = {"key": key, **NAV_ITEMS[key]}
        href = it.get("href", "")
        it["active"] = bool(href) and "#" not in href and (path == href or (href != "/" and path.startswith(href + "/")))
        return it
    return {
        "tabs": [item(k) for k in PHONE_TABS.get(role, [])],
        "more": [item(k) for k in MORE_SHEET] if role == "manager" else [],
        "sidebar": [(group, [item(k) for k in keys]) for group, keys in SIDEBAR.get(role, [])],
    }

# Path ids must fit SQLite's INTEGER; a huge number would otherwise crash the query
Id = Annotated[int, PathParam(ge=1, le=2**31 - 1)]


def _blank_is_missing(value):
    """HTML GET forms send every field, empty ones as "" (e.g. ?date_from=&staff_id=).
    That means "not set", not "invalid"."""
    return None if isinstance(value, str) and not value.strip() else value


# Optional query filters that tolerate empty form fields
OptionalDate = Annotated[date | None, BeforeValidator(_blank_is_missing)]
# The bounds sit on the int inside the union, so a blank field (None) isn't range-checked
OptionalId = Annotated[Annotated[int, Field(ge=1, le=2**31 - 1)] | None, BeforeValidator(_blank_is_missing)]


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


def business_datetime(day: date, at: time) -> datetime:
    """A date + clock time from a form, read as a BUSINESS day: 00:30 on the 27th's form
    means the night of the 27th (calendar 28th), like every report in the app."""
    ts = datetime.combine(day, at)
    return ts + timedelta(days=1) if at.hour < config.BUSINESS_DAY_START_HOUR else ts


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
templates.env.filters["initials"] = initials
templates.env.globals["role_label"] = role_label
templates.env.globals.update(
    RESTAURANT_NAME=config.RESTAURANT_NAME,
    BUSINESS_DAY_START_HOUR=config.BUSINESS_DAY_START_HOUR,
    WARN_KITCHEN_SEC=config.WARN_KITCHEN_MIN * 60,
    table_alert_in=table_alert_in,
    CANCEL_REASONS=CANCEL_REASONS,
    demo_mode=lambda: config.DEMO_MODE,  # read at render time (tests and .env can switch it)
    nav_for=nav_for,
    BOOKING_EDIT_ROLES=BOOKING_EDIT_ROLES,
    BOOKING_CANCEL_REASONS=BOOKING_CANCEL_REASONS,
    DURATION_CHOICES=(30, 45, 60, 90, 120, 150, 180, 240, 300),
    timedelta=timedelta,
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
