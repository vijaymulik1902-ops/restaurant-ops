"""Audit log: one row per audited action, written in the SAME transaction as the change."""
from datetime import date

import pytest
from sqlalchemy import func, select, text

from app.db import read_session, write_engine, write_session
from app.models import AuditLog, Expense, MenuItem
from app.services import audit, billing, expenses, kitchen, menu, menu_admin, orders, tables
from conftest import open_and_order, serve_all


def _rows(action=None) -> list[AuditLog]:
    with read_session() as s:
        stmt = select(AuditLog).order_by(AuditLog.id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        rows = list(s.scalars(stmt))
        s.expunge_all()
    return rows


def _count() -> int:
    with read_session() as s:
        return s.scalar(select(func.count(AuditLog.id)))


# Each case: (action, setup(db) -> ctx, act(db, ctx), unchanged(db, ctx) -> bool)
def _pending_item(db):
    kot = open_and_order(db, [("naan", 1)])
    return {"order_id": kot["order_id"], "item_id": orders.get_order(kot["order_id"])["items"][0]["item_id"]}


def _billable(db):
    kot = open_and_order(db, [("naan", 2)])
    serve_all(db, kot["order_id"])
    return {"order_id": kot["order_id"]}


def _billed(db):
    ctx = _billable(db)
    bill, _ = billing.generate_bill(ctx["order_id"], 0, db["staff"]["counter"])
    ctx["bill_id"] = bill["bill_id"]
    return ctx


def _expense(db):
    return expenses.add_expense(date(2026, 9, 25), "rent", 500000, "Sept", db["staff"]["manager"])


def _dish(db, name=None):
    with read_session() as s:
        return s.get(MenuItem, db["menu"]["naan"]).__dict__.copy()


CASES = {
    audit.ITEM_CANCEL: (
        _pending_item,
        lambda db, c: kitchen.cancel_item(c["item_id"], "Out of stock", db["staff"]["waiter"]),
        lambda db, c: orders.get_order(c["order_id"])["items"][0]["status"] == "pending",
    ),
    audit.ORDER_CANCEL: (
        _pending_item,
        lambda db, c: orders.cancel_order(c["order_id"], "Guests left", db["staff"]["manager"]),
        lambda db, c: orders.get_order(c["order_id"])["status"] == "open",
    ),
    audit.BILL_GENERATED: (
        _billable,
        lambda db, c: billing.generate_bill(c["order_id"], 0, db["staff"]["counter"]),
        lambda db, c: orders.get_order(c["order_id"])["bill_id"] is None,
    ),
    audit.BILL_DISCOUNT: (
        _billable,
        lambda db, c: billing.generate_bill(c["order_id"], 500, db["staff"]["counter"]),
        lambda db, c: orders.get_order(c["order_id"])["bill_id"] is None,
    ),
    audit.BILL_PAID: (
        _billed,
        lambda db, c: billing.pay_bill(c["bill_id"], "upi", db["staff"]["counter"]),
        lambda db, c: billing.get_bill(c["bill_id"])["paid_at"] is None,
    ),
    audit.AVAILABILITY: (
        lambda db: {},
        lambda db, c: menu.set_available(db["menu"]["naan"], False, db["staff"]["chef_tandoor"]),
        lambda db, c: _dish(db)["available"] is True,
    ),
    audit.PRICE_CHANGE: (
        lambda db: {},
        lambda db, c: menu_admin.set_price(db["menu"]["naan"], 5000, db["staff"]["manager"]),
        lambda db, c: _dish(db)["price_paise"] == 4500,
    ),
    audit.COST_CHANGE: (
        lambda db: {},
        lambda db, c: menu_admin.set_cost(db["menu"]["naan"], 1500, db["staff"]["manager"]),
        lambda db, c: _dish(db)["cost_paise"] == 1200,
    ),
    audit.DISH_ADDED: (
        lambda db: {},
        lambda db, c: menu_admin.add_dish("Tandoori Roti", "Breads", "tandoor", 2500, 600, db["staff"]["manager"]),
        lambda db, c: all(d["name"] != "Tandoori Roti" for d in menu.list_menu()),
    ),
    audit.DISH_RENAMED: (
        lambda db: {},
        lambda db, c: menu_admin.rename_dish(db["menu"]["naan"], "Makhani Naan", db["staff"]["manager"]),
        lambda db, c: _dish(db)["name"] == "Butter Naan",
    ),
    audit.DISH_ARCHIVED: (
        lambda db: {},
        lambda db, c: menu_admin.set_archived(db["menu"]["naan"], True, db["staff"]["manager"]),
        lambda db, c: _dish(db)["archived"] is False,
    ),
    audit.DISH_RESTORED: (
        lambda db: menu_admin.set_archived(db["menu"]["naan"], True, db["staff"]["manager"]),
        lambda db, c: menu_admin.set_archived(db["menu"]["naan"], False, db["staff"]["manager"]),
        lambda db, c: _dish(db)["archived"] is True,
    ),
    audit.EXPENSE_ADDED: (
        lambda db: {},
        lambda db, c: _expense(db),
        lambda db, c: _expense_count() == 0,
    ),
    audit.EXPENSE_DELETED: (
        lambda db: _expense(db),
        lambda db, c: expenses.delete_expense(c["expense_id"], "typo", db["staff"]["manager"]),
        lambda db, c: _expense_count() == 1,
    ),
}


def _expense_count() -> int:
    with read_session() as s:
        return s.scalar(select(func.count(Expense.id)))


@pytest.mark.parametrize("action", list(CASES))
def test_each_audited_action_writes_exactly_one_row(db, action):
    setup, act, _ = CASES[action]
    ctx = setup(db)
    before = len(_rows(action))
    act(db, ctx)
    rows = _rows(action)
    assert len(rows) == before + 1
    row = rows[-1]
    assert row.staff_id is not None and row.at is not None and row.entity in (
        "order", "order_item", "bill", "menu_item", "expense")


@pytest.mark.parametrize("action", list(CASES))
def test_rollback_leaves_no_audit_row_and_no_change(db, action, monkeypatch):
    """If anything fails after the audit row is added, BOTH the change and the row roll back."""
    setup, act, unchanged = CASES[action]
    ctx = setup(db)
    before = _count()
    real_record = audit.record

    def record_then_fail(s, *args, **kwargs):
        real_record(s, *args, **kwargs)
        s.flush()  # the row really is in the transaction...
        raise RuntimeError("simulated crash after audit write")  # ...then the transaction dies

    monkeypatch.setattr(audit, "record", record_then_fail)
    with pytest.raises(RuntimeError, match="simulated crash"):
        act(db, ctx)
    assert _count() == before
    assert unchanged(db, ctx)


def test_bill_without_discount_has_no_discount_row(db):
    ctx = _billable(db)
    billing.generate_bill(ctx["order_id"], 0, db["staff"]["counter"])
    assert len(_rows(audit.BILL_GENERATED)) == 1 and _rows(audit.BILL_DISCOUNT) == []


@pytest.mark.parametrize("action", list(CASES))
def test_audit_rows_carry_no_cost_except_cost_changes(db, action):
    setup, act, _ = CASES[action]
    act(db, setup(db))
    for row in _rows():
        payload = (row.old_value or "") + (row.new_value or "")
        if row.action == audit.COST_CHANGE:
            assert "cost_paise" in payload
        else:
            assert "cost" not in payload, (row.action, payload)


def test_record_refuses_cost_in_non_cost_action(db):
    with write_session() as s, pytest.raises(ValueError, match="cost"):
        audit.record(s, db["staff"]["manager"], audit.PRICE_CHANGE, "menu_item", 1,
                     old={"price_paise": 1, "cost_paise": 2})


def test_order_cancel_is_one_row_even_with_many_items(db):
    kot = open_and_order(db, [("naan", 1), ("dal", 1), ("lassi", 1)])
    orders.cancel_order(kot["order_id"], "Guests left", db["staff"]["manager"])
    assert len(_rows(audit.ORDER_CANCEL)) == 1 and _rows(audit.ITEM_CANCEL) == []


def test_no_op_toggle_or_same_price_writes_nothing(db):
    menu.set_available(db["menu"]["naan"], True, db["staff"]["manager"])  # already available
    menu_admin.set_price(db["menu"]["naan"], 4500, db["staff"]["manager"])  # same price
    assert _count() == 0


def test_audit_log_is_append_only_in_the_database(db):
    open_and_order(db, [("naan", 1)])
    item_id = next(i for i in kitchen.live_items("tandoor"))["item_id"]
    kitchen.cancel_item(item_id, "Out of stock", db["staff"]["waiter"])
    with write_engine.connect() as conn:
        for sql in ("UPDATE audit_log SET reason = 'edited'", "DELETE FROM audit_log"):
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text(sql))
            conn.rollback()
    assert _count() == 1


def test_no_code_updates_or_deletes_audit_rows():
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    for path in app_dir.rglob("*.py"):
        src = path.read_text()
        for bad in ("update(AuditLog", "delete(AuditLog", "UPDATE audit_log", "DELETE FROM audit_log"):
            assert bad not in src, f"{path.name}: {bad}"


def test_list_audit_filters_highlights_and_pages(db, clock):
    # A ready item cancelled (highlight), a >10% discount (highlight), a price change (highlight)
    kot = open_and_order(db, [("naan", 1), ("dal", 1)])
    naan, dal = [i["item_id"] for i in orders.get_order(kot["order_id"])["items"]]
    kitchen.start_item(naan, "tandoor")
    kitchen.ready_item(naan, "tandoor")
    kitchen.cancel_item(naan, "Burnt", db["staff"]["manager"])
    serve_all(db, kot["order_id"])
    billing.generate_bill(kot["order_id"], 5000, db["staff"]["manager"])  # 5000/18000 = 27.8%
    menu_admin.set_price(db["menu"]["lassi"], 8000, db["staff"]["manager"])

    result = audit.list_audit()
    flags = {r["action"]: r["highlight"] for r in result["rows"]}
    assert flags[audit.ITEM_CANCEL] == "Cancelled after it was ready"
    assert flags[audit.BILL_DISCOUNT] == "Discount above 10%"
    assert flags[audit.PRICE_CHANGE] == "Price changed"
    assert flags[audit.BILL_GENERATED] is None
    assert [r["id"] for r in result["rows"]] == sorted((r["id"] for r in result["rows"]), reverse=True)

    assert {r["action"] for r in audit.list_audit(action=audit.PRICE_CHANGE)["rows"]} == {audit.PRICE_CHANGE}
    assert audit.list_audit(staff_id=db["staff"]["waiter"])["total"] == 0
    assert audit.list_audit(date_from=date(2026, 9, 26))["total"] == 0
    assert audit.list_audit(date_from=date(2026, 9, 25), date_to=date(2026, 9, 25))["total"] == result["total"]


def test_list_audit_paginates_50_per_page(db):
    with write_session() as s:
        for i in range(120):
            audit.record(s, db["staff"]["manager"], audit.AVAILABILITY, "menu_item", i + 1,
                         old={"available": True}, new={"available": False})
    first, third = audit.list_audit(page=1), audit.list_audit(page=3)
    assert (first["total"], first["pages"], len(first["rows"]), len(third["rows"])) == (120, 3, 50, 20)
    assert first["rows"][0]["entity_id"] == 120  # newest first
    assert audit.list_audit(page=99)["page"] == 3  # clamped
