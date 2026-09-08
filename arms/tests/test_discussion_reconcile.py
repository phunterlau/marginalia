import asyncio
import json
from types import SimpleNamespace

import pytest

from research_arms.discussion_reconcile import DiscussionReconciler
from test_registry import setup
from test_discussion_worker import answered
from test_discussion_edits import Access, drain, STAMP


class Rest:
    def __init__(self, status=200, code=None):
        self.status, self.code, self.routes = status, code, []
    async def get(self, route):
        self.routes.append(route)
        channel, message = route.split("/")[1::2]
        data = {"id": message, "channel_id": channel,
            "author": {"id": "1" if message == "100" else "123"},
            "content": "Updated geometry", "attachments": [], "edited_timestamp": STAMP if message == "100" else None}
        if self.status != 200: data = {"code": self.code}
        return SimpleNamespace(status_code=self.status, content=json.dumps(data).encode(), json=lambda: data)


def app(arms, rest):
    return SimpleNamespace(registry=arms, access=Access(), rest=rest, user=SimpleNamespace(id=123))


def test_reconnect_finds_edit_without_replaying_pi_and_restart_is_idempotent(setup):
    arms, spaces = setup
    turn, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    rest = Rest()
    scan = DiscussionReconciler(app(arms, rest))
    async def run():
        await drain(arms)
        scan.restart()
        assert await scan.work_once() == turn
        assert await scan.work_once() is None
        await drain(arms)
        scan.restart()
        await scan.work_once()
        await drain(arms)
    asyncio.run(run())
    assert len(spaces.open("project").search_discussions("geometry")["items"]) == 1
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM discussion_jobs").fetchone()[0] == 3
        assert db.execute("SELECT prompt FROM turns").fetchone()[0] == "Contrast directions"
    assert set(rest.routes) == {"channels/20/messages/100", "channels/21/messages/200"}


@pytest.mark.parametrize("status,code,deleted", [(404,10008,True), (404,10003,False), (403,50001,False), (429,0,False), (500,0,False)])
def test_only_verified_unknown_message_tombstones(setup, status, code, deleted):
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    scan = DiscussionReconciler(app(arms, Rest(status, code)))
    async def run():
        await drain(arms)
        scan.restart()
        await scan.work_once()
        await drain(arms)
    asyncio.run(run())
    assert bool(spaces.open("project").search_discussions("Contrast")["items"]) is not deleted


def test_unknown_and_interaction_questions_are_never_fetched(setup):
    arms, _ = setup
    conv = arms.new_conversation("1", channel_id="30")
    arms.enqueue(conv, "1", channel_id="30", message_id="100", prompt="Slash", question_is_message=False)
    legacy = arms.enqueue(conv, "1", channel_id="30", message_id="101", prompt="Legacy")
    with arms.connect() as db:
        db.execute("DELETE FROM events WHERE subject=? AND kind='question_message'", (legacy,))
    rest = Rest()
    scan = DiscussionReconciler(app(arms, rest))
    async def run():
        scan.restart()
        while await scan.work_once() is not None: pass
    asyncio.run(run())
    assert rest.routes == []


@pytest.mark.parametrize("deny_at", [1, 2])
def test_access_failure_does_not_delete(setup, deny_at):
    arms, _ = setup
    answered(arms)
    rest = Rest(404, 10008)
    client = app(arms, rest)
    calls = 0
    async def deny(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls >= deny_at: raise PermissionError()
    client.access.authorize = deny
    scan = DiscussionReconciler(client)
    async def run():
        scan.restart()
        await scan.work_once()
    asyncio.run(run())
    assert len(rest.routes) == deny_at - 1
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='discussion_message_deleted'").fetchone()[0] == 0
