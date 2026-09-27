"""App shell: each role sees exactly its tabs / sidebar items, and every link it is shown opens."""
import re

import pytest

from app.web import NAV_ITEMS, nav_for
from test_routes import ROLES, SCREENS, login

PHONE = {
    "waiter": ["Floor", "Orders", "Alerts", "Me"],
    "chef": ["Kitchen", "Summary", "Availability", "Me"],
    "counter": ["Counter", "Floor", "Me"],
    "manager": ["Home", "Floor", "Kitchen", "Reports", "More"],
}
MORE = ["Menu", "Staff", "Expenses", "Audit", "Day close", "Insights"]
LANDING = {"waiter": "/floor", "chef": "/kitchen", "counter": "/counter", "manager": "/home"}


def _labels(block: str) -> list[str]:
    return re.findall(r'<span class="label">([^<]+)</span>', block)


def _page(role: str):
    c = login(role)
    return c, c.get(LANDING[role]).text


@pytest.mark.parametrize("role", ROLES)
def test_phone_tabs_are_exactly_the_roles_items(db, role):
    _, html = _page(role)
    tabbar = html.split('<nav class="tabbar"')[1].split("</nav>")[0]
    assert _labels(tabbar) == PHONE[role]


def test_sidebar_groups_for_manager(db):
    _, html = _page("manager")
    side = html.split('<nav class="sidebar"')[1].split("</nav>")[0]
    assert re.findall(r'<div class="group">([^<]+)</div>', side) == ["Operations", "Reports", "Admin"]
    assert _labels(side) == ["Home", "Floor", "Kitchen", "Counter", "Reports", "Day close", "Insights",
                             "Menu", "Staff", "Expenses", "Audit", "Collapse"]


def test_more_sheet_lists_admin_pages_for_manager_only(db):
    _, html = _page("manager")
    sheet = html.split('id="sheet-more"')[1].split("</ul>")[0]
    for label in MORE:
        assert label in sheet
    for role in ("waiter", "chef", "counter"):
        assert 'id="sheet-more"' not in _page(role)[1]


@pytest.mark.parametrize("role", ROLES)
def test_every_shown_link_opens_for_the_role(db, role):
    """No nav item leads to a 403: the shell never advertises a page the role can't use."""
    c, html = _page(role)
    shell = html.split("<main")[0] + html.split("</main>")[1]
    hrefs = set(re.findall(r'href="(/[^"#]*)', shell)) - {"/"}
    assert hrefs, role
    for href in hrefs:
        assert c.get(href).status_code == 200, (role, href)


@pytest.mark.parametrize("role", ROLES)
def test_nav_matches_the_access_matrix(db, role):
    """Every nav page is shown to a role exactly when the access matrix lets it in, and the
    server still refuses the rest (the nav is never the guard)."""
    c, html = _page(role)
    nav = nav_for(role)
    shown = {i["href"].split("#")[0] for i in nav["tabs"] + nav["more"] + [x for _, g in nav["sidebar"] for x in g]
             if i.get("href")}
    for item in NAV_ITEMS.values():
        href = item.get("href", "").split("#")[0]
        if not href:
            continue
        allowed = role in SCREENS.get(href, {"manager"})
        assert c.get(href).status_code == (200 if allowed else 403), (role, href)
        if href in shown:
            assert allowed, (role, href)


def test_active_item_marked(db):
    c = login("manager")
    html = c.get("/menu").text
    assert 'data-nav="menu" aria-current="page"' in html
    assert 'data-nav="floor" aria-current' not in html


def test_shell_uses_inline_sprite_not_a_cdn(db):
    _, html = _page("waiter")
    assert '<symbol id="i-floor"' in html and "cdn" not in html.lower()
