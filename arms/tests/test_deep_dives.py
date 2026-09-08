import asyncio
import pytest

from research_arms.deep_dives import preview, run
from research_arms.feedback import record
from test_registry import setup
from test_discussion_worker import answered, Access
from research_arms.discussion_worker import DiscussionWorker


def ready(arms, spaces):
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    asyncio.run(DiscussionWorker(arms, Access()).work_once())
    scope = spaces.scope("alice", conversation_id="feedback", writable_space="project")
    record(arms, scope, guild="10", channel="21", message="200", emoji="🔬", active=True)
    return scope


def test_preview_does_not_queue_and_approved_run_is_idempotent(setup):
    arms, spaces = setup
    ready(arms, spaces)
    value = preview(arms, "1", "10", "21", "200")
    with arms.connect(readonly=True) as db: assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
    args = {"digest": value["digest"], "expected_space": "project", "parent_channel_id": "20"}
    first = run(arms, "1", "10", "21", "200", request_id="500", **args)
    assert run(arms, "1", "10", "21", "200", request_id="500", **args) == first
    assert run(arms, "1", "10", "21", "200", request_id="501", **args) == first
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM turns WHERE status='QUEUED'").fetchone()[0] == 1


def test_removal_stale_digest_and_nonmaintainer_cannot_dispatch(setup):
    arms, spaces = setup
    scope = ready(arms, spaces)
    value = preview(arms, "2", "10", "21", "200")
    with pytest.raises(PermissionError):
        run(arms, "2", "10", "21", "200", digest=value["digest"], request_id="500", expected_space="project", parent_channel_id="20")
    value = preview(arms, "1", "10", "21", "200")
    with pytest.raises(ValueError):
        run(arms, "1", "10", "21", "200", digest="0" * 64, request_id="501", expected_space="project")
    record(arms, scope, guild="10", channel="21", message="200", emoji="🔬", active=False)
    with pytest.raises(ValueError):
        run(arms, "1", "10", "21", "200", digest=value["digest"], request_id="502", expected_space="project")
    with arms.connect(readonly=True) as db: assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1


def test_interrupted_setup_retains_approval_and_resumes_once(setup, monkeypatch):
    arms, spaces = setup
    ready(arms, spaces)
    value = preview(arms, "1", "10", "21", "200")
    original = arms.new_conversation
    def fail(*args, **kwargs): raise RuntimeError("Injected setup failure")
    monkeypatch.setattr(arms, "new_conversation", fail)
    args = {"digest": value["digest"], "request_id": "500", "expected_space": "project", "parent_channel_id": "20"}
    with pytest.raises(RuntimeError): run(arms, "1", "10", "21", "200", **args)
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='deep_dive_approved'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
    monkeypatch.setattr(arms, "new_conversation", original)
    run(arms, "1", "10", "21", "200", **args)
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='deep_dive_approved'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 2
