import asyncio
import sqlite3

import pytest

from research_arms.discussion_worker import DiscussionWorker
from test_registry import setup


class Access:
    async def authorize_turn(self, ident): return {}


def answered(arms, *, private=False):
    channel, guild = ("30", None) if private else ("21", "10")
    conv = arms.new_conversation("1", channel_id=channel, guild_id=guild, parent_channel_id="20" if guild else None)
    turn = arms.enqueue(conv, "1", channel_id=channel, guild_id=guild, message_id="100",
        prompt="PRIVATE_CANARY" if private else "Contrast directions", question_channel_id="20" if guild else channel)
    arms.claim()
    arms.save_answer(turn, "A recorded answer", "entry")
    delivery = arms.begin_delivery(turn)
    return turn, delivery


def test_confirmed_delivery_queues_once_and_projects_in_original_space(setup):
    arms, spaces = setup
    turn, delivery = answered(arms)
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM discussion_jobs").fetchone()[0] == 0
    arms.confirm_delivery(delivery["delivery_id"], "200")
    arms.confirm_delivery(delivery["delivery_id"], "200")
    worker = DiscussionWorker(arms, Access())
    async def run():
        assert await worker.work_once() == turn
        assert await worker.work_once() is None
    asyncio.run(run())
    hit = spaces.open("project").search_discussions("Contrast")["items"][0]
    assert hit["question_url"] == "https://discord.com/channels/10/20/100"
    assert hit["answer_url"] == "https://discord.com/channels/10/21/200"
    assert hit["question_author"] == "alice"


def test_private_discussion_does_not_project_to_shared_space(setup):
    arms, spaces = setup
    _, delivery = answered(arms, private=True)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    asyncio.run(DiscussionWorker(arms, Access()).work_once())
    assert len(spaces.open("alice").search_discussions("PRIVATE_CANARY")["items"]) == 1
    assert spaces.open("project").search_discussions("PRIVATE_CANARY")["items"] == []


def test_projection_queue_failure_rolls_back_delivery_confirmation(setup):
    arms, _ = setup
    _, delivery = answered(arms)
    with arms.connect() as db:
        db.execute("CREATE TRIGGER fail_discussion BEFORE INSERT ON discussion_jobs BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError): arms.confirm_delivery(delivery["delivery_id"], "200")
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT state FROM outbox").fetchone()[0] == "SENDING"


def test_revoked_scope_never_projects_and_is_not_replayed(setup):
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    spaces.set_membership("project", "bob", None)
    worker = DiscussionWorker(arms, Access())
    async def run():
        with pytest.raises(PermissionError): await worker.work_once()
        assert await worker.work_once() is None
    asyncio.run(run())
    assert spaces.open("project").search_discussions("Contrast")["items"] == []
