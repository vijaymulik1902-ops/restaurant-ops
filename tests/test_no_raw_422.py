"""No POST route answers staff with a raw 422: bad or missing fields always get the friendly flow."""
import re

import pytest

from app.main import app
from app.services import tables
from conftest import open_and_order
from test_routes import login


def _post_routes():
    from fastapi.routing import APIRoute

    from app.routers import auth, counter, floor, kitchen, manager, orders

    for mod in (auth, counter, floor, kitchen, manager, orders):
        for r in mod.router.routes:
            if isinstance(r, APIRoute) and "POST" in r.methods and r.path not in ("/login", "/logout"):
                yield re.sub(r"\{[^}]+\}", "1", r.path)


@pytest.mark.parametrize("path", sorted(set(_post_routes())))
@pytest.mark.parametrize("body", [{}, {"qty_1": "abc", "version": "x", "guest_count": "lots", "discount": "₹₹",
                                       "price": "abc", "amount": "-", "spent_on": "someday", "available": "?"}])
def test_post_routes_never_return_422(db, path, body):
    open_and_order(db, [("naan", 1)])
    resp = login("manager").post(path, data=body)
    assert resp.status_code not in (422, 500), (path, resp.status_code)


def test_kot_post_with_missing_or_invalid_fields_is_friendly(db):
    opened, _ = tables.open_table(db["tables"][0], db["staff"]["waiter"], 2)
    url = f"/orders/{opened['order_id']}/kot"
    c = login("waiter")
    for data in ({}, {"qty_1": "0"}, {"kot_id": "not-a-uuid", "qty_1": "1"}, {"qty_1": "abc"}):
        resp = c.post(url, data=data)
        assert resp.status_code == 200, (data, resp.status_code)
        assert 'class="flash flash-error"' in resp.text
