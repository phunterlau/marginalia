import asyncio

from research_brain.jobs import SpendingLimits
from research_arms.submissions import PaperSubmissions
from research_arms.paid_worker import PaidAbsorptionWorker
from test_registry import setup


def test_paid_worker_opt_in_exact_job_and_fresh_access(setup):
    arms, _ = setup
    checks, dispatched = [], []
    class Access:
        async def authorize(self, actor, **kwargs): checks.append(actor)
    access = Access()
    submissions = PaperSubmissions(arms, access)
    ident = submissions.enqueue("1", "30", None, "alice", "100", "https://arxiv.org/abs/2506.24056v2", limits=SpendingLimits())
    with arms.connect() as db:
        db.execute("UPDATE paper_submissions SET state='SOURCE_READY',job_id='job_exact' WHERE id=?", (ident,))
    class Jobs:
        def show(self, job_id): return {"status": "QUEUED", "plan": {"authorization_context": {"policy": 1}},
            "events": [{"kind": "approved", "actor": "discord:1"}]}
        def work_once(self, **kwargs):
            self.authorization_check({"policy": 1})
            dispatched.append(kwargs["target_job_id"])
            return {"status": "COMPLETE"}
    class Service:
        def __init__(self, *args): self.jobs = Jobs()
        def _check(self, context): assert context == {"policy": 1}
    async def run():
        worker = PaidAbsorptionWorker(arms, access, service_factory=Service)
        assert await worker.work_once() is None and dispatched == []
        worker.enabled = True
        assert (await worker.work_once())["status"] == "COMPLETE"
        assert dispatched == ["job_exact"] and len(checks) >= 2
    asyncio.run(run())


def test_discord_revocation_blocks_paid_work(setup):
    arms, _ = setup
    class Access:
        async def authorize(self, *args, **kwargs): raise PermissionError()
    access = Access()
    queue = PaperSubmissions(arms, access)
    ident = queue.enqueue("1", "30", None, "alice", "100", "https://arxiv.org/abs/2506.24056v2", limits=SpendingLimits())
    with arms.connect() as db: db.execute("UPDATE paper_submissions SET state='SOURCE_READY',job_id='job_exact' WHERE id=?", (ident,))
    class Jobs:
        def show(self, ident): return {"status": "QUEUED", "plan": {"authorization_context": {}}, "events": [{"kind": "approved", "actor": "discord:1"}]}
        def work_once(self, **kwargs): raise AssertionError("Must not dispatch")
    class Service:
        def __init__(self, *args): self.jobs = Jobs()
    asyncio.run(PaidAbsorptionWorker(arms, access, enabled=True, service_factory=Service).work_once())
    assert queue.show(ident, "1", "30", None, "alice")["state"] == "NEEDS_ATTENTION"
