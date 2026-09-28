"""Audit log: append-only record of sensitive changes. Viewing is manager-only.

record() is called INSIDE the caller's write_session, so the audit row commits or
rolls back together with the change it describes. There is deliberately no update
or delete function here (and the database refuses both, see models.AuditLog).
"""
import json
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import now, read_session
from app.models import AuditLog, Staff
from app.ordering import seniority_key
from app.services import ServiceError, business_day_bounds

ITEM_CANCEL = "item_cancel"
ORDER_CANCEL = "order_cancel"
BILL_GENERATED = "bill_generated"
BILL_DISCOUNT = "bill_discount"
BILL_PAID = "bill_paid"
AVAILABILITY = "availability"
PRICE_CHANGE = "price_change"
COST_CHANGE = "cost_change"
DISH_ADDED = "dish_added"
DISH_RENAMED = "dish_renamed"
DISH_ARCHIVED = "dish_archived"
DISH_RESTORED = "dish_restored"
PIN_CHANGED = "pin_changed"
STAFF_DEACTIVATED = "staff_deactivated"
STAFF_REACTIVATED = "staff_reactivated"
BACKUP_DOWNLOADED = "backup_downloaded"
AI_QUESTION = "ai_question"
EXPENSE_ADDED = "expense_added"
EXPENSE_DELETED = "expense_deleted"
BOOKING_CREATED = "booking_created"
BOOKING_UPDATED = "booking_updated"
BOOKING_CANCELLED = "booking_cancelled"
BOOKING_NO_SHOW = "booking_no_show"
BOOKING_SEATED = "booking_seated"
BOOKING_OVERRIDE = "booking_override"
ACTIONS = (ITEM_CANCEL, ORDER_CANCEL, BILL_GENERATED, BILL_DISCOUNT, BILL_PAID, AVAILABILITY,
           PRICE_CHANGE, COST_CHANGE, DISH_ADDED, DISH_RENAMED, DISH_ARCHIVED, DISH_RESTORED,
           EXPENSE_ADDED, EXPENSE_DELETED, PIN_CHANGED, STAFF_DEACTIVATED, STAFF_REACTIVATED,
           BACKUP_DOWNLOADED, AI_QUESTION, BOOKING_CREATED, BOOKING_UPDATED, BOOKING_CANCELLED,
           BOOKING_NO_SHOW, BOOKING_SEATED, BOOKING_OVERRIDE)

PAGE_SIZE = 50
_COST_KEYS = ("cost_paise", "unit_cost_paise")
_SECRET_KEYS = ("pin", "pin_hash", "new_pin", "password")
_PRIVATE_KEYS = ("phone",)  # guest phone numbers stay in the bookings table only


def _to_json(value: dict | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def _has_cost(value) -> bool:
    if isinstance(value, dict):
        return any(k in _COST_KEYS or _has_cost(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_cost(v) for v in value)
    return False


def record(s: Session, staff_id: int, action: str, entity: str, entity_id: int,
           old: dict | None = None, new: dict | None = None, reason: str | None = None) -> None:
    """Add one audit row to the caller's open write session (same transaction as the change).

    Cost values are only allowed when the action itself is a cost change.
    """
    if action not in ACTIONS:
        raise ValueError(f"Unknown audit action {action!r}")
    if action != COST_CHANGE and (_has_cost(old) or _has_cost(new)):
        raise ValueError(f"Audit action {action!r} must not carry cost values")
    if any(k in _SECRET_KEYS for v in (old or {}, new or {}) for k in v):
        raise ValueError("Audit rows must never contain a PIN or its hash")
    if any(k in _PRIVATE_KEYS for v in (old or {}, new or {}) for k in v):
        raise ValueError("Audit rows must never contain a guest phone number")
    s.add(AuditLog(at=now(), staff_id=staff_id, action=action, entity=entity, entity_id=entity_id,
                   old_value=_to_json(old), new_value=_to_json(new), reason=reason))


def _highlight(action: str, old: dict | None, new: dict | None) -> str | None:
    """Why a row deserves a second look (shown in red on /audit), or None."""
    old, new = old or {}, new or {}
    if action == ITEM_CANCEL and old.get("status") == "ready":
        return "Cancelled after it was ready"
    if action == ORDER_CANCEL and "ready" in (old.get("item_statuses") or {}).values():
        return "Order cancelled with food ready"
    if action == BILL_DISCOUNT and (new.get("percent") or 0) > 10:
        return "Discount above 10%"
    if action == PRICE_CHANGE:
        return "Price changed"
    if action == BOOKING_OVERRIDE:
        return "Walk-in seated at a reserved table"
    return None


def list_audit(date_from: date | None = None, date_to: date | None = None, staff_id: int | None = None,
               action: str | None = None, page: int = 1) -> dict:
    """Audit rows newest first, 50 per page, filtered by business-day range, staff and action.

    Manager-only (the router enforces it): cost_change rows carry cost values.
    """
    if action and action not in ACTIONS:
        raise ServiceError("Unknown action")
    if date_from and date_to and date_from > date_to:
        raise ServiceError("'From' date is after 'to' date")
    for d in (date_from, date_to):
        if d and not 2000 <= d.year <= 2100:
            raise ServiceError("Pick dates between 2000 and 2100")

    filters = []
    if date_from:
        filters.append(AuditLog.at >= business_day_bounds(date_from)[0])
    if date_to:
        filters.append(AuditLog.at < business_day_bounds(date_to)[1])
    if staff_id:
        filters.append(AuditLog.staff_id == staff_id)
    if action:
        filters.append(AuditLog.action == action)

    with read_session() as s:
        total = s.scalar(select(func.count(AuditLog.id)).where(*filters))
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(1, page), pages)
        rows = s.execute(
            select(AuditLog.id, AuditLog.at, AuditLog.action, AuditLog.entity, AuditLog.entity_id,
                   AuditLog.old_value, AuditLog.new_value, AuditLog.reason, Staff.name.label("staff_name"))
            .join(Staff, Staff.id == AuditLog.staff_id)
            .where(*filters)
            .order_by(AuditLog.at.desc(), AuditLog.id.desc())
            .limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
        ).all()
        staff = sorted(s.execute(select(Staff.id, Staff.name, Staff.role, Staff.station, Staff.section)).all(),
                       key=lambda x: seniority_key(x.role, x.station, x.section, x.name))

    out = []
    for r in rows:
        old = json.loads(r.old_value) if r.old_value else None
        new = json.loads(r.new_value) if r.new_value else None
        out.append({"id": r.id, "at": r.at, "staff_name": r.staff_name, "action": r.action,
                    "entity": r.entity, "entity_id": r.entity_id, "old": old, "new": new,
                    "reason": r.reason, "highlight": _highlight(r.action, old, new)})
    return {"rows": out, "page": page, "pages": pages, "total": total,
            "actions": ACTIONS, "staff_options": [{"id": x.id, "name": x.name} for x in staff]}
