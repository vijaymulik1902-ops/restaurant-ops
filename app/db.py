"""Database setup.

Two engines on the same SQLite file:
- write engine: every transaction starts with BEGIN IMMEDIATE, so it takes the
  write lock up front. Two writers never deadlock; the second one waits
  (busy_timeout) and then proceeds.
- read engine: normal deferred transactions. In WAL mode reads never block
  writes and writes never block reads, so floor/kitchen screens stay fast.
"""
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import DB_PATH, TIMEZONE


class Base(DeclarativeBase):
    pass


def now() -> datetime:
    """Current local restaurant time (naive, since SQLite has no timezone type)."""
    return datetime.now(TIMEZONE).replace(tzinfo=None)


def _make_engine(immediate: bool):
    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):
        # Let SQLAlchemy control transactions instead of the sqlite3 driver
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


write_engine = _make_engine(immediate=True)
read_engine = _make_engine(immediate=False)

_WriteSession = sessionmaker(bind=write_engine, expire_on_commit=False)
_ReadSession = sessionmaker(bind=read_engine, expire_on_commit=False)


@contextmanager
def write_session():
    """Use for anything that changes data. Commits on success, rolls back on error."""
    session: Session = _WriteSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def read_session():
    """Use for screens and reports. Never writes."""
    session: Session = _ReadSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def init_db():
    from app import models  # noqa: F401  (registers tables on Base)

    Base.metadata.create_all(write_engine)
