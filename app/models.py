"""Data model. Money is always integer paise (₹1 = 100 paise) to avoid rounding errors."""
from datetime import date, datetime

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, now

ROLES = ("waiter", "chef", "counter", "manager")
TABLE_STATUSES = ("available", "occupied", "billing")
ORDER_STATUSES = ("open", "billed", "paid", "cancelled")
ITEM_STATUSES = ("pending", "preparing", "ready", "served", "cancelled")
STATIONS = ("tandoor", "kitchen", "bar")
PAYMENT_MODES = ("cash", "upi", "card")
EXPENSE_CATEGORIES = ("ingredients", "salaries", "rent", "utilities", "equipment", "other")
BOOKING_STATUSES = ("booked", "seated", "completed", "cancelled", "no_show")
# Existing databases get new entries via app.migrations (SQLite cannot ALTER a CHECK constraint)
AUDIT_ENTITIES = ("order", "order_item", "bill", "menu_item", "expense", "staff", "backup", "ai", "booking")


def _in(col: str, values: tuple) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Staff(Base):
    __tablename__ = "staff"
    __table_args__ = (
        CheckConstraint(_in("role", ROLES), name="ck_staff_role"),
        CheckConstraint(_in("station", STATIONS) + " OR station IS NULL", name="ck_staff_station"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    role: Mapped[str] = mapped_column(String(10))
    pin_hash: Mapped[str] = mapped_column(String(100))
    section: Mapped[str | None] = mapped_column(String(10))  # waiters: which tables they cover
    station: Mapped[str | None] = mapped_column(String(10))  # chefs: which station they cook at
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Bumped on every PIN change; sessions remember it, so a new PIN logs out old sessions.
    # Existing databases get this column from app.migrations.ensure_schema() at startup.
    pin_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))


class DiningTable(Base):
    __tablename__ = "dining_tables"
    __table_args__ = (
        CheckConstraint(_in("status", TABLE_STATUSES), name="ck_table_status"),
        CheckConstraint("capacity > 0", name="ck_table_capacity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True)
    capacity: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(10), default="available")
    status_since: Mapped[datetime] = mapped_column(DateTime, default=now)  # drives floor timers


class MenuItem(Base):
    __tablename__ = "menu_items"
    __table_args__ = (
        CheckConstraint(_in("station", STATIONS), name="ck_menu_station"),
        CheckConstraint("price_paise >= 0", name="ck_menu_price"),
        CheckConstraint("cost_paise >= 0", name="ck_menu_cost"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    category: Mapped[str] = mapped_column(String(30))
    station: Mapped[str] = mapped_column(String(10))
    price_paise: Mapped[int] = mapped_column(Integer)
    cost_paise: Mapped[int] = mapped_column(Integer, default=0)  # approx ingredient cost per plate
    available: Mapped[bool] = mapped_column(Boolean, default=True)  # false = "86'd", can't be ordered
    # Archived = retired from the menu (instead of deleting, so history stays intact).
    # Existing databases get this column from app.migrations.ensure_schema() at startup.
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))


class Order(Base):
    """One order per table visit. Stays open while guests keep ordering."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(_in("status", ORDER_STATUSES), name="ck_order_status"),
        CheckConstraint("guest_count > 0", name="ck_order_guests"),
        # At most one open order per table, enforced by the database itself
        Index(
            "uq_open_order_per_table",
            "table_id",
            unique=True,
            sqlite_where=text("status IN ('open', 'billed')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("dining_tables.id"))
    waiter_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    guest_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(10), default="open")
    version: Mapped[int] = mapped_column(Integer, nullable=False)  # optimistic locking
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)

    table: Mapped[DiningTable] = relationship()
    waiter: Mapped[Staff] = relationship()
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", order_by="OrderItem.id")

    # SQLAlchemy bumps `version` on every UPDATE and refuses stale writes
    __mapper_args__ = {"version_id_col": version}


class Kot(Base):
    """Kitchen Order Ticket: one batch of items sent to the kitchen together.

    `id` is a UUID generated in the browser when the order form loads.
    A double-tap or retry sends the same id -> primary key blocks the duplicate.
    """

    __tablename__ = "kots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    number: Mapped[int] = mapped_column(Integer)  # human-friendly, sequential per day
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    waiter_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint(_in("status", ITEM_STATUSES), name="ck_item_status"),
        CheckConstraint(_in("station", STATIONS), name="ck_item_station"),
        CheckConstraint("qty > 0", name="ck_item_qty"),
        CheckConstraint("unit_price_paise >= 0", name="ck_item_price"),
        CheckConstraint("unit_cost_paise >= 0", name="ck_item_cost"),
        CheckConstraint(
            "status != 'cancelled' OR cancel_reason IS NOT NULL", name="ck_item_cancel_reason"
        ),
        # Kitchen board only ever scans live items, however large history grows
        Index(
            "ix_items_live_by_station",
            "station",
            "created_at",
            sqlite_where=text("status IN ('pending', 'preparing', 'ready')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    kot_id: Mapped[str] = mapped_column(ForeignKey("kots.id"), index=True)
    menu_item_id: Mapped[int] = mapped_column(ForeignKey("menu_items.id"))
    name: Mapped[str] = mapped_column(String(80))  # snapshot, so renaming a dish doesn't alter history
    station: Mapped[str] = mapped_column(String(10))  # snapshot of menu_item.station
    qty: Mapped[int] = mapped_column(Integer)
    unit_price_paise: Mapped[int] = mapped_column(Integer)  # price at time of ordering
    unit_cost_paise: Mapped[int] = mapped_column(Integer, default=0)  # cost at time of ordering
    note: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(10), default="pending")
    cancel_reason: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime)
    served_at: Mapped[datetime | None] = mapped_column(DateTime)

    order: Mapped[Order] = relationship(back_populates="items")


class Bill(Base):
    __tablename__ = "bills"
    __table_args__ = (
        CheckConstraint(_in("payment_mode", PAYMENT_MODES) + " OR payment_mode IS NULL",
                        name="ck_bill_mode"),
        CheckConstraint("discount_paise >= 0 AND discount_paise <= subtotal_paise",
                        name="ck_bill_discount"),
        CheckConstraint("total_paise = subtotal_paise - discount_paise + gst_paise",
                        name="ck_bill_math"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_no: Mapped[int] = mapped_column(Integer, unique=True)  # sequential, no gaps
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)  # one bill per order
    subtotal_paise: Mapped[int] = mapped_column(Integer)
    discount_paise: Mapped[int] = mapped_column(Integer, default=0)
    gst_percent: Mapped[int] = mapped_column(Integer)
    gst_paise: Mapped[int] = mapped_column(Integer)
    total_paise: Mapped[int] = mapped_column(Integer)
    payment_mode: Mapped[str | None] = mapped_column(String(10))
    created_by: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)  # sales reports filter on this


class Expense(Base):
    """Money spent running the restaurant ("amount invested"), entered by the manager."""

    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint(_in("category", EXPENSE_CATEGORIES), name="ck_expense_category"),
        CheckConstraint("amount_paise > 0", name="ck_expense_amount"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    spent_on: Mapped[date] = mapped_column(Date, index=True)
    category: Mapped[str] = mapped_column(String(15))
    amount_paise: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(160))
    created_by: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Booking(Base):
    """A table reservation. The floor "hold" (reserved soon) is derived from these rows at read
    time, never stored in DiningTable.status. Created by create_all() on startup if missing."""

    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint(_in("status", BOOKING_STATUSES), name="ck_booking_status"),
        CheckConstraint("party_size BETWEEN 1 AND 50", name="ck_booking_party"),
        CheckConstraint("duration_min BETWEEN 30 AND 300", name="ck_booking_duration"),
        Index("ix_bookings_starts_at", "starts_at"),
        Index("ix_bookings_table_starts", "table_id", "starts_at"),
        Index("ix_bookings_order", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    guest_name: Mapped[str] = mapped_column(String(60))
    phone: Mapped[str | None] = mapped_column(String(15))  # counter/manager only, never in events/audit/AI
    party_size: Mapped[int] = mapped_column(Integer)
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    duration_min: Mapped[int] = mapped_column(Integer, default=90)
    table_id: Mapped[int | None] = mapped_column(ForeignKey("dining_tables.id"))
    status: Mapped[str] = mapped_column(String(10), default="booked")
    note: Mapped[str | None] = mapped_column(String(120))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))  # set when seated
    created_by: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AuditLog(Base):
    """Append-only record of sensitive changes (who, when, old -> new, why).

    Written by app.services.audit.record() inside the same transaction as the change,
    so a rolled-back change never leaves an audit row. Nothing in the app updates or
    deletes rows, and the database refuses to (triggers below).
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(_in("entity", AUDIT_ENTITIES), name="ck_audit_entity"),
        Index("ix_audit_at", "at"),
        Index("ix_audit_staff_at", "staff_id", "at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    action: Mapped[str] = mapped_column(String(30))
    entity: Mapped[str] = mapped_column(String(12))
    entity_id: Mapped[int] = mapped_column(Integer)
    old_value: Mapped[str | None] = mapped_column(Text)  # JSON
    new_value: Mapped[str | None] = mapped_column(Text)  # JSON
    reason: Mapped[str | None] = mapped_column(String(120))


for _op in ("UPDATE", "DELETE"):
    event.listen(
        AuditLog.__table__,
        "after_create",
        DDL(
            f"CREATE TRIGGER IF NOT EXISTS audit_log_no_{_op.lower()} BEFORE {_op} ON audit_log "
            f"BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
        ),
    )
