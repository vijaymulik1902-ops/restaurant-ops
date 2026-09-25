"""Staff management: MANAGER ONLY. Change PINs, deactivate/reactivate staff. Audited.

PIN rules: digits only; 6 digits for managers, 4 for everyone else. PINs are stored only as
bcrypt hashes and never written to the audit log or any other log.
"""
import bcrypt
from sqlalchemy import func, select

from app.auth import limiter, pin_length_for
from app.db import read_session, write_session
from app.models import Staff
from app.services import ServiceError, audit
from app.services.tables import get_active_staff

def hash_pin(pin: str) -> str:
    return bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()


def _require_manager(s, staff_id: int) -> Staff:
    staff = get_active_staff(s, staff_id)
    if staff.role != "manager":
        raise ServiceError("Only a manager can manage staff")
    return staff


def list_staff() -> list[dict]:
    """Everyone, active first, with role and section/station (never PIN data)."""
    with read_session() as s:
        rows = s.execute(select(Staff.id, Staff.name, Staff.role, Staff.section, Staff.station, Staff.active)
                         .order_by(Staff.active.desc(), Staff.role, Staff.name)).all()
    return [{"staff_id": r.id, "name": r.name, "role": r.role, "section": r.section, "station": r.station,
             "active": r.active, "pin_length": pin_length_for(r.role)} for r in rows]


def change_pin(staff_id: int, new_pin: str, confirm_pin: str, by_staff_id: int) -> dict:
    """Set a new PIN for anyone (including yourself). Audited as pin_changed, without the PIN."""
    new_pin = (new_pin or "").strip()
    with write_session() as s:
        _require_manager(s, by_staff_id)
        target = s.get(Staff, staff_id)
        if target is None:
            raise ServiceError("Staff member not found")
        length = pin_length_for(target.role)
        if not (new_pin.isascii() and new_pin.isdigit() and len(new_pin) == length):
            raise ServiceError(f"{target.name}'s PIN must be exactly {length} digits")
        if len(set(new_pin)) == 1:
            raise ServiceError("Pick a PIN that isn't one digit repeated")
        if new_pin != (confirm_pin or "").strip():
            raise ServiceError("The two PINs don't match")
        audit.record(s, by_staff_id, audit.PIN_CHANGED, "staff", target.id,
                     new={"name": target.name, "role": target.role})
        target.pin_hash = hash_pin(new_pin)
        name = target.name
    limiter.reset(staff_id)  # a fresh PIN also clears any wrong-PIN lockout
    return {"staff_id": staff_id, "name": name}


def set_active(staff_id: int, active: bool, by_staff_id: int) -> dict:
    """Deactivate (logged out on their next tap, can't log in) or reactivate a staff member."""
    active = bool(active)
    with write_session() as s:
        manager = _require_manager(s, by_staff_id)
        target = s.get(Staff, staff_id)
        if target is None:
            raise ServiceError("Staff member not found")
        if target.active == active:
            return {"staff_id": target.id, "name": target.name, "active": active, "changed": False}
        if not active:
            if target.id == manager.id:
                raise ServiceError("You can't deactivate yourself")
            if target.role == "manager":
                others = s.scalar(select(func.count(Staff.id)).where(
                    Staff.role == "manager", Staff.active.is_(True), Staff.id != target.id))
                if not others:
                    raise ServiceError("Keep at least one active manager")
        audit.record(s, by_staff_id, audit.STAFF_REACTIVATED if active else audit.STAFF_DEACTIVATED,
                     "staff", target.id, old={"active": target.active, "name": target.name},
                     new={"active": active})
        target.active = active
        return {"staff_id": target.id, "name": target.name, "active": active, "changed": True}
