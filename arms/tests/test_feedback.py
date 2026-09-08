from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import asyncio
from types import SimpleNamespace

import pytest

from research_arms.feedback import record, view, clear
from research_arms.feedback import observe
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


class Emoji:
    def __init__(self, value="🔥", ident=None): self.value, self.id = value, ident
    def __str__(self): return self.value


def test_raw_events_are_scoped_and_removal_survives_revocation(setup):
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    calls = []
    class Access:
        async def authorize(self, *args, **kwargs): calls.append((args, kwargs))
    client = SimpleNamespace(registry=arms, access=Access(), user=SimpleNamespace(id=123))
    event = SimpleNamespace(user_id=2, channel_id=21, message_id=200, guild_id=10, emoji=Emoji(), member=None)
    async def run():
        assert await observe(client, event, active=True)
        assert not await observe(client, event, active=True)
        spaces.set_membership("project", "bob", None)
        assert await observe(client, event, active=False)
        assert len(calls) == 2  # Withdrawal needs no newly granted access.
        with pytest.raises(PermissionError): await observe(client, event, active=True)
        event.message_id = 999
        assert not await observe(client, event, active=True)
        event.message_id, event.emoji = 200, Emoji(ident=42)
        assert not await observe(client, event, active=True)
    asyncio.run(run())


def test_clear_is_exact_atomic_and_idempotent(setup):
    import sqlite3
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    scope = spaces.scope("alice", conversation_id="feedback", writable_space="project")
    for emoji in ("🔥", "⭐", "🔬"):
        record(arms, scope, guild="10", channel="21", message="200", emoji=emoji, active=True)
    assert clear(arms, guild="10", channel="99", message="200") == 0
    assert clear(arms, guild=None, channel="21", message="200") == 0
    assert clear(arms, guild="10", channel="21", message="200", emoji="🔥") == 1
    assert clear(arms, guild="10", channel="21", message="200", emoji="🔥") == 0
    with arms.connect() as db:
        db.execute("CREATE TRIGGER fail_clear BEFORE INSERT ON events WHEN NEW.kind='feedback_signal' AND json_extract(NEW.subject,'$.emoji')='⭐' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError): clear(arms, guild="10", channel="21", message="200")
    with arms.connect() as db: db.execute("DROP TRIGGER fail_clear")
    assert clear(arms, guild="10", channel="21", message="200") == 2
    assert clear(arms, guild="10", channel="21", message="200") == 0
