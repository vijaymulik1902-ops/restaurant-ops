"""Login, sessions and role checks.

The session cookie (signed with SECRET_KEY) holds only the staff id. Staff are
reloaded from the DB on every request, so deactivating someone logs them out
on their very next tap.
"""
import logging
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app.config import COOKIE_SECURE, SECRET_KEY
from app.db import read_session
from app.models import Staff

log = logging.getLogger(__name__)

SESSION_MAX_AGE = 14 * 60 * 60  # one long shift
MAX_WRONG_PINS = 5
LOCKOUT_WINDOW_SEC = 5 * 60
MAX_PIN_LEN = 8
# PIN length by role (enforced when a PIN is set; login accepts 4-8 digits so older PINs work)
PIN_LENGTH = {"manager": 6}
DEFAULT_PIN_LENGTH = 4


def pin_length_for(role: str) -> int:
    return PIN_LENGTH.get(role, DEFAULT_PIN_LENGTH)

# Monotonic clock, replaceable in tests
_clock = time.monotonic


@dataclass(frozen=True)
class CurrentStaff:
    id: int
    name: str
    role: str
    section: str | None
    station: str | None


class AuthError(Exception):
    """Login failed; `message` is safe to show on the login screen."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class LoginRequired(Exception):
    """Raised by current_staff; main.py turns it into a redirect to /login."""


DEV_SECRET_KEY = "dev-only-change-me"


def check_production_settings(secret_key: str | None = None, cookie_secure: bool | None = None) -> None:
    """Refuse to start in production (COOKIE_SECURE on) with the public dev SECRET_KEY:
    anyone could forge a session cookie and log in as the manager."""
    key = SECRET_KEY if secret_key is None else secret_key
    secure = COOKIE_SECURE if cookie_secure is None else cookie_secure
    if secure and key == DEV_SECRET_KEY:
        raise RuntimeError("Set a real SECRET_KEY in .env before running with COOKIE_SECURE=true")


def install(app: FastAPI) -> None:
    """Add the signed-cookie session middleware."""
    if SECRET_KEY == DEV_SECRET_KEY:
        log.warning("SECRET_KEY is the dev default; set SECRET_KEY in .env before real use")
    app.add_middleware(
        SessionMiddleware, secret_key=SECRET_KEY, max_age=SESSION_MAX_AGE,
        same_site="lax", https_only=COOKIE_SECURE,
    )


# ---------- CSRF ----------
# One random token per session. Every POST must echo it back, either as the
# X-CSRF-Token header (HTMX, set via hx-headers on <body>) or as the csrf_token
# form field (plain forms). A page from another site can't read it, so it can't forge a POST.
CSRF_SESSION_KEY = "csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def csrf_token(request: Request) -> str:
    """This session's CSRF token, created on first use."""
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


async def csrf_protect(request: Request) -> None:
    """App-wide dependency: reject unsafe requests without the session's token (403)."""
    if request.method not in UNSAFE_METHODS:
        return
    expected = request.session.get(CSRF_SESSION_KEY)
    sent = request.headers.get(CSRF_HEADER)
    if not sent:
        field = (await request.form()).get(CSRF_FORM_FIELD)
        sent = field if isinstance(field, str) else None
    if not expected or not sent or not secrets.compare_digest(str(expected), sent):
        raise HTTPException(status_code=403,
                            detail="This page has expired. Go back, reload it and try again.")


class _PinLimiter:
    """Counts wrong PINs per staff member in a sliding window (in memory, per process)."""

    def __init__(self) -> None:
        self._failures: dict[int, deque[float]] = {}
        self._lock = threading.Lock()  # sync routes run in a thread pool

    def _recent(self, staff_id: int, at: float) -> deque[float]:
        q = self._failures.setdefault(staff_id, deque())
        while q and at - q[0] >= LOCKOUT_WINDOW_SEC:
            q.popleft()
        return q

    def seconds_locked(self, staff_id: int) -> int:
        at = _clock()
        with self._lock:
            q = self._recent(staff_id, at)
            if len(q) < MAX_WRONG_PINS:
                return 0
            return max(1, int(LOCKOUT_WINDOW_SEC - (at - q[0])))

    def fail(self, staff_id: int) -> None:
        at = _clock()
        with self._lock:
            self._recent(staff_id, at).append(at)

    def reset(self, staff_id: int | None = None) -> None:
        with self._lock:
            if staff_id is None:
                self._failures.clear()
            else:
                self._failures.pop(staff_id, None)


limiter = _PinLimiter()


# Per-IP limit on POST /login, on top of the per-staff lockout: stops one device (or bot)
# from trying PINs across many names. Counts every attempt. Staff behind the restaurant's
# Wi-Fi share one public IP, so keep the limit above the number of staff logging in at
# shift start. Raise it for load tests (many simulated users on 127.0.0.1).
LOGIN_IP_LIMIT = int(os.getenv("LOGIN_IP_LIMIT", "20"))
LOGIN_IP_WINDOW_SEC = int(os.getenv("LOGIN_IP_WINDOW_SEC", "600"))


class _IpLimiter:
    """Sliding-window counter of login attempts per client IP (in memory, per process)."""

    MAX_TRACKED = 10_000  # prune idle IPs beyond this so memory can't grow without bound

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, ip: str, at: float) -> deque[float]:
        q = self._hits.setdefault(ip, deque())
        while q and at - q[0] >= LOGIN_IP_WINDOW_SEC:
            q.popleft()
        return q

    def attempt(self, ip: str) -> int:
        """Record one attempt. Returns 0 if allowed, else seconds until the next is allowed."""
        at = _clock()
        with self._lock:
            if len(self._hits) > self.MAX_TRACKED:
                for key in [k for k, q in self._hits.items() if not q or at - q[-1] >= LOGIN_IP_WINDOW_SEC]:
                    del self._hits[key]
            q = self._recent(ip, at)
            if len(q) >= LOGIN_IP_LIMIT:
                return max(1, int(LOGIN_IP_WINDOW_SEC - (at - q[0])))
            q.append(at)
            return 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


ip_limiter = _IpLimiter()


def client_ip(request: Request) -> str:
    """The caller's IP. Behind Render's proxy, uvicorn --proxy-headers puts the real one here."""
    return request.client.host if request.client else "unknown"


def _to_current(staff: Staff) -> CurrentStaff:
    return CurrentStaff(staff.id, staff.name, staff.role, staff.section, staff.station)


def active_staff_names() -> list[dict]:
    """Names and roles for the login picker (no PIN data)."""
    with read_session() as s:
        rows = s.execute(
            select(Staff.name, Staff.role).where(Staff.active.is_(True)).order_by(Staff.role, Staff.name)
        ).all()
    return [{"name": r.name, "role": r.role, "pin_length": pin_length_for(r.role)} for r in rows]


def login(name: str, pin: str) -> CurrentStaff:
    """Check name + PIN. Raises AuthError on failure or while locked out."""
    with read_session() as s:
        staff = s.scalar(select(Staff).where(Staff.name == name))
        if staff is None or not staff.active:
            raise AuthError("Wrong name or PIN")
        pin_hash = staff.pin_hash
        current = _to_current(staff)

    locked = limiter.seconds_locked(current.id)
    if locked:
        raise AuthError(f"Too many wrong PINs. Try again in {(locked + 59) // 60} min")
    # bcrypt rejects inputs over 72 bytes, so check the shape before hashing
    well_formed = isinstance(pin, str) and pin.isascii() and pin.isdigit() and 4 <= len(pin) <= MAX_PIN_LEN
    if not well_formed or not bcrypt.checkpw(pin.encode(), pin_hash.encode()):
        limiter.fail(current.id)
        raise AuthError("Wrong name or PIN")
    limiter.reset(current.id)
    return current


def start_session(request: Request, staff: CurrentStaff) -> None:
    token = request.session.get(CSRF_SESSION_KEY)
    request.session.clear()
    request.session["staff_id"] = staff.id
    if token:
        request.session[CSRF_SESSION_KEY] = token  # keeps other open tabs' forms valid


def end_session(request: Request) -> None:
    request.session.clear()


def load_staff(staff_id: int) -> CurrentStaff | None:
    """Active staff member by id, or None."""
    with read_session() as s:
        staff = s.get(Staff, staff_id)
        if staff is None or not staff.active:
            return None
        return _to_current(staff)


def optional_staff(request: Request) -> CurrentStaff | None:
    """Logged-in staff or None (for pages like /login and /)."""
    staff_id = request.session.get("staff_id")
    if not isinstance(staff_id, int):
        return None
    staff = load_staff(staff_id)
    if staff is None:
        request.session.clear()
    return staff


def current_staff(request: Request) -> CurrentStaff:
    """Dependency: the logged-in, still-active staff member, else redirect to /login."""
    staff = optional_staff(request)
    if staff is None:
        raise LoginRequired()
    return staff


def require_role(*roles: str):
    """Dependency factory: 403 unless the current staff member has one of `roles`."""

    def check(staff: CurrentStaff = Depends(current_staff)) -> CurrentStaff:
        if staff.role not in roles:
            raise HTTPException(status_code=403, detail="Not allowed for your role")
        return staff

    return check
