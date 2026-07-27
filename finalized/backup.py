"""
backup.py — safe, online backups of the filing database.

Why not just copy the file
--------------------------
The database runs in WAL mode, so at any moment the committed state is split
between ``edgar_filings.sqlite`` and its ``-wal`` sidecar.  A plain
``shutil.copy`` while an ingest or extraction run is writing can capture a
torn pair and produce a backup that will not open.

Everything here goes through SQLite's **online backup API**
(``Connection.backup``), which takes a transactionally consistent snapshot of a
live database without stopping the writer.  A backup taken mid-run is a valid
database as of some instant during that run.

The result is a single self-contained file — no sidecars to keep together —
because the backup connection is checkpointed as it is written.

Usage
-----
    from finalized.backup import backup_database, list_backups, restore_backup

    path = backup_database("finalized/edgar_filings.sqlite")
    for b in list_backups("finalized/edgar_filings.sqlite"):
        print(b.name, b.size_mb, b.created)
    restore_backup(path, "finalized/edgar_filings.sqlite")
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

BACKUP_SUFFIX = ".bak-"


@dataclass
class BackupInfo:
    """One backup file on disk."""
    path: str
    name: str
    size_mb: float
    created: str


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_database(
    db_path: str,
    dest: Optional[str] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    """
    Snapshot ``db_path`` to a timestamped file and return the new path.

    Safe to run while an ingest or extraction is in progress.  ``progress`` is
    called as ``progress(done_pages, total_pages)`` during the copy.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    if dest is None:
        # Timestamps are second-resolution, so two backups inside the same
        # second would otherwise silently overwrite each other.
        dest = f"{db_path}{BACKUP_SUFFIX}{_stamp()}"
        if os.path.exists(dest):
            n = 2
            while os.path.exists(f"{dest}-{n}"):
                n += 1
            dest = f"{dest}-{n}"

    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        def _cb(status, remaining, total):
            if progress:
                progress(total - remaining, total)

        # pages=-1 copies in one step; a positive value yields to the writer
        # between batches, which matters on a multi-GB file.
        src.backup(out, pages=2048, progress=_cb if progress else None)
        # The snapshot inherits WAL mode from the source, which would leave
        # -wal/-shm sidecars beside every backup.  Collapse to a single
        # self-contained file so a backup is one thing you can move or archive.
        out.execute("PRAGMA journal_mode=DELETE")
    finally:
        out.close()
        src.close()
    return dest


def list_backups(db_path: str) -> list[BackupInfo]:
    """Every backup of ``db_path``, newest first."""
    folder = os.path.dirname(os.path.abspath(db_path)) or "."
    base = os.path.basename(db_path) + BACKUP_SUFFIX
    hits = []
    for name in os.listdir(folder):
        # Skip SQLite sidecars: a stray -wal/-shm is not a backup, and listing
        # one would let prune_backups count it against `keep`.
        if not name.startswith(base) or name.endswith(("-wal", "-shm")):
            continue
        full = os.path.join(folder, name)
        st = os.stat(full)
        hits.append(BackupInfo(
            path=full,
            name=name,
            size_mb=round(st.st_size / 1e6, 1),
            created=datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        ))
    return sorted(hits, key=lambda b: b.created, reverse=True)


def restore_backup(backup_path: str, db_path: str, keep_current: bool = True) -> str:
    """
    Replace ``db_path`` with ``backup_path``.

    The database being replaced is itself backed up first unless
    ``keep_current`` is False — restoring the wrong snapshot should not be the
    end of the story.  Returns the path of that safety copy, or "" if skipped.
    """
    if not os.path.exists(backup_path):
        raise FileNotFoundError(backup_path)

    safety = ""
    if keep_current and os.path.exists(db_path):
        safety = f"{db_path}{BACKUP_SUFFIX}{_stamp()}-prerestore"
        shutil.copy2(db_path, safety)

    # Drop stale sidecars: they belong to the database being replaced.
    for side in (db_path + "-wal", db_path + "-shm"):
        if os.path.exists(side):
            os.remove(side)
    shutil.copy2(backup_path, db_path)
    return safety


def prune_backups(db_path: str, keep: int = 3) -> list[str]:
    """Delete all but the ``keep`` newest backups.  Returns what was removed."""
    removed = []
    for b in list_backups(db_path)[keep:]:
        os.remove(b.path)
        removed.append(b.name)
    return removed


def verify_backup(backup_path: str) -> tuple[bool, str]:
    """
    Open the backup and run an integrity check.

    A backup you have never opened is a hope, not a backup.
    """
    try:
        con = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            n = con.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
        finally:
            con.close()
    except Exception as exc:                                    # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return ok == "ok", f"integrity_check={ok}, filings={n:,}"
