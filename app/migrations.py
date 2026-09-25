"""Tiny, idempotent schema upgrades for databases created by an older version.

create_all() adds missing TABLES but never missing COLUMNS, so each new column on an
existing table is added here with SQLite's ALTER TABLE ADD COLUMN. Safe to run on every start.
"""
import logging

from sqlalchemy import Engine, inspect, text

from app.db import write_engine

log = logging.getLogger(__name__)

# (table, column, SQL type + default) - append only, never edit a released entry
ADDED_COLUMNS = [
    ("menu_items", "archived", "BOOLEAN NOT NULL DEFAULT 0"),
]


def ensure_schema(engine: Engine = write_engine) -> list[str]:
    """Add any missing columns. Returns the 'table.column' names it added."""
    added = []
    with engine.begin() as conn:
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
