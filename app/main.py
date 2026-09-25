"""FastAPI app: middleware, error handling, routers.

Run with a SINGLE worker: the SSE broadcaster lives in this process's memory.
    uvicorn app.main:app --reload            (add --host 0.0.0.0 for phones on Wi-Fi)
"""
import asyncio
import json
import logging
import os
from html import escape
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import auth
from app.db import init_db
from app.migrations import ensure_schema
from app.routers import auth as auth_routes
from app.routers import counter, floor, kitchen, manager, orders, stream
from app.services import ServiceError, backups
from app.web import ROLE_HOME, back_url, flash, is_htmx, see_other

log = logging.getLogger("app")

STALE_MESSAGE = "Someone else updated this, reloaded"
BUSY_MESSAGE = "The system is busy, please try again"
BAD_INPUT_MESSAGE = "Something in that form wasn't valid, please check and try again"


BACKUP_INTERVAL_SEC = 24 * 60 * 60


async def _backup_now() -> None:
    try:
        await run_in_threadpool(backups.ensure_daily_backup)
    except Exception:  # noqa: BLE001 - a failed backup must not take the restaurant down
        log.exception("Daily backup failed")


async def _daily_backups() -> None:
    """After the startup backup: another every 24 hours."""
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_SEC)
        await _backup_now()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    auth.check_production_settings()
    init_db()
    ensure_schema()
    task = None
    if os.getenv("BACKUPS_ENABLED", "1") == "1":
        await _backup_now()  # today's backup, if missing, before serving (a copy takes seconds at most)
        task = asyncio.create_task(_daily_backups())
    yield
    if task:
        task.cancel()


app = FastAPI(title="Restaurant Ops", lifespan=lifespan, docs_url=None, redoc_url=None,
              dependencies=[Depends(auth.csrf_protect)])
auth.install(app)
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")
for module in (auth_routes, floor, orders, kitchen, counter, manager, stream):
    app.include_router(module.router)


def _htmx_flash(message: str, *, refresh: bool = False) -> Response:
    """Tell HTMX not to swap anything and show a toast (or reload the page)."""
    headers = {"HX-Trigger": json.dumps({"flash": {"message": message}})}
    if refresh:
        headers["HX-Refresh"] = "true"
    else:
        headers["HX-Reswap"] = "none"
    return Response(status_code=200, headers=headers)


def _flash_and_return(request: Request, message: str, *, refresh: bool = False) -> Response:
    if is_htmx(request):
        if refresh:
            flash(request, message)
        return _htmx_flash(message, refresh=refresh)
    flash(request, message)
    # A failed GET goes home (avoids redirect loops); a failed POST goes back to its form
    return see_other(back_url(request) if request.method == "POST" else "/")


@app.exception_handler(ServiceError)
async def on_service_error(request: Request, exc: ServiceError):
    return _flash_and_return(request, exc.message)


@app.exception_handler(StaleDataError)
async def on_stale(request: Request, _exc: StaleDataError):
    return _flash_and_return(request, STALE_MESSAGE, refresh=True)


@app.exception_handler(IntegrityError)
async def on_integrity(request: Request, exc: IntegrityError):
    # Services check rules first; this only fires on a race the DB caught for us
    log.warning("IntegrityError on %s %s: %s", request.method, request.url.path, exc.orig)
    return _flash_and_return(request, STALE_MESSAGE, refresh=True)


@app.exception_handler(OperationalError)
async def on_db_busy(request: Request, exc: OperationalError):
    log.error("OperationalError on %s %s: %s", request.method, request.url.path, exc.orig)
    if request.method == "GET" and not is_htmx(request):
        # Not a redirect: if every screen is failing, redirecting home would loop forever
        return HTMLResponse(_simple_page("Busy", BUSY_MESSAGE), status_code=503)
    return _flash_and_return(request, BUSY_MESSAGE)


@app.exception_handler(RequestValidationError)
async def on_bad_input(request: Request, exc: RequestValidationError):
    """Malformed ids / form fields: a friendly message, never a JSON error page."""
    log.info("Bad input on %s %s: %s", request.method, request.url.path, exc.errors())
    if request.url.path == "/stream":
        return JSONResponse({"detail": "bad request"}, status_code=400)
    return _flash_and_return(request, BAD_INPUT_MESSAGE)


@app.exception_handler(Exception)
async def on_unexpected(request: Request, exc: Exception):
    """Last resort: log the traceback, show staff a plain page instead of a stack trace."""
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    if is_htmx(request):
        headers = {"HX-Trigger": json.dumps({"flash": {"message": "Something went wrong, try again"}}),
                   "HX-Reswap": "none"}
        return Response(status_code=500, headers=headers)
    return HTMLResponse(_simple_page("Something went wrong",
                                     "Nothing was lost. Go back and try again."), status_code=500)


def _simple_page(title: str, message: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<body style='font-family:system-ui;padding:2rem;background:#111;color:#eee;font-size:1.1rem'>"
        f"<h2>{escape(title)}</h2><p>{escape(message)}</p>"
        "<p><a style='color:#8cf;font-size:1.2rem' href='/'>Go to my screen</a></p>"
    )


@app.exception_handler(auth.LoginRequired)
async def on_login_required(request: Request, _exc: auth.LoginRequired):
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    if is_htmx(request):
        headers = {"HX-Trigger": json.dumps({"flash": {"message": detail}}), "HX-Reswap": "none"}
        return Response(status_code=exc.status_code, headers=headers)
    if exc.status_code == 404:
        return HTMLResponse(_simple_page("Not found", "That page doesn't exist."), status_code=404)
    if exc.status_code == 403:
        title = "Not allowed" if "role" in detail.lower() else "Please reload"
        return HTMLResponse(_simple_page(title, detail), status_code=403)
    return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/")
def home(staff: auth.CurrentStaff | None = Depends(auth.optional_staff)):
    if staff is None:
        return see_other("/login")
    return see_other(ROLE_HOME[staff.role])

