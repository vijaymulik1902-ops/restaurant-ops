"""Guards for the UI rules: no XSS escape hatches, motion is opt-out, empty states direct action."""
import re
from pathlib import Path

from test_routes import login

APP = Path(__file__).resolve().parent.parent / "app"


def test_no_safe_filter_or_innerhtml():
    for tpl in (APP / "templates").glob("*.html"):
        assert "|safe" not in tpl.read_text().replace(" ", ""), tpl.name
    for js in (APP / "static").glob("*.js"):
        if js.name.endswith(".min.js"):
            continue  # vendored libraries
        assert not re.search(r"\.innerHTML\s*=", js.read_text()), js.name


def test_all_motion_is_disabled_under_reduced_motion():
    css = (APP / "static" / "style.css").read_text()
    block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert "animation: none !important" in block and "transition: none !important" in block
    # motion only animates transform/opacity: check every @keyframes block (matched by braces)
    blocks = 0
    for m in re.finditer(r"@keyframes [\w-]+\s*\{", css):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        props = set(re.findall(r"([a-z-]+)\s*:", css[m.end():i - 1]))
        assert props <= {"transform", "opacity"}, props
        blocks += 1
    assert blocks >= 4
    js = (APP / "static" / "sales.js").read_text()
    assert "prefers-reduced-motion" in js


def test_design_tokens_defined():
    css = (APP / "static" / "style.css").read_text()
    for token in ("--ink:", "--turmeric:", "--chilli:", "--cardamom:", "--steel:", "--paper:"):
        assert token in css, token


def test_kitchen_empty_state_directs_action(db):
    html = login("chef").get("/kitchen").text
    assert "No tickets yet." in html and "New orders appear here instantly." in html


def test_order_page_send_bar_has_summary_slot_and_prices(db):
    from app.services import tables

    opened, _ = tables.open_table(db["tables"][0], db["staff"]["waiter"], 2)
    html = login("waiter").get(f"/orders/{opened['order_id']}").text
    assert 'id="send-count"' in html and 'class="send-label"' in html
    assert re.search(r'data-price="4500"', html)  # display-only total; the server prices the KOT


def test_dim_kitchen_toggle_only_on_chef_screens(db):
    chef = login("chef")
    for url in ("/kitchen", "/kitchen/availability"):
        assert "data-dim-toggle" in chef.get(url).text, url
    assert "data-dim-toggle" not in login("waiter").get("/floor").text
    base = (APP / "templates" / "base.html").read_text()
    assert "localStorage.getItem('dimKitchen')" in base and "try {" in base   # remembered, guarded
    assert "(pg === 'kitchen' || pg === 'availability')" in base               # chef screens only
    css = (APP / "static" / "style.css").read_text()
    assert ':root[data-theme="dark"] {' in css and "--ink: #151b22;" in css
