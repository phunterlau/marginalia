import asyncio
import fcntl
import os
import sqlite3

import pytest

from research_arms.recovery import recover
from research_arms.forks import prepare
from test_registry import setup
from test_forks import source_fixture
from test_thread_worker import queue_shared


def interrupted(arms):
    queue, ident = queue_shared(arms)
    asyncio.run(queue.work_once())
    source, turn = source_fixture(arms)
    fork, _ = prepare(arms, source, turn, "1", channel_id="21", guild_id="10", request_id="crashed", name="Partial")
    with arms.connect() as db:
        db.execute("UPDATE paper_submissions SET state='RUNNING'")
        db.execute("UPDATE paper_thread_jobs SET state='RUNNING'")
        db.execute("INSERT INTO paper_threads VALUES ('paper_thread_test','project','10','20','doc_test','v2','THREAD_SENDING','300',NULL,NULL,'now')")
    return ident, fork["target_id"]


def test_recovery_requires_confirmation_and_free_ownership_lock(setup):
    arms, _ = setup
    interrupted(arms)
    with pytest.raises(ValueError): recover(arms)
    lock = os.open(arms.root / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="still owns"): recover(arms, confirmed_stopped=True)
    finally: os.close(lock)
    assert list(arms.root.glob("arms-recovery-backup-*")) == []
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT state FROM paper_threads").fetchone()[0] == "THREAD_SENDING"


def test_recovery_backs_up_and_quarantines_without_replay(setup):
    arms, _ = setup
    _, target = interrupted(arms)
    result = recover(arms, confirmed_stopped=True)
    for key in ("paper_submissions", "paper_thread_jobs", "paper_threads", "session_forks"):
        assert result[key + "_quarantined"] == 1
    assert os.stat(result["backup"]).st_mode & 0o777 == 0o600
    with sqlite3.connect(result["backup"]) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT state FROM paper_threads").fetchone()[0] == "THREAD_SENDING"
    with arms.connect(readonly=True) as db:
        for table in ("paper_submissions", "paper_thread_jobs", "paper_threads", "session_forks"):
            assert db.execute(f"SELECT state FROM {table}").fetchone()[0] == "NEEDS_ATTENTION"
        assert db.execute("SELECT status FROM conversations WHERE id=?", (target,)).fetchone()[0] == "NEEDS_ATTENTION"
        assert db.execute("SELECT starter_id FROM paper_threads").fetchone()[0] == "300"
        count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    again = recover(arms, confirmed_stopped=True)
    assert all(value == 0 for key, value in again.items() if key.endswith("quarantined"))
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == count


def test_recovery_failure_rolls_back_all_quarantine_changes(setup):
    arms, _ = setup
    interrupted(arms)
    with arms.connect() as db:
        db.execute("CREATE TRIGGER fail_recovery BEFORE UPDATE ON paper_threads BEGIN SELECT RAISE(ABORT,'injected disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError): recover(arms, confirmed_stopped=True)
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT state FROM paper_submissions").fetchone()[0] == "RUNNING"
        assert db.execute("SELECT state FROM paper_thread_jobs").fetchone()[0] == "RUNNING"
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='source_interrupted'").fetchone()[0] == 0
    assert len(list(arms.root.glob("arms-recovery-backup-*"))) == 1
