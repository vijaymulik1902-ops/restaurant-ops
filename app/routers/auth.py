"""Login and logout."""
from fastapi import APIRouter, Depends, Form, Request

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
    try:
        staff = auth.login(name, pin)
    except auth.AuthError as e:
        return render(request, "login.html", status_code=401, error=e.message,
                      selected=name, staff_list=auth.active_staff_names())
    auth.start_session(request, staff)
    return see_other("/")


@router.post("/logout")
def logout(request: Request):
    auth.end_session(request)
    return see_other("/login")
