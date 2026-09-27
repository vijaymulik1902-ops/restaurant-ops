"""Expenses ("amount invested"): manager only. Every add/delete is audited."""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import now, read_session, write_session
from app.models import EXPENSE_CATEGORIES, Expense, Staff
from app.services import ServiceError, audit, validate_range
from app.services.tables import get_active_staff

MAX_EXPENSE_PAISE = 10**10  # ₹10 crore: anything bigger is a typo
MAX_NOTE_LEN = 160


def latest_allowed_date() -> date:
    """Expenses can be dated up to today (calendar date), never in the future."""
    return now().date()


def _require_manager(s: Session, staff_id: int) -> None:
    if get_active_staff(s, staff_id).role != "manager":
        raise ServiceError("Only a manager can manage expenses")


def _snapshot(e: Expense) -> dict:
    return {"spent_on": e.spent_on.isoformat(), "category": e.category,
            "amount_paise": e.amount_paise, "note": e.note}


def add_expense(spent_on: date, category: str, amount_paise: int, note: str | None,
                by_staff_id: int) -> dict:
    """Record money spent. Returns the new expense."""
    if not isinstance(spent_on, date):
        raise ServiceError("Pick the date the money was spent")
    if spent_on > latest_allowed_date():
        raise ServiceError("An expense can't be dated in the future")
    if spent_on.year < 2000:
        raise ServiceError("Pick a date after 2000")
    if category not in EXPENSE_CATEGORIES:
        raise ServiceError("Choose a category: " + ", ".join(EXPENSE_CATEGORIES))
    if not isinstance(amount_paise, int) or isinstance(amount_paise, bool) or not 0 < amount_paise <= MAX_EXPENSE_PAISE:
        raise ServiceError("Amount must be more than zero")
    note = (note or "").strip() or None
    if note and len(note) > MAX_NOTE_LEN:
        raise ServiceError(f"Note is too long (max {MAX_NOTE_LEN} characters)")

    with write_session() as s:
        _require_manager(s, by_staff_id)
        e = Expense(spent_on=spent_on, category=category, amount_paise=amount_paise, note=note,
                    created_by=by_staff_id, created_at=now())
        s.add(e)
        s.flush()
        audit.record(s, by_staff_id, audit.EXPENSE_ADDED, "expense", e.id, new=_snapshot(e))
        return {"expense_id": e.id, **_snapshot(e)}


def delete_expense(expense_id: int, reason: str | None, by_staff_id: int) -> None:
    """Remove a wrongly entered expense. The audit row keeps what it was."""
    with write_session() as s:
        _require_manager(s, by_staff_id)
        e = s.get(Expense, expense_id)
        if e is None:
            raise ServiceError("Expense not found")
        audit.record(s, by_staff_id, audit.EXPENSE_DELETED, "expense", e.id, old=_snapshot(e),
                     reason=(reason or "").strip()[:120] or None)
        s.delete(e)


def list_expenses(start: date, end: date) -> dict:
    """Expenses with spent_on in [start, end] (inclusive), newest first, plus totals by category."""
    validate_range(start, end)
    with read_session() as s:
        rows = s.execute(
            select(Expense.id, Expense.spent_on, Expense.category, Expense.amount_paise, Expense.note,
                   Staff.name.label("created_by"))
            .join(Staff, Staff.id == Expense.created_by)
            .where(Expense.spent_on >= start, Expense.spent_on <= end)
            .order_by(Expense.spent_on.desc(), Expense.id.desc())
        ).all()
        by_cat = dict(s.execute(
            select(Expense.category, func.sum(Expense.amount_paise))
            .where(Expense.spent_on >= start, Expense.spent_on <= end)
            .group_by(Expense.category)
        ).all())
    return {
        "start": start, "end": end,
        "rows": [{"expense_id": r.id, "spent_on": r.spent_on, "category": r.category,
                  "amount_paise": r.amount_paise, "note": r.note, "created_by": r.created_by} for r in rows],
        "by_category": {c: by_cat.get(c, 0) for c in EXPENSE_CATEGORIES},
        # Split the way the profit report uses them: operating costs vs food (already in COGS)
        "operating_by_category": {c: by_cat.get(c, 0) for c in EXPENSE_CATEGORIES if c != "ingredients"},
        "operating_total_paise": sum(v for c, v in by_cat.items() if c != "ingredients"),
        "ingredients_total_paise": by_cat.get("ingredients", 0),
        "total_paise": sum(by_cat.values()),
    }
