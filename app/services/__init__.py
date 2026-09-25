"""Service layer: all business rules live here.

Conventions:
- Each public function is one transaction (its own write_session / read_session).
- Rule violations raise ServiceError(message); the message is safe to show to staff.
- Functions that change data return `(result, events)`. The caller publishes the
  events only after the function has returned, i.e. after the commit.
- Results are plain dicts, never ORM objects, and never contain cost fields.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.config import BUSINESS_DAY_START_HOUR

MAX_REASON_LEN = 120


class ServiceError(Exception):
    """A business rule was violated. `message` is shown to the user."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Event:
    """A small live update for one SSE channel (see CLAUDE.md for channel names)."""

    channel: str
    type: str
    data: dict = field(default_factory=dict)


def minutes_since(start, current) -> int:
    """Whole minutes elapsed between two naive datetimes, never negative."""
    if start is None:
        return 0
    return max(0, int((current - start).total_seconds() // 60))


def business_day_bounds(d: date) -> tuple[datetime, datetime]:
    """[start, end) of business day `d`: d at BUSINESS_DAY_START_HOUR to the same hour next day."""
    start = datetime.combine(d, time(hour=BUSINESS_DAY_START_HOUR))
    return start, start + timedelta(days=1)


def business_day_of(ts: datetime) -> date:
    """The business day a timestamp belongs to (00:15 belongs to the previous evening)."""
    return (ts - timedelta(hours=BUSINESS_DAY_START_HOUR)).date()


def clean_reason(reason: str | None) -> str:
    """A non-empty, trimmed cancel reason, or ServiceError."""
    reason = (reason or "").strip()
    if not reason:
        raise ServiceError("A reason is required to cancel")
    if len(reason) > MAX_REASON_LEN:
        raise ServiceError(f"Reason is too long (max {MAX_REASON_LEN} characters)")
    return reason


MAX_REPORT_DAYS = 366


def validate_range(start: date, end: date) -> None:
    """Report date ranges: end not before start, at most 366 days, sane years."""
    if not isinstance(start, date) or not isinstance(end, date):
        raise ServiceError("Pick a start and end date")
    if end < start:
        raise ServiceError("The end date is before the start date")
    if (end - start).days + 1 > MAX_REPORT_DAYS:
        raise ServiceError(f"Pick a range of at most {MAX_REPORT_DAYS} days")
    if start.year < 2000 or end.year > 2100:
        raise ServiceError("Pick dates between 2000 and 2100")
