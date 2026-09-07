"""Local consistent snapshots, restore verification and read-only health checks."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
import fcntl
import hashlib
import importlib.resources
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .store.sqlite import utc_now


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def database(path: Path, *, writable=False, immutable=False):
    suffix = "&immutable=1" if immutable and not writable else ""
    db = sqlite3.connect(f"{path.as_uri()}?mode={'rw' if writable else 'ro'}{suffix}", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        db.close()


@contextmanager
def maintenance_lock(root: Path):
    with (root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("An active worker prevents maintenance") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _check_db(path: Path, *, immutable=False) -> None:
    with database(path, immutable=immutable) as db:
        if [r[0] for r in db.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError(f"Database integrity failure: {path.name}")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError(f"Database foreign-key failure: {path.name}")


def backup(root: str | Path, destination: str | Path, *, _lock_held: bool = False) -> dict:
    root = Path(root).expanduser().resolve(strict=True)
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Backup destination must not exist")
    if destination.resolve() == root or root in destination.resolve().parents:
        raise ValueError("Backup must be outside the data root")
    # Validate before creating even a worker lock in a non-Brain directory.
    _check_db(root / "brain.sqlite3")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (nullcontext() if _lock_held else maintenance_lock(root)), ExitStack() as stack:
        # Jobs -> canonical is the same lock order as job reconciliation. Holding
        # writers on both DBs makes a real snapshot, not two unrelated copies.
        names = (["jobs.sqlite3"] if (root / "jobs.sqlite3").exists() else []) + ["brain.sqlite3"]
        for name in names:
            writer = stack.enter_context(database(root / name, writable=True))
            writer.execute("BEGIN IMMEDIATE")
        stage = Path(tempfile.mkdtemp(prefix=".brain-backup-", dir=destination.parent))
        # Failed stages remain inspectable; they are never published as backups.
        try:
            for name in names:
                with database(root / name) as source, sqlite3.connect(stage / name) as target:
                    source.backup(target)
                _check_db(stage / name)
            files = {name: file_digest(stage / name) for name in names}
            with database(stage / "brain.sqlite3") as db:
                assets = db.execute("SELECT id,local_path,sha256 FROM source_assets ORDER BY id").fetchall()
            asset_map = {}
            for asset in assets:
                source = Path(asset["local_path"]).resolve(strict=True)
                if (root / "assets").resolve() not in source.parents:
                    raise ValueError("Source asset is outside this Brain's assets directory")
                if file_digest(source) != asset["sha256"]:
                    raise ValueError("Source asset checksum mismatch")
                relative = f"assets/{asset['sha256'][:2]}/{asset['sha256']}"
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copyfile(source, target)
                files[relative] = file_digest(target)
                if files[relative] != asset["sha256"]:
                    raise ValueError("Asset changed during snapshot")
                asset_map[asset["id"]] = relative
            manifest = {"format": "brain-backup-v1", "created_at": utc_now(),
                        "files": files, "assets": asset_map,
                        "includes": names, "excludes": ["space registry", "Pi sessions", "orphan assets"]}
            (stage / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
            verify_backup(stage)
            # Flush contents before making the complete directory visible.
            for file in stage.rglob("*"):
                if file.is_file():
                    with file.open("rb") as stream:
                        os.fsync(stream.fileno())
            if destination.exists():
                raise FileExistsError("Backup destination appeared during snapshot")
            stage.rename(destination)
            return {"backup": str(destination), "verified": True, "asset_count": len(asset_map), "databases": names}
        except Exception as exc:
            raise RuntimeError(f"Backup failed; unpublished staging preserved at {stage}: {exc}") from exc


def verify_backup(path: str | Path) -> dict:
    path = Path(path).expanduser().resolve(strict=True)
    manifest = json.loads((path / "manifest.json").read_text())
    if manifest.get("format") != "brain-backup-v1" or not isinstance(manifest.get("files"), dict):
        raise ValueError("Unsupported backup manifest")
    allowed_dbs = {"brain.sqlite3", "jobs.sqlite3"}
    if "brain.sqlite3" not in manifest["files"] or set(manifest["includes"]) - allowed_dbs:
        raise ValueError("Invalid backup database set")
    if set(manifest["includes"]) != (set(manifest["files"]) & allowed_dbs):
        raise ValueError("Backup database inventory mismatch")
    for name, checksum in manifest["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not (name in allowed_dbs or name.startswith("assets/")):
            raise ValueError("Unsafe backup member")
        target = (path / relative).resolve(strict=True)
        if path not in target.parents or any(p.is_symlink() for p in (path / relative, *(path / relative).parents) if p != path and path in p.parents) or not target.is_file() or file_digest(target) != checksum:
            raise ValueError("Backup checksum or path failure")
    for name in manifest["includes"]:
        if name not in manifest["files"]:
            raise ValueError("Missing backup database")
        journal = path / f"{name}-wal"
        if journal.exists() and journal.stat().st_size:
            raise ValueError("Unexpected WAL in published snapshot")
        _check_db(path / name, immutable=True)
    with database(path / "brain.sqlite3", immutable=True) as db:
        assets = {row["id"]: row["sha256"] for row in db.execute("SELECT id,sha256 FROM source_assets")}
    if set(assets) != set(manifest["assets"]):
        raise ValueError("Backup asset inventory mismatch")
    for asset_id, relative in manifest["assets"].items():
        if manifest["files"].get(relative) != assets[asset_id]:
            raise ValueError("Backup asset mapping mismatch")
    return manifest


def restore(path: str | Path, destination: str | Path) -> dict:
    path = Path(path).expanduser().resolve(strict=True)
    manifest = verify_backup(path)
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Restore requires a new directory; existing data is never overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".brain-restore-", dir=destination.parent))
    for relative in manifest["files"]:
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path / relative, target)
        if file_digest(target) != manifest["files"][relative]:
            raise ValueError(f"Copy verification failed; staging preserved at {stage}")
    with database(stage / "brain.sqlite3", writable=True) as db:
        for asset_id, relative in manifest["assets"].items():
            db.execute("UPDATE source_assets SET local_path=? WHERE id=?", (str(destination / relative), asset_id))
        db.commit()
    if "jobs.sqlite3" in manifest["includes"]:
        with database(stage / "jobs.sqlite3", writable=True) as db:
            # Even QUEUED approvals must not automatically spend after restore.
            for row in db.execute("SELECT id FROM jobs WHERE status IN ('QUEUED','RUNNING')").fetchall():
                db.execute("UPDATE jobs SET status='NEEDS_ATTENTION',claimed_by=NULL,updated_at=? WHERE id=?", (utc_now(), row[0]))
                db.execute("INSERT INTO events(job_id,kind,actor,payload_json,at) VALUES (?,'restored_requires_review','local','{}',?)", (row[0], utc_now()))
            db.commit()
    for name in manifest["includes"]:
        _check_db(stage / name)
    if destination.exists():
        raise FileExistsError("Restore destination appeared during copy")
    stage.rename(destination)
    return {"root": str(destination), "restored": True, "verified": True,
            "job_policy": "Queued/running jobs require explicit review and reapproval"}


def doctor(root: str | Path) -> dict:
    root = Path(root).expanduser().resolve(strict=True)
    _check_db(root / "brain.sqlite3")
    with database(root / "brain.sqlite3") as db:
        assets = db.execute("SELECT id,local_path,sha256 FROM source_assets ORDER BY id").fetchall()
        schemas = [r[0] for r in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
        journal = db.execute("PRAGMA journal_mode").fetchone()[0]
    issues = []
    expected = sorted(int(p.name.split('_')[0]) for p in importlib.resources.files("research_brain.store").joinpath("migrations").iterdir() if p.suffix == ".sql")
    if schemas != expected:
        issues.append({"issue": "incompatible_schema", "expected": expected})
    referenced = set()
    for asset in assets:
        path = Path(asset["local_path"]).resolve()
        referenced.add(path)
        if (root / "assets").resolve() not in path.parents:
            issues.append({"asset_id": asset["id"], "issue": "outside_root"})
        elif not path.is_file() or file_digest(path) != asset["sha256"]:
            issues.append({"asset_id": asset["id"], "issue": "missing_or_checksum_mismatch"})
    orphans = sorted(str(p.relative_to(root)) for p in (root / "assets").rglob("*") if p.is_file() and p.resolve() not in referenced)
    job_states = {}
    if (root / "jobs.sqlite3").exists():
        _check_db(root / "jobs.sqlite3")
        with database(root / "jobs.sqlite3") as db:
            job_states = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status")}
    return {"healthy": not issues, "schema_versions": schemas, "journal_mode": journal,
            "asset_count": len(assets), "issues": issues, "orphan_assets": orphans, "job_states": job_states,
            "staging_files": [name for name in orphans if Path(name).name.startswith(".")],
            "orphan_policy": "Report only; never automatically delete canonical or orphan assets"}


def migrate(root: str | Path, backup_destination: str | Path) -> dict:
    from .store.sqlite import SQLiteStore
    root = Path(root).expanduser().resolve(strict=True)
    _check_db(root / "brain.sqlite3")
    with maintenance_lock(root):
        snapshot = backup(root, backup_destination, _lock_held=True)
        SQLiteStore(root / "brain.sqlite3", allow_migration=True)
        _check_db(root / "brain.sqlite3")
    return {"migrated": True, "backup": snapshot, "health": doctor(root)}
