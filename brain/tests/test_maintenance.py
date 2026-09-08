import importlib.resources
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from research_brain import Brain
from research_brain import maintenance
from research_brain.store.sqlite import SQLiteStore
from test_absorption_jobs import jobs, enqueue, approve, work


def test_snapshot_restore_is_consistent_and_never_replays_queued_work(jobs, tmp_path):
    job = enqueue(jobs)
    approve(jobs, job)
    snapshot = tmp_path / "snapshot"
    result = maintenance.backup(jobs.brain.root, snapshot)
    assert result["verified"]
    restored = tmp_path / "restored"
    maintenance.restore(snapshot, restored)
    assert maintenance.doctor(restored)["healthy"]
    from research_brain.jobs import AbsorptionJobs
    recovered = AbsorptionJobs(Brain(restored, initialize=False), "personal")
    assert recovered.show(job["id"])["status"] == "NEEDS_ATTENTION"
    assert work(recovered)["status"] == "IDLE"
    assert jobs.show(job["id"])["status"] == "QUEUED"  # Original untouched.
    with recovered.brain.store.connect() as db:
        asset_paths = [Path(r[0]) for r in db.execute("SELECT local_path FROM source_assets")]
    assert all(restored in path.parents and path.is_file() for path in asset_paths)


def test_backup_refuses_active_worker_and_existing_destinations(jobs, tmp_path):
    snapshot = tmp_path / "snapshot"
    with jobs.worker_lock(), pytest.raises(RuntimeError, match="active worker"):
        maintenance.backup(jobs.brain.root, snapshot)
    assert not snapshot.exists()
    snapshot.mkdir()
    with pytest.raises(FileExistsError):
        maintenance.backup(jobs.brain.root, snapshot)


def test_asset_corruption_and_tampered_snapshot_fail_closed(jobs, tmp_path):
    snapshot = tmp_path / "snapshot"
    maintenance.backup(jobs.brain.root, snapshot)
    manifest = maintenance.verify_backup(snapshot)
    member = snapshot / next(iter(manifest["assets"].values()))
    member.write_bytes(b"corruption")
    target = tmp_path / "restored"
    with pytest.raises(ValueError, match="checksum"):
        maintenance.restore(snapshot, target)
    assert not target.exists()


def test_doctor_reports_orphans_without_mutating_corpus(jobs):
    orphan = jobs.brain.root / "assets" / "unreferenced"
    orphan.write_text("preserve me")
    before = maintenance.file_digest(jobs.brain.store.path)
    report = maintenance.doctor(jobs.brain.root)
    assert "assets/unreferenced" in report["orphan_assets"]
    assert orphan.read_text() == "preserve me"
    assert maintenance.file_digest(jobs.brain.store.path) == before


def test_failed_copy_leaves_no_published_backup(jobs, tmp_path):
    destination = tmp_path / "snapshot"
    with patch("research_brain.maintenance.shutil.copyfile", side_effect=OSError("disk full")):
        with pytest.raises(RuntimeError, match="staging preserved"):
            maintenance.backup(jobs.brain.root, destination)
    assert not destination.exists()
    assert list(tmp_path.glob(".brain-backup-*"))
    assert maintenance.doctor(jobs.brain.root)["healthy"]


def old_database(root):
    root.mkdir()
    path = root / "brain.sqlite3"
    sql = importlib.resources.files("research_brain.store").joinpath("migrations/001_initial.sql").read_text()
    with sqlite3.connect(path) as db:
        db.executescript(sql)
        db.execute("INSERT INTO schema_migrations VALUES (1,'fixture')")
    return path


def test_existing_schema_never_migrates_implicitly_and_explicit_migration_has_backup(tmp_path):
    root = tmp_path / "legacy"
    path = old_database(root)
    with pytest.raises(ValueError, match="Incompatible"):
        Brain(root)
    snapshot = tmp_path / "pre-migration"
    result = maintenance.migrate(root, snapshot)
    assert result["migrated"]
    assert result["health"]["schema_versions"] == [1, 2, 3]
    with maintenance.database(snapshot / "brain.sqlite3") as db:
        assert [r[0] for r in db.execute("SELECT version FROM schema_migrations")] == [1]
    SQLiteStore(path, initialize=False)


def test_migration_ddl_and_version_rollback_together(tmp_path):
    root = tmp_path / "legacy"
    path = old_database(root)
    fake_package = tmp_path / "package"
    migrations = fake_package / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "001_initial.sql").write_text("-- already applied\n")
    (migrations / "002_bad.sql").write_text("CREATE TABLE must_rollback(x);\nINVALID SQL;\n")
    with patch("research_brain.store.sqlite.importlib.resources.files", return_value=fake_package):
        with pytest.raises(sqlite3.OperationalError):
            maintenance.migrate(root, tmp_path / "snapshot")
    with maintenance.database(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='must_rollback'").fetchone() is None
        assert [r[0] for r in db.execute("SELECT version FROM schema_migrations")] == [1]
    assert maintenance.verify_backup(tmp_path / "snapshot")


def test_restore_never_overwrites_and_rejects_traversal(jobs, tmp_path):
    snapshot = tmp_path / "snapshot"
    maintenance.backup(jobs.brain.root, snapshot)
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("important")
    with pytest.raises(FileExistsError):
        maintenance.restore(snapshot, existing)
    assert (existing / "keep").read_text() == "important"
    manifest = json.loads((snapshot / "manifest.json").read_text())
    manifest["files"]["../escape"] = "fake"
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unsafe"):
        maintenance.verify_backup(snapshot)


def test_unlisted_job_database_cannot_bypass_restored_approval(jobs, tmp_path):
    snapshot = tmp_path / "snapshot"
    maintenance.backup(jobs.brain.root, snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    manifest["includes"].remove("jobs.sqlite3")
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="inventory"):
        maintenance.restore(snapshot, tmp_path / "restored")


def test_published_snapshot_does_not_consume_unverified_wal(jobs, tmp_path):
    snapshot = tmp_path / "snapshot"
    maintenance.backup(jobs.brain.root, snapshot)
    (snapshot / "brain.sqlite3-wal").write_bytes(b"unverified WAL bytes")
    with pytest.raises(ValueError, match="Unexpected WAL"):
        maintenance.verify_backup(snapshot)
