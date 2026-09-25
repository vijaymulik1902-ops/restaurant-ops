"""FastAPI app: middleware, error handling, routers.

Run with a SINGLE worker: the SSE broadcaster lives in this process's memory.
    uvicorn app.main:app --reload            (add --host 0.0.0.0 for phones on Wi-Fi)
"""
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import auth
from app.db import init_db
from app.routers import auth as auth_routes
from app.routers import counter, floor, kitchen, orders, stream
from app.services import ServiceError
from app.web import ROLE_HOME, back_url, flash, is_htmx, see_other

STALE_MESSAGE = "Someone else updated this, reloaded"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Restaurant Ops", lifespan=lifespan, docs_url=None, redoc_url=None)
auth.install(app)
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")
for module in (auth_routes, floor, orders, kitchen, counter, stream):
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


@app.exception_handler(auth.LoginRequired)
async def on_login_required(request: Request, _exc: auth.LoginRequired):
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 403 and "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            "<!doctype html><meta name=viewport content='width=device-width'>"
            "<body style='font-family:system-ui;padding:2rem;background:#111;color:#eee'>"
            "<h2>Not allowed</h2><p>Your role can't open this screen.</p>"
            "<p><a style='color:#8cf' href='/'>Go to my screen</a></p>",
            status_code=403,
        )
    return await http_exception_handler(request, exc)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/")
def home(staff: auth.CurrentStaff | None = Depends(auth.optional_staff)):
    if staff is None:
        return see_other("/login")
    return see_other(ROLE_HOME[staff.role])

