"""Login and logout."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse

from app import auth
from app.web import render, see_other

router = APIRouter()


@router.get("/login")
def login_page(request: Request, staff: auth.CurrentStaff | None = Depends(auth.optional_staff)):
    if staff is not None:
        return see_other("/")
    return render(request, "login.html", staff_list=auth.active_staff_names())


@router.post("/login")
def login_submit(request: Request, name: str = Form(""), pin: str = Form("")):
    wait = auth.ip_limiter.attempt(auth.client_ip(request))
    if wait:
        return render(request, "login.html", status_code=429, selected=name,
                      error=f"Too many login attempts from this network. Try again in {(wait + 59) // 60} min",
                      staff_list=auth.active_staff_names())
    try:
        staff = auth.login(name, pin)
    except auth.AuthError as e:
        return render(request, "login.html", status_code=401, error=e.message,
                      selected=name, staff_list=auth.active_staff_names())
    auth.start_session(request, staff)
    return see_other("/")


@router.post("/logout")
def logout(request: Request):
    auth.end_session(request)  # clears everything, including the CSRF token
    return see_other("/login")


@router.get("/auth/check")
def auth_check(staff: auth.CurrentStaff | None = Depends(auth.optional_staff)):
    """Tiny probe for app.js: 200 while logged in, 401 once the session is gone."""
    headers = {"Cache-Control": "no-store"}
    if staff is None:
        return JSONResponse({"ok": False}, status_code=401, headers=headers)
    return JSONResponse({"ok": True, "role": staff.role}, headers=headers)
