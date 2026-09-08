import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from research_arms.discussion_recovery import inspect, retry
from research_arms.discussion_worker import DiscussionWorker
from test_registry import setup
from test_discussion_worker import Access, answered


def failed(arms):
    turn, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    with arms.connect() as db:
        db.execute("UPDATE discussion_jobs SET state='NEEDS_ATTENTION'")
    return turn + ":1"


def test_retry_is_scoped_explicit_and_concurrent_safe(setup):
    arms, spaces = setup
    job = failed(arms)
    assert inspect(arms, space_id="alice")["items"] == []
    with pytest.raises(ValueError, match="unavailable"): retry(arms, job, space_id="alice")
    def attempt(_):
        try: return retry(arms, job, space_id="project")
        except ValueError: return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(item is not None for item in pool.map(attempt, range(4))) == 1
    asyncio.run(DiscussionWorker(arms, Access()).work_once())
    assert len(spaces.open("project").search_discussions("Contrast")["items"]) == 1
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='discussion_retry_requested'").fetchone()[0] == 1
        assert db.execute("SELECT state FROM outbox").fetchone()[0] == "DELIVERED"
        assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1


def test_revoked_scope_cannot_retry_but_retraction_can(setup):
    arms, spaces = setup
    job = failed(arms)
    spaces.set_membership("project", "bob", None)
    with pytest.raises(PermissionError): retry(arms, job, space_id="project")
    arms.discussion_message_deleted(guild_id="10", channel_id="20", message_id="100")
    with pytest.raises(ValueError, match="Superseded"): retry(arms, job, space_id="project")
    with arms.connect() as db:
        db.execute("UPDATE discussion_jobs SET state='NEEDS_ATTENTION'")
    retry(arms, job[:-1] + "2", space_id="project")
    asyncio.run(DiscussionWorker(arms, Access()).work_once())
    assert spaces.open("project").search_discussions("Contrast")["items"] == []


def test_uncertain_already_written_projection_is_idempotent(setup):
    arms, spaces = setup
    job = failed(arms)
    retry(arms, job, space_id="project")
    asyncio.run(DiscussionWorker(arms, Access()).work_once())
    # Simulate loss of the Arms completion checkpoint after Brain committed.
    with arms.connect() as db:
        db.execute("UPDATE discussion_jobs SET state='NEEDS_ATTENTION'")
    retry(arms, job, space_id="project")
    asyncio.run(DiscussionWorker(arms, Access()).work_once())
    with spaces.open("project").store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM discussion_revisions").fetchone()[0] == 1
