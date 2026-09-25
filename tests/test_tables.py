import pytest

from app.services import ServiceError, tables


def test_open_table_marks_occupied(db, clock):
    table_id = db["tables"][0]
    opened, events = tables.open_table(table_id, db["staff"]["waiter"], 3)
    clock.advance(minutes=7)
    row = next(t for t in tables.list_tables() if t["table_id"] == table_id)
    assert row["status"] == "occupied"
    assert row["order_id"] == opened["order_id"]
    assert row["waiter_name"] == "Rahul"
    assert row["minutes_in_status"] == 7
    assert {e.channel for e in events} == {"section:A", "counter"}


def test_second_open_on_occupied_table_rejected(db):
    table_id = db["tables"][0]
    tables.open_table(table_id, db["staff"]["waiter"], 2)
    with pytest.raises(ServiceError, match="occupied"):
        tables.open_table(table_id, db["staff"]["waiter2"], 4)


def test_open_table_rejects_bad_guest_count(db):
    with pytest.raises(ServiceError):
        tables.open_table(db["tables"][0], db["staff"]["waiter"], 0)


def test_list_tables_filters_by_section(db):
    assert [t["number"] for t in tables.list_tables("A")] == [1, 2]
    assert [t["number"] for t in tables.list_tables("B")] == [3]
    assert all(t["order_id"] is None for t in tables.list_tables())
