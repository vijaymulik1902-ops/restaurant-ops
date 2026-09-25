"""Part C: random PINs, per-IP login limit, staff management, backups, deployment files."""
import json
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from app import auth
from app import seed as seed_module
from app.config import DB_PATH
from app.db import read_session, write_session
from app.main import app
from app.models import AuditLog, Staff
from app.services import ServiceError, audit, backups, staff_admin
from conftest import PIN_HASH, TEST_PIN
from test_routes import csrf_from, login

ROOT = Path(__file__).resolve().parent.parent


# ---------- C1: random PINs ----------

@pytest.fixture
def fast_bcrypt(monkeypatch):
    import bcrypt

    monkeypatch.setattr(seed_module, "_hash", lambda pin: bcrypt.hashpw(pin.encode(), bcrypt.gensalt(4)).decode())


def _printed_pins(out: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(r"^(\w+)\s+\w+\s+(?:[\w -]+?\s+)?(\d{4,6})$", out, re.M)}


def test_random_pins_printed_once_and_only_hashed(db, fast_bcrypt, capsys):
    seed_module.seed(table_count=5, reset=True, random_pins=True)
    pins = _printed_pins(capsys.readouterr().out)
    assert len(pins) == 12
    assert len(pins["Manager"]) == 6 and all(len(p) == 4 for n, p in pins.items() if n != "Manager")
    assert pins["Rahul"] != "1111" and pins["Manager"] != "9191"
    with read_session() as s:
        hashes = {st.name: st.pin_hash for st in s.scalars(select(Staff))}
    assert all(h.startswith("$2b$") and pins[n] not in h for n, h in hashes.items())
    assert auth.login("Manager", pins["Manager"]).role == "manager"
    assert auth.login("Rahul", pins["Rahul"]).name == "Rahul"


def test_random_pins_on_existing_data_rotates_everyone(db, fast_bcrypt, capsys, monkeypatch):
    seed_module.seed(table_count=5, reset=True, random_pins=True)
    first = _printed_pins(capsys.readouterr().out)
    monkeypatch.setattr(seed_module, "_random_pin", lambda role: "135790" if role == "manager" else "2468")
    seed_module.seed(table_count=5, reset=False, random_pins=True)
    second = _printed_pins(capsys.readouterr().out)
    assert set(second) == set(first) and second["Manager"] == "135790" and second["Rahul"] == "2468"
    assert auth.login("Manager", "135790").role == "manager"
    if first["Manager"] != "135790":
        with pytest.raises(auth.AuthError):
            auth.login("Manager", first["Manager"])


def test_random_pin_never_one_repeated_digit():
    assert all(len(set(seed_module._random_pin(role))) > 1 for role in ("waiter", "manager") for _ in range(500))


# ---------- C1: per-IP login limit ----------

def _login_attempt(c: TestClient, pin: str = "0000"):
    return c.post("/login", data={"name": "Rahul", "pin": pin})


def test_ip_limit_20_attempts_per_10_minutes(db, monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(auth, "_clock", lambda: t["now"])
    c = TestClient(app, follow_redirects=False)
    c.headers["X-CSRF-Token"] = csrf_from(c.get("/login").text)
    names = ["Rahul", "Sneha", "Counter", "Manager", "Suresh", "Mahesh"]
    for i in range(20):  # spread over several names so the per-staff lockout isn't what stops it
        assert c.post("/login", data={"name": names[i % 6], "pin": "0000"}).status_code == 401
    blocked = c.post("/login", data={"name": "Sneha", "pin": TEST_PIN})  # even the right PIN
    assert blocked.status_code == 429 and "Too many login attempts from this network" in blocked.text
    t["now"] += auth.LOGIN_IP_WINDOW_SEC
    assert c.post("/login", data={"name": "Sneha", "pin": TEST_PIN}).status_code == 303


def test_ip_limit_is_per_ip():
    limiter = auth._IpLimiter()
    for _ in range(auth.LOGIN_IP_LIMIT):
        assert limiter.attempt("10.0.0.1") == 0
    assert limiter.attempt("10.0.0.1") > 0
    assert limiter.attempt("10.0.0.2") == 0


def test_per_staff_lockout_still_applies_under_the_ip_limit(db):
    c = TestClient(app, follow_redirects=False)
    c.headers["X-CSRF-Token"] = csrf_from(c.get("/login").text)
    for _ in range(5):
        _login_attempt(c)
    resp = _login_attempt(c, TEST_PIN)
    assert resp.status_code == 401 and "Too many wrong PINs" in resp.text


def test_login_page_knows_each_roles_pin_length(db):
    html = TestClient(app).get("/login").text
    assert re.search(r'value="Manager" data-pin-length="6"', html)
    assert re.search(r'value="Rahul" data-pin-length="4"', html)
    assert 'pattern="[0-9]{4,8}"' in html and 'maxlength="8"' in html


# ---------- C1: /staff ----------

def _audit_rows(action):
    with read_session() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action == action)))
        s.expunge_all()
    return rows


def test_staff_page_is_manager_only(db):
    page = login("manager").get("/staff")
    assert page.status_code == 200 and 'href="/admin/backup"' in page.text
    for role in ("waiter", "chef", "counter"):
        c = login(role)
        assert c.get("/staff").status_code == 403
        assert c.post(f"/staff/{db['staff']['waiter']}/pin", data={"new_pin": "4821", "confirm_pin": "4821"}).status_code == 403
        assert c.post(f"/staff/{db['staff']['waiter']}/active", data={"active": "0"}).status_code == 403


def test_change_pin_works_and_is_audited_without_the_pin(db):
    c = login("manager")
    resp = c.post(f"/staff/{db['staff']['waiter']}/pin", data={"new_pin": "4821", "confirm_pin": "4821"})
    assert resp.status_code == 303 and "PIN changed for Rahul" in c.get("/staff").text
    assert auth.login("Rahul", "4821").name == "Rahul"
    with pytest.raises(auth.AuthError):
        auth.login("Rahul", TEST_PIN)
    rows = _audit_rows(audit.PIN_CHANGED)
    assert len(rows) == 1 and rows[0].entity == "staff" and rows[0].entity_id == db["staff"]["waiter"]
    stored = (rows[0].old_value or "") + (rows[0].new_value or "") + (rows[0].reason or "")
    assert "4821" not in stored and "pin" not in json.loads(rows[0].new_value)


@pytest.mark.parametrize("who, new, confirm, match", [
    ("waiter", "12345", "12345", "exactly 4 digits"),
    ("manager", "4821", "4821", "exactly 6 digits"),
    ("waiter", "12a4", "12a4", "exactly 4 digits"),
    ("waiter", "7777", "7777", "one digit repeated"),
    ("waiter", "4821", "4822", "don't match"),
])
def test_change_pin_rules(db, who, new, confirm, match):
    with pytest.raises(ServiceError, match=match):
        staff_admin.change_pin(db["staff"][who], new, confirm, db["staff"]["manager"])
    assert _audit_rows(audit.PIN_CHANGED) == []


def test_change_pin_clears_wrong_pin_lockout(db):
    for _ in range(5):
        with pytest.raises(auth.AuthError):
            auth.login("Rahul", "0000")
    staff_admin.change_pin(db["staff"]["waiter"], "4821", "4821", db["staff"]["manager"])
    assert auth.login("Rahul", "4821").name == "Rahul"


def test_deactivate_logs_out_and_reactivate_restores(db):
    waiter, manager = login("waiter"), login("manager")
    assert waiter.get("/floor").status_code == 200
    manager.post(f"/staff/{db['staff']['waiter']}/active", data={"active": "0"})
    assert waiter.get("/floor").headers["location"] == "/login"
    with pytest.raises(auth.AuthError):
        auth.login("Rahul", TEST_PIN)
    manager.post(f"/staff/{db['staff']['waiter']}/active", data={"active": "1"})
    assert auth.login("Rahul", TEST_PIN).name == "Rahul"
    assert [r.action for r in _audit_rows(audit.STAFF_DEACTIVATED) + _audit_rows(audit.STAFF_REACTIVATED)] == [
        audit.STAFF_DEACTIVATED, audit.STAFF_REACTIVATED]


def test_manager_cannot_deactivate_themselves_but_can_deactivate_another_manager(db):
    m = db["staff"]["manager"]
    with pytest.raises(ServiceError, match="yourself"):
        staff_admin.set_active(m, False, m)
    with write_session() as s:
        s.add(Staff(name="Owner", role="manager", pin_hash=PIN_HASH))
    with read_session() as s:
        owner = s.scalar(select(Staff.id).where(Staff.name == "Owner"))
    staff_admin.set_active(owner, False, m)
    # Since nobody can deactivate themselves, an active manager always remains
    with pytest.raises(ServiceError):  # and an inactive manager can't act at all
        staff_admin.set_active(m, False, owner)


def test_audit_refuses_pins():
    with pytest.raises(ValueError, match="PIN"):
        with write_session() as s:
            audit.record(s, 1, audit.PIN_CHANGED, "staff", 1, new={"pin": "1234"})


# ---------- C2: backups ----------

def _row_counts(path: str) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


def test_backup_opens_with_same_row_counts(db, tmp_path):
    from app.services import kitchen
    from conftest import open_and_order

    open_and_order(db, [("naan", 2), ("dal", 1)])
    item_id = kitchen.live_items("tandoor")[0]["item_id"]
    kitchen.cancel_item(item_id, "Out of stock", db["staff"]["waiter"])  # one audit row to protect
    dest = backups.create_backup(tmp_path / "copy.db")
    live, copy = _row_counts(DB_PATH), _row_counts(str(dest))
    assert copy == live and copy["order_items"] == 2 and copy["staff"] == 6
    conn = sqlite3.connect(dest)  # the append-only triggers came along too
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_daily_backup_once_per_day_and_keeps_seven(db, tmp_path):
    for i in range(9):  # nine older daily files
        (tmp_path / f"restaurant-2026-09-{i + 1:02d}.db").write_bytes(b"old")
    today = date(2026, 9, 25)
    assert backups.ensure_daily_backup(today, tmp_path) is not None
    assert backups.ensure_daily_backup(today, tmp_path) is None  # already there: not redone
    files = sorted(p.name for p in tmp_path.glob("restaurant-*.db"))
    assert len(files) == 7 and files[-1] == "restaurant-2026-09-25.db"
    assert files[0] == "restaurant-2026-09-04.db"
    assert _row_counts(str(tmp_path / files[-1]))["staff"] == 6


def test_backup_runs_at_startup(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    with TestClient(app):  # runs the lifespan (startup + shutdown)
        pass
    made = list(tmp_path.glob("restaurant-*.db"))
    assert len(made) == 1 and _row_counts(str(made[0])) == _row_counts(DB_PATH)


def test_manager_downloads_fresh_backup_audited(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    resp = login("manager").get("/admin/backup")
    assert resp.status_code == 200
    assert resp.content[:16] == b"SQLite format 3\x00"
    assert 'filename="restaurant-backup-' in resp.headers["content-disposition"]
    downloaded = tmp_path / "check.db"
    downloaded.write_bytes(resp.content)
    counts = _row_counts(str(downloaded))
    assert counts["staff"] == 6 and counts["menu_items"] == 4
    assert len(_audit_rows(audit.BACKUP_DOWNLOADED)) == 1
    assert list(tmp_path.glob("download-*.db")) == []  # temp copy removed after sending
    for role in ("waiter", "chef", "counter"):
        assert login(role).get("/admin/backup").status_code == 403


# ---------- migration for existing databases ----------

def test_old_audit_log_is_rebuilt_to_allow_new_entities(tmp_path):
    from sqlalchemy.schema import CreateTable

    from app.migrations import ensure_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    old_ddl = str(CreateTable(AuditLog.__table__).compile(engine)).replace(", 'staff', 'backup'", "")
    assert "'staff'" not in old_ddl
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE staff (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO staff (id) VALUES (1)"))
        conn.execute(text(old_ddl))
        conn.execute(text("CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log "
                          "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"))
        conn.execute(text("INSERT INTO audit_log (at, staff_id, action, entity, entity_id) "
                          "VALUES ('2026-09-01', 1, 'bill_paid', 'bill', 7)"))
    assert "audit_log.entity check" in ensure_schema(engine)
    assert ensure_schema(engine) == []
    with engine.begin() as conn:
        assert conn.execute(text("SELECT action, entity_id FROM audit_log")).all() == [("bill_paid", 7)]
        conn.execute(text("INSERT INTO audit_log (at, staff_id, action, entity, entity_id) "
                          "VALUES ('2026-09-02', 1, 'pin_changed', 'staff', 1)"))
    with engine.connect() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("DELETE FROM audit_log"))


# ---------- C3: deployment files ----------

def test_deployment_files():
    runtime = (ROOT / "requirements.txt").read_text().lower()
    for dev_only in ("pytest", "locust", "httpx", "flask", "gevent"):
        assert dev_only not in runtime, dev_only
    for needed in ("fastapi==", "uvicorn==", "sqlalchemy==", "sse-starlette==", "bcrypt==", "jinja2=="):
        assert needed in runtime, needed
    dev = (ROOT / "requirements-dev.txt").read_text()
    assert "-r requirements.txt" in dev and all(p in dev for p in ("pytest==", "httpx==", "locust=="))
    render = (ROOT / "render.yaml").read_text()
    for expected in ("pip install -r requirements.txt", "--workers 1", "--proxy-headers", "--forwarded-allow-ips '*'",
                     "mountPath: /var/data", "DB_PATH", "/var/data/restaurant.db", "COOKIE_SECURE",
                     "generateValue: true", "healthCheckPath: /health", "GST_PERCENT"):
        assert expected in render, expected
    # Major.minor only, so Render picks its latest 3.14.x patch release
    assert (ROOT / ".python-version").read_text().strip() == "%d.%d" % sys.version_info[:2]


# ---------- final round, Part C: sessions end when a PIN changes ----------

def test_pin_change_logs_out_existing_sessions(db):
    waiter, manager = login("waiter"), login("manager")
    assert waiter.get("/floor").status_code == 200
    manager.post(f"/staff/{db['staff']['waiter']}/pin", data={"new_pin": "4821", "confirm_pin": "4821"})
    resp = waiter.get("/floor")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    assert waiter.get("/auth/check").status_code == 401
    # Logging in with the new PIN works as normal
    fresh = TestClient(app, follow_redirects=False)
    fresh.headers["X-CSRF-Token"] = csrf_from(fresh.get("/login").text)
    assert fresh.post("/login", data={"name": "Rahul", "pin": "4821"}).status_code == 303
    assert fresh.get("/floor").status_code == 200


def test_changing_your_own_pin_keeps_this_session_only(db):
    here, elsewhere = login("manager"), login("manager")
    here.post(f"/staff/{db['staff']['manager']}/pin", data={"new_pin": "482193", "confirm_pin": "482193"})
    assert here.get("/staff").status_code == 200
    assert elsewhere.get("/staff").headers["location"] == "/login"


def test_random_pins_rotation_logs_everyone_out(db, fast_bcrypt, capsys):
    c = login("counter")
    assert c.get("/counter").status_code == 200
    seed_module.seed(table_count=3, reset=False, random_pins=True)
    capsys.readouterr()
    assert c.get("/counter").headers["location"] == "/login"


def test_stream_ends_after_pin_change(db, monkeypatch):
    import threading

    from app.routers import stream as stream_mod

    monkeypatch.setattr(stream_mod, "RECHECK_SECONDS", 0.2)
    c = login("waiter")
    done = threading.Event()

    def consume():
        with c.stream("GET", "/stream") as resp:
            for _ in resp.iter_lines():
                pass
        done.set()

    threading.Thread(target=consume, daemon=True).start()
    assert not done.wait(0.6)
    staff_admin.change_pin(db["staff"]["waiter"], "4821", "4821", db["staff"]["manager"])
    assert done.wait(3), "stream should close once the PIN changes"


def test_migration_adds_pin_version(tmp_path):
    from app.migrations import ensure_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE staff (id INTEGER PRIMARY KEY, name VARCHAR(60))"))
        conn.execute(text("INSERT INTO staff (name) VALUES ('Old Timer')"))
    assert "staff.pin_version" in ensure_schema(engine)
    assert ensure_schema(engine) == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT pin_version FROM staff")).scalar() == 1
