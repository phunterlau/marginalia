from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json

import pytest

from research_arms.feedback import record, view
from research_arms import Unavailable
from test_registry import setup
from test_discussion_worker import answered


def test_dedup_removal_decay_and_private_shared_isolation(setup):
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    _, private_delivery = answered(arms, private=True)
    arms.confirm_delivery(private_delivery["delivery_id"], "201")
    shared = spaces.scope("alice", conversation_id="feedback", writable_space="project")
    private = spaces.scope("alice", conversation_id="feedback", writable_space="alice", read_spaces=("project",))
    args = {"guild": "10", "channel": "21", "message": "200", "emoji": "🔥", "active": True}
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: record(arms, shared, **args), range(4))) == 1
    record(arms, private, guild=None, channel="30", message="201", emoji="🔥", active=True)
    record(arms, private, guild=None, channel="30", message="201", emoji="🔬", active=True)
    assert len(view(arms, private)["items"]) == 3
    project = view(arms, shared, shared=True)
    assert len(project["items"]) == 1 and project["items"][0]["message_id"] == "200"
    with arms.connect(readonly=True) as db:
        stamp = json.loads(db.execute("SELECT subject FROM events WHERE kind='feedback_signal' ORDER BY id LIMIT 1").fetchone()[0])["recorded_at"]
        assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 2
    future = datetime.fromisoformat(stamp) + timedelta(days=14)
    assert view(arms, shared, shared=True, at=future)["items"][0]["score"] == pytest.approx(0.5)
    assert record(arms, shared, **{**args, "active": False})
    assert not record(arms, shared, **{**args, "active": False})
    assert not view(arms, shared, shared=True)["items"]
    with pytest.raises(Unavailable): view(arms, shared, shared=False)
    with pytest.raises(Unavailable): record(arms, shared, guild=None, channel="30", message="201", emoji="🔥", active=True)


def test_revoked_contributor_disappears_and_can_withdraw(setup):
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    bob = spaces.scope("bob", conversation_id="feedback", writable_space="project")
    args = {"guild": "10", "channel": "21", "message": "200", "emoji": "🔥"}
    record(arms, bob, **args, active=True)
    spaces.set_membership("project", "bob", None)
    alice = spaces.scope("alice", conversation_id="feedback", writable_space="project")
    assert not view(arms, alice, shared=True)["items"]
    assert record(arms, bob, **args, active=False)
    with pytest.raises(PermissionError): record(arms, bob, **args, active=True)
