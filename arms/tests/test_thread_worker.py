import asyncio

import pytest

from research_brain.jobs import SpendingLimits
from research_arms.submissions import PaperSubmissions
from research_arms.paper_threads import PaperThreads
from research_arms.thread_worker import PaperThreadWorker
from test_registry import setup
from test_paper_threads import Service as PaperService, Client


class Access:
    async def authorize(self, actor, **kwargs):
        return {"parent_channel_id": "20" if kwargs["channel_id"] != "20" else None}


class Service(PaperService):
    def __init__(self, *args, **kwargs): super().__init__(*args)
    def submit(self, *args, **kwargs): return {"job_id": "job_test"}


def queue_shared(arms, *, actor="1"):
    queue = PaperSubmissions(arms, Access(), service_factory=Service, automatic_threads=True)
    ident = queue.enqueue(actor, "20", "10", "project", "100", "https://arxiv.org/abs/2506.24056v2", limits=SpendingLimits())
    return queue, ident


def test_source_completion_persists_thread_work_and_reuses_revision(setup):
    arms, _ = setup
    queue, ident = queue_shared(arms)
    client = Client()
    async def run():
        await queue.work_once()
        assert queue.show(ident, "1", "20", "10", "project")["paper_thread"]["state"] == "QUEUED"
        worker = PaperThreadWorker(arms, Access(), queue,
            PaperThreads(arms, Access(), client, "123", service_factory=Service))
        assert await worker.work_once() == ident
        assert await worker.work_once() is None
        # A second request for the same pinned paper gets the completed thread.
        second = queue.enqueue("1", "20", "10", "project", "101", "https://arxiv.org/abs/2506.24056v2", limits=SpendingLimits())
        await queue.work_once()
        await worker.work_once()
        assert queue.show(second, "1", "20", "10", "project")["paper_thread"] == {"state": "COMPLETE", "thread_id": "300"}
        assert len(client.calls) == 2
    asyncio.run(run())


def test_dm_never_queues_shared_thread(setup):
    arms, _ = setup
    queue = PaperSubmissions(arms, Access(), service_factory=Service, automatic_threads=True)
    queue.enqueue("1", "30", None, "alice", "100", "https://arxiv.org/abs/2506.24056v2", limits=SpendingLimits())
    asyncio.run(queue.work_once())
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM paper_thread_jobs").fetchone()[0] == 0


def test_revoked_submission_cannot_create_thread(setup):
    arms, spaces = setup
    spaces.set_membership("project", "bob", "maintainer")
    queue, ident = queue_shared(arms, actor="2")
    client = Client()
    async def run():
        await queue.work_once()
        spaces.set_membership("project", "bob", "member")
        worker = PaperThreadWorker(arms, Access(), queue,
            PaperThreads(arms, Access(), client, "123", service_factory=Service))
        with pytest.raises(PermissionError): await worker.work_once()
        assert await worker.work_once() is None
        assert client.calls == []
        with arms.connect(readonly=True) as db:
            assert db.execute("SELECT state FROM paper_thread_jobs").fetchone()[0] == "NEEDS_ATTENTION"
            assert db.execute("SELECT state FROM paper_submissions").fetchone()[0] == "SOURCE_READY"
    asyncio.run(run())


def test_thread_failure_does_not_replay_or_invalidate_source(setup):
    arms, _ = setup
    queue, ident = queue_shared(arms)
    client = Client(fail=True)
    async def run():
        await queue.work_once()
        worker = PaperThreadWorker(arms, Access(), queue,
            PaperThreads(arms, Access(), client, "123", service_factory=Service))
        with pytest.raises(asyncio.TimeoutError): await worker.work_once()
        assert await worker.work_once() is None
        status = queue.show(ident, "1", "20", "10", "project")
        assert status["state"] == "SOURCE_READY" and status["paper_thread"]["state"] == "NEEDS_ATTENTION"
        assert len(client.calls) == 1
    asyncio.run(run())


def test_thread_queue_insert_and_source_completion_are_atomic(setup):
    import sqlite3
    arms, _ = setup
    queue, _ = queue_shared(arms)
    with arms.connect() as db:
        db.execute("CREATE TRIGGER fail_thread_queue BEFORE INSERT ON paper_thread_jobs BEGIN SELECT RAISE(ABORT,'injected disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError): asyncio.run(queue.work_once())
    with arms.connect(readonly=True) as db:
        row = db.execute("SELECT state,job_id FROM paper_submissions").fetchone()
        assert tuple(row) == ("NEEDS_ATTENTION", None)
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='source_prepared'").fetchone()[0] == 0


def test_concurrent_thread_workers_claim_once(setup):
    arms, _ = setup
    queue, _ = queue_shared(arms)
    async def run():
        await queue.work_once()
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        class Threads:
            async def ensure(self, *args):
                calls.append(args)
                started.set()
                await release.wait()
                return {"thread_id": "300"}
        first = PaperThreadWorker(arms, Access(), queue, Threads())
        second = PaperThreadWorker(arms, Access(), queue, Threads())
        task = asyncio.create_task(first.work_once())
        await started.wait()
        assert await second.work_once() is None
        release.set()
        await task
        assert len(calls) == 1
    asyncio.run(run())
