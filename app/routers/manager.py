"""Manager-only screens: audit log, menu, expenses, sales report. These may show cost."""
import csv
import io
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from app.auth import CurrentStaff, require_role
from app.db import now
from app.models import EXPENSE_CATEGORIES, STATIONS
from app.services import ServiceError, audit, backups, business_day_of, expenses, menu, menu_admin, sales, staff_admin
from app.web import Id, flash, parse_rupees, publish, render, see_other

router = APIRouter()
manager_only = require_role("manager")


@router.get("/audit")
def audit_page(request: Request, date_from: date | None = None, date_to: date | None = None,
               staff_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None,
               action: str | None = None, page: Annotated[int, Query(ge=1, le=100_000)] = 1,
               staff: CurrentStaff = Depends(manager_only)):
    result = audit.list_audit(date_from, date_to, staff_id, action or None, page)
    filters = {"date_from": date_from, "date_to": date_to, "staff_id": staff_id, "action": action or ""}
    return render(request, "audit.html", staff, **result, filters=filters)


@router.get("/menu")
def menu_page(request: Request, archived: bool = False, staff: CurrentStaff = Depends(manager_only)):
    return render(request, "menu_admin.html", staff, dishes=menu_admin.list_menu_admin(show_archived=archived),
                  stations=STATIONS, show_archived=archived)


def _saved(request: Request, message: str, warning: str | None) -> None:
    flash(request, warning or message, kind="warn" if warning else "ok")


@router.post("/menu")
def add_dish(request: Request, name: str = Form(""), category: str = Form(""), station: str = Form(""),
             price: str = Form(""), cost: str = Form(""), staff: CurrentStaff = Depends(manager_only)):
    result, events = menu_admin.add_dish(name, category, station, parse_rupees(price, "Price"),
                                         parse_rupees(cost, "Cost"), staff.id)
    publish(events)
    _saved(request, f"{result['name']} added", result["warning"])
    return see_other(f"/menu#dish-{result['menu_item_id']}")


# Price and cost are separate forms and separate routes: each reads exactly one field,
# so saving a price can never touch the cost and vice versa.
@router.post("/menu/{menu_item_id}/price")
def save_price(request: Request, menu_item_id: Id, price: str = Form(""),
               staff: CurrentStaff = Depends(manager_only)):
    result, events = menu_admin.set_price(menu_item_id, parse_rupees(price, "Price"), staff.id)
    publish(events)
    _saved(request, "Price saved (applies to new orders only)" if result["changed"] else "Price unchanged",
           result["warning"])
    return see_other(f"/menu#dish-{menu_item_id}")


@router.post("/menu/{menu_item_id}/cost")
def save_cost(request: Request, menu_item_id: Id, cost: str = Form(""),
              staff: CurrentStaff = Depends(manager_only)):
    result, events = menu_admin.set_cost(menu_item_id, parse_rupees(cost, "Cost"), staff.id)
    publish(events)
    _saved(request, "Cost saved" if result["changed"] else "Cost unchanged", result["warning"])
    return see_other(f"/menu#dish-{menu_item_id}")


@router.post("/menu/{menu_item_id}/availability")
def toggle_availability(menu_item_id: Id, available: str = Form(""),
                        staff: CurrentStaff = Depends(manager_only)):
    _, events = menu.set_available(menu_item_id, available == "1", staff.id)
    publish(events)
    return see_other(f"/menu#dish-{menu_item_id}")


@router.post("/menu/{menu_item_id}/rename")
def rename_dish(request: Request, menu_item_id: Id, name: str = Form(""),
                staff: CurrentStaff = Depends(manager_only)):
    result, events = menu_admin.rename_dish(menu_item_id, name, staff.id)
    publish(events)
    _saved(request, f"Renamed to {result['name']} (past bills keep the old name)" if result["changed"]
           else "Name unchanged", None)
    return see_other(f"/menu#dish-{menu_item_id}")


@router.post("/menu/{menu_item_id}/archive")
def archive_dish(request: Request, menu_item_id: Id, archived: str = Form(""),
                 staff: CurrentStaff = Depends(manager_only)):
    result, events = menu_admin.set_archived(menu_item_id, archived == "1", staff.id)
    publish(events)
    if result["changed"]:
        _saved(request, f"{result['name']} {'archived' if result['archived'] else 'restored'}", None)
    return see_other("/menu?archived=1" if not result["archived"] else "/menu")


# ---------- expenses ----------

@router.get("/expenses")
def expenses_page(request: Request, start: date | None = None, end: date | None = None,
                  staff: CurrentStaff = Depends(manager_only)):
    if not (start or end):
        start, end = sales.preset_range("this_month")
    result = expenses.list_expenses(start or end, end or start)
    return render(request, "expenses.html", staff, **result, categories=EXPENSE_CATEGORIES,
                  today=now().date())


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise ServiceError("Pick the date the money was spent")


@router.post("/expenses")
def add_expense(request: Request, spent_on: str = Form(""), category: str = Form(""), amount: str = Form(""),
                note: str = Form(""), staff: CurrentStaff = Depends(manager_only)):
    result = expenses.add_expense(_parse_date(spent_on), category, parse_rupees(amount, "Amount"), note, staff.id)
    flash(request, f"Expense added: {result['category']}", kind="ok")
    return see_other("/expenses")


@router.post("/expenses/{expense_id}/delete")
def delete_expense(request: Request, expense_id: Id, reason: str = Form(""),
                   staff: CurrentStaff = Depends(manager_only)):
    expenses.delete_expense(expense_id, reason, staff.id)
    flash(request, "Expense deleted (kept in the audit log)", kind="ok")
    return see_other("/expenses")


# ---------- sales report ----------

@router.get("/reports/sales")
def sales_page(request: Request, preset: str | None = None, start: date | None = None,
               end: date | None = None, staff: CurrentStaff = Depends(manager_only)):
    start, end, preset = sales.resolve_range(preset, start, end)
    return render(request, "sales.html", staff, report=sales.sales_summary(start, end), preset=preset,
                  presets=sales.PRESETS, today=business_day_of(now()))


@router.get("/reports/sales.csv")
def sales_csv(preset: str | None = None, start: date | None = None, end: date | None = None,
              staff: CurrentStaff = Depends(manager_only)):
    start, end, _ = sales.resolve_range(preset, start, end)
    buf = io.StringIO()
    csv.writer(buf).writerows(sales.menu_csv_rows(sales.sales_summary(start, end)))
    filename = f"menu-sales-{start.isoformat()}-to-{end.isoformat()}.csv"
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------- staff ----------

@router.get("/staff")
def staff_page(request: Request, staff: CurrentStaff = Depends(manager_only)):
    return render(request, "staff.html", staff, people=staff_admin.list_staff())


@router.post("/staff/{staff_id}/pin")
def change_pin(request: Request, staff_id: Id, new_pin: str = Form(""), confirm_pin: str = Form(""),
               staff: CurrentStaff = Depends(manager_only)):
    result = staff_admin.change_pin(staff_id, new_pin, confirm_pin, staff.id)
    if staff_id == staff.id:  # your own PIN: keep THIS session, other devices are logged out
        request.session["pin_version"] = result["pin_version"]
    flash(request, f"PIN changed for {result['name']} (their other sessions are logged out)", kind="ok")
    return see_other(f"/staff#staff-{staff_id}")


@router.post("/staff/{staff_id}/active")
def set_active(request: Request, staff_id: Id, active: str = Form(""),
               staff: CurrentStaff = Depends(manager_only)):
    result = staff_admin.set_active(staff_id, active == "1", staff.id)
    if result["changed"]:
        state = "reactivated" if result["active"] else "deactivated (logged out on their next tap)"
        flash(request, f"{result['name']} {state}", kind="ok")
    return see_other(f"/staff#staff-{staff_id}")


# ---------- backups ----------

@router.get("/admin/backup")
def download_backup(staff: CurrentStaff = Depends(manager_only)):
    """A fresh, consistent copy of the whole database (audited). Deleted from disk once sent."""
    path, filename = backups.fresh_backup_for_download(staff.id)
    return FileResponse(path, filename=filename, media_type="application/vnd.sqlite3",
                        background=BackgroundTask(path.unlink, missing_ok=True))
