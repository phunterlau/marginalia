"""Explicit local registry migration. Backups remain private runtime artifacts."""
import fcntl
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import uuid


def migrate_v1_to_v2(root):
    root = Path(root).resolve(strict=True)
    path = root / "arms.sqlite3"
    lock = os.open(root / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT version FROM meta").fetchall()
            if version == [(2,)]:
                return None
            if version != [(1,)]:
                raise ValueError("Unsupported Arms migration")
            if db.execute("SELECT 1 FROM turns WHERE status='RUNNING' LIMIT 1").fetchone():
                raise ValueError("Verify and recover interrupted workers before migration")
            backup = root / ("arms-v1-backup-" + uuid.uuid4().hex + ".sqlite3")
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            # A separate read connection snapshots committed state while the
            # IMMEDIATE writer transaction excludes competing writes.
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as source, closing(sqlite3.connect(backup)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Backup verification failed")
            db.execute("CREATE TABLE conversation_routes(route_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id))")
            db.execute("UPDATE meta SET version=2")
            return backup
    finally:
        os.close(lock)
