"""Database backups with SQLite's online backup API (consistent even while the app writes; WAL-safe).

- Daily: restaurant-YYYY-MM-DD.db in BACKUP_DIR, the last 7 kept. Made at startup if today's
  is missing, then every 24 hours by a background task (app.main).
- On demand: a manager downloads a fresh copy (audited as backup_downloaded).

BACKUP_DIR defaults to a "backups" folder next to the database (on Render: /var/data/backups,
on the persistent disk, so backups survive deploys).
"""
import logging
import os
import sqlite3
import tempfile
import time
from datetime import date
from pathlib import Path

from app.config import DB_PATH
from app.db import now, read_session, write_session
from app.services import ServiceError, audit
from app.services.tables import get_active_staff

log = logging.getLogger(__name__)

KEEP_DAILY = 7
DAILY_PREFIX = "restaurant-"


def backup_dir() -> Path:
    return Path(os.getenv("BACKUP_DIR") or Path(DB_PATH).resolve().parent / "backups")


def create_backup(dest: Path, source: str = DB_PATH) -> Path:
    """Copy the live database to `dest` page by page. Written to a temp name, then renamed,
    so a half-written file never looks like a backup."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    src = sqlite3.connect(source, timeout=30)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst, pages=1024)  # in chunks: writers aren't blocked for the whole copy
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(tmp, dest)
    return dest


def _daily_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.glob(f"{DAILY_PREFIX}????-??-??.db"))


def ensure_daily_backup(today: date | None = None, folder: Path | None = None) -> Path | None:
    """Make today's backup if it doesn't exist yet, then keep only the newest 7.
    Returns the new file, or None if today's was already there."""
    folder = folder or backup_dir()
    today = today or now().date()
    target = folder / f"{DAILY_PREFIX}{today.isoformat()}.db"
    created = None
    if not target.exists():
        created = create_backup(target)
        log.info("Daily backup written: %s", target)
    for old in _daily_files(folder)[:-KEEP_DAILY]:
        old.unlink(missing_ok=True)
    cutoff = time.time() - 3600  # downloads interrupted mid-send leave a temp copy behind
    for stale in folder.glob("download-*.db"):
        if stale.stat().st_mtime < cutoff:
            stale.unlink(missing_ok=True)
    return created


def fresh_backup_for_download(by_staff_id: int) -> tuple[Path, str]:
    """A brand-new copy for a manager to download. Returns (temp file path, download name).
    The caller deletes the temp file after sending it.

    The copy is made WITHOUT holding the write lock (floor and kitchen keep working);
    only the short audit write takes it.
    """
    with read_session() as s:
        if get_active_staff(s, by_staff_id).role != "manager":
            raise ServiceError("Only a manager can download backups")
    stamp = now().strftime("%Y%m%d%H%M")
    folder = backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="download-", suffix=".db", dir=folder)
    os.close(fd)
    path = Path(name)
    try:
        create_backup(path)
        with write_session() as s:
            audit.record(s, by_staff_id, audit.BACKUP_DOWNLOADED, "backup", int(stamp),
                         new={"file": f"restaurant-backup-{stamp}.db", "bytes": path.stat().st_size})
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path, f"restaurant-backup-{stamp}.db"
