"""Explicit local registry migration. Backups remain private runtime artifacts."""
import fcntl
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import uuid


def migrate_v1_to_v2(root):
    return _migrate(root, 1, 2, "CREATE TABLE conversation_routes(route_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id))")


def migrate_v2_to_v3(root):
    return _migrate(root, 2, 3, "CREATE TABLE session_forks(request_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES conversations(id), target_id TEXT UNIQUE NOT NULL REFERENCES conversations(id), turn_id TEXT NOT NULL REFERENCES turns(id), actor TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL)")


def _migrate(root, previous, current, statement):
    root = Path(root).resolve(strict=True)
    path = root / "arms.sqlite3"
    lock = os.open(root / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT version FROM meta").fetchall()
            if version == [(current,)]:
                return None
            if version != [(previous,)]:
                raise ValueError("Unsupported Arms migration")
            if db.execute("SELECT 1 FROM turns WHERE status='RUNNING' LIMIT 1").fetchone():
                raise ValueError("Verify and recover interrupted workers before migration")
            backup = root / (f"arms-v{previous}-backup-" + uuid.uuid4().hex + ".sqlite3")
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            # A separate read connection snapshots committed state while the
            # IMMEDIATE writer transaction excludes competing writes.
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as source, closing(sqlite3.connect(backup)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Backup verification failed")
            db.execute(statement)
            db.execute("UPDATE meta SET version=?", (current,))
            return backup
    finally:
        os.close(lock)
