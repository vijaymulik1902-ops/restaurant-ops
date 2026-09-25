"""Page loads must use a fixed number of queries, however many tables/items there are (no N+1)."""
from contextlib import contextmanager

from sqlalchemy import event

from app.db import read_engine, write_engine, write_session
from app.models import DiningTable
from app.services import kitchen, orders, tables
from conftest import new_kot_id, reset_and_seed
from test_routes import login


@contextmanager
def count_queries():
    statements: list[str] = []

    def spy(conn, cursor, statement, *args):
        if statement.lstrip().split(" ", 1)[0].upper() in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            statements.append(statement)

    for engine in (read_engine, write_engine):
        event.listen(engine, "before_cursor_execute", spy)
    try:
        yield statements
    finally:
        for engine in (read_engine, write_engine):
            event.remove(engine, "before_cursor_execute", spy)


def _busy_floor(db, table_count: int) -> int:
    """`table_count` tables in section A, every one occupied with a KOT; some items ready."""
    with write_session() as s:
        existing = len(db["tables"])
        s.add_all(DiningTable(number=100 + n, capacity=4, section="A") for n in range(table_count - existing))
    order_id = None
    for t in tables.list_tables():
        opened, _ = tables.open_table(t["table_id"], db["staff"]["waiter"], 2)
        order_id = opened["order_id"]
        orders.send_kot(order_id, new_kot_id(), db["staff"]["waiter"],
                        [(db["menu"]["naan"], 1, "n"), (db["menu"]["dal"], 1, None)])
    for item in kitchen.live_items("tandoor")[::3]:
        kitchen.start_item(item["item_id"], "tandoor")
        kitchen.ready_item(item["item_id"], "tandoor")
    return order_id


PAGES = [
    ("waiter", "/floor"), ("waiter", "/floor?all=1"), ("waiter", "/floor/board?all=1"),
    ("counter", "/counter"), ("chef", "/kitchen"), ("chef", "/kitchen/board"),
    ("manager", "/reports/day-close"), ("waiter", "/orders/{order_id}"),
]


def _counts(db, table_count: int) -> dict[str, int]:
    order_id = _busy_floor(db, table_count)
    clients = {role: login(role) for role in {r for r, _ in PAGES}}
    result = {}
    for role, path in PAGES:
        url = path.format(order_id=order_id)
        with count_queries() as statements:
            assert clients[role].get(url).status_code == 200, url
        result[f"{role} {path}"] = len(statements)
    return result


def test_queries_do_not_grow_with_table_count(db):
    small = _counts(db, 20)
    big = _counts(reset_and_seed(), 150)
    assert big == small, {k: (small[k], big[k]) for k in small if small[k] != big[k]}
    assert max(small.values()) <= 6, small  # and every page stays small in absolute terms
