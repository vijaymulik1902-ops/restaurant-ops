"""Tiny, idempotent schema upgrades for databases created by an older version.

create_all() adds missing TABLES but never missing COLUMNS, so each new column on an
existing table is added here with SQLite's ALTER TABLE ADD COLUMN. Safe to run on every start.
"""
import logging

import re

from sqlalchemy import Engine, inspect, text
from sqlalchemy.schema import CreateTable

from app.db import write_engine
from app.models import AUDIT_ENTITIES, AuditLog

log = logging.getLogger(__name__)

# (table, column, SQL type + default) - append only, never edit a released entry
ADDED_COLUMNS = [
    ("menu_items", "archived", "BOOLEAN NOT NULL DEFAULT 0"),
    ("staff", "pin_version", "INTEGER NOT NULL DEFAULT 1"),
]


def ensure_schema(engine: Engine = write_engine) -> list[str]:
    """Add any missing columns and upgrade constraints. Returns what it changed."""
    added = []
    with engine.begin() as conn:
        if _audit_entities_outdated(conn):
            _rebuild_audit_log(conn)
            added.append("audit_log.entity check")
        insp = inspect(conn)
        tables = set(insp.get_table_names())
        for table, column, ddl in ADDED_COLUMNS:
            if table not in tables:
                continue  # create_all will build it complete
            if column not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                added.append(f"{table}.{column}")
                log.warning("Schema upgrade: added column %s.%s", table, column)
    return added


def _audit_entities_outdated(conn) -> bool:
    """True if the existing audit_log CHECK constraint doesn't allow every current entity."""
    sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'")).scalar()
    if not sql:
        return False  # no table yet: create_all builds it with the current constraint
    match = re.search(r"entity IN \(([^)]*)\)", sql)
    allowed = set(re.findall(r"'([^']*)'", match.group(1))) if match else set()
    return not set(AUDIT_ENTITIES) <= allowed


def _rebuild_audit_log(conn) -> None:
    """Recreate audit_log with the current CHECK, keeping every row, index and trigger.

    Runs inside the caller's transaction: all or nothing. The append-only triggers only
    block UPDATE/DELETE of rows; copying rows and dropping the old table is allowed.
    """
    triggers = conn.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_log'")).scalars().all()
    ddl = str(CreateTable(AuditLog.__table__).compile(conn)).replace(
        "CREATE TABLE audit_log", "CREATE TABLE audit_log_new", 1)
    cols = ", ".join(c.name for c in AuditLog.__table__.columns)
    conn.execute(text(ddl))
    conn.execute(text(f"INSERT INTO audit_log_new ({cols}) SELECT {cols} FROM audit_log"))
    conn.execute(text("DROP TABLE audit_log"))  # takes its old indexes and triggers with it
    conn.execute(text("ALTER TABLE audit_log_new RENAME TO audit_log"))
    for index in AuditLog.__table__.indexes:
        index.create(conn)
    for trigger_sql in triggers:
        conn.execute(text(trigger_sql))
    log.warning("Schema upgrade: rebuilt audit_log to allow entities %s", ", ".join(AUDIT_ENTITIES))
