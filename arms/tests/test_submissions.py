import asyncio

import pytest

from research_brain.jobs import SpendingLimits
from research_arms.submissions import PaperSubmissions
from test_registry import setup


class Access:
    async def authorize(self, *args, **kwargs): return {}


def test_explicit_source_retry_preserves_request_and_audits_once(setup):
    from research_arms import Unavailable
    arms, _ = setup
    calls = []
    class Service:
        def __init__(self, *args, **kwargs): pass
        def submit(self, url, *, limits):
            calls.append((url, limits))
            if len(calls) == 1: raise OSError("synthetic failure")
            return {"job_id": "job_recovered"}
    queue = PaperSubmissions(arms, Access(), service_factory=Service)
    ident = queue.enqueue("1", "30", None, "alice", "100", "https://arxiv.org/abs/2506.24056v2", limits=SpendingLimits())
    with pytest.raises(Unavailable): queue.retry(ident, "1", "30", None, "alice")
    with pytest.raises(OSError): asyncio.run(queue.work_once())
    for actor, channel, space in [("2", "30", "alice"), ("1", "31", "alice"), ("1", "30", "project")]:
        with pytest.raises(Unavailable): queue.retry(ident, actor, channel, None, space)
    assert queue.retry(ident, "1", "30", None, "alice") == ident
    with pytest.raises(Unavailable): queue.retry(ident, "1", "30", None, "alice")
    assert asyncio.run(queue.work_once()) == ident
    assert calls[0] == calls[1]
    assert queue.show(ident, "1", "30", None, "alice")["state"] == "SOURCE_READY"
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='source_retry_requested'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events WHERE kind='source_dispatched'").fetchone()[0] == 2


def test_source_request_is_durable_and_idempotent_without_paid_work(setup):
    arms, _ = setup
    calls = []
    class Service:
        def __init__(self, *args, **kwargs): pass
        def submit(self, url, *, limits):
            calls.append(url)
            return {"job_id": "job_synthetic"}
    queue = PaperSubmissions(arms, Access(), service_factory=Service)
    ident = queue.enqueue("1", "30", None, "alice", "100", "https://arxiv.org/abs/2506.24056v2", limits=SpendingLimits())
    assert queue.enqueue("1", "30", None, "alice", "100", "https://arxiv.org/abs/2506.24056v2", limits=SpendingLimits()) == ident
    assert queue.show(ident, "1", "30", None, "alice")["state"] == "QUEUED"
    async def run():
        assert await queue.work_once() == ident
        assert await queue.work_once() is None
    asyncio.run(run())
    assert len(calls) == 1 and queue.show(ident, "1", "30", None, "alice")["job_id"] == "job_synthetic"
    with pytest.raises(PermissionError): queue.show(ident, "2", "30", None, "bob")


def test_failure_and_revocation_do_not_replay_source_work(setup):
    arms, spaces = setup
    class Service:
        def __init__(self, *args, **kwargs): raise AssertionError("Must not ingest")
    queue = PaperSubmissions(arms, Access(), service_factory=Service)
    spaces.set_membership("project", "bob", "maintainer")
    ident = queue.enqueue("2", "21", "10", "project", "100", "https://arxiv.org/abs/2506.24056v2", limits=SpendingLimits())
    spaces.set_membership("project", "bob", "member")
    async def run():
        with pytest.raises(PermissionError): await queue.work_once()
        assert await queue.work_once() is None
    asyncio.run(run())
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT state FROM paper_submissions WHERE id=?", (ident,)).fetchone()[0] == "NEEDS_ATTENTION"
