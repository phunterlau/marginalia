import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from research_arms.discussion_edits import observe
from research_arms.discussion_worker import DiscussionWorker
from test_registry import setup
from test_discussion_worker import answered


STAMP = "2026-09-07T13:00:00+00:00"


@pytest.mark.parametrize("outcome", ["verified", "unavailable", "deleted"])
def test_question_edit_before_answer_is_applied_at_confirmation(setup, outcome):
    arms, spaces = setup
    conv = arms.new_conversation("1", channel_id="21", guild_id="10", parent_channel_id="20")
    turn = arms.enqueue(conv, "1", channel_id="21", guild_id="10", message_id="100",
        prompt="Contrast directions", question_channel_id="20")
    async def edit():
        app = client(arms, author="999" if outcome == "unavailable" else "1")
        if outcome == "unavailable":
            with pytest.raises(PermissionError):
                await observe(app, guild="10", channel="20", message="100", edited_at=STAMP)
        else:
            if outcome == "deleted":
                arms.discussion_message_deleted(guild_id="10", channel_id="20", message_id="100")
            await observe(app, guild="10", channel="20", message="100", edited_at=STAMP)
            await observe(app, guild="10", channel="20", message="100", edited_at=STAMP)
            app.rest.data.update(content="Obsolete content", edited_timestamp="2026-09-07T12:59:00+00:00")
            await observe(app, guild="10", channel="20", message="100", edited_at=app.rest.data["edited_timestamp"])
    asyncio.run(edit())
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM discussion_jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM discussion_edits").fetchone()[0] == (1 if outcome == "unavailable" else 2)
        assert db.execute("SELECT prompt FROM turns WHERE id=?", (turn,)).fetchone()[0] == "Contrast directions"
    arms.claim()
    arms.save_answer(turn, "A recorded answer", "entry")
    delivery = arms.begin_delivery(turn)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    arms.confirm_delivery(delivery["delivery_id"], "200")
    asyncio.run(drain(arms))
    brain = spaces.open("project")
    assert brain.search_discussions("Contrast")["items"] == []
    assert brain.search_discussions("Obsolete")["items"] == []
    hits = brain.search_discussions("geometry")["items"]
    assert len(hits) == (1 if outcome == "verified" else 0)
    if hits: assert "earlier version" in hits[0]["edit_notice"]
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM discussion_jobs").fetchone()[0] == 1


class Access:
    async def authorize(self, *args, **kwargs): return {}
    async def authorize_turn(self, *args): return {}


class Rest:
    def __init__(self, author="1", content="Revised geometry question", attachments=None):
        self.calls = 0
        self.data = {"id": "100", "channel_id": "20", "author": {"id": author},
            "content": content, "attachments": attachments or [], "edited_timestamp": STAMP}
    async def get(self, route):
        self.calls += 1
        return SimpleNamespace(status_code=200, json=lambda: self.data)


def client(arms, **kwargs):
    return SimpleNamespace(registry=arms, access=Access(), rest=Rest(**kwargs), user=SimpleNamespace(id=123))


async def drain(arms):
    worker = DiscussionWorker(arms, Access())
    while await worker.work_once() is not None: pass


def test_verified_edit_updates_search_not_pi_turn_and_labels_old_answer(setup):
    arms, spaces = setup
    turn, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    async def run():
        await drain(arms)
        app = client(arms)
        await observe(app, guild="10", channel="20", message="100", edited_at=STAMP)
        await observe(app, guild="10", channel="20", message="100", edited_at=STAMP)
        await drain(arms)
    asyncio.run(run())
    brain = spaces.open("project")
    assert brain.search_discussions("Contrast")["items"] == []
    hit = brain.search_discussions("geometry")["items"][0]
    assert "earlier version" in hit["edit_notice"]
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT prompt FROM turns WHERE id=?", (turn,)).fetchone()[0] == "Contrast directions"
        assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM discussion_jobs").fetchone()[0] == 3


def test_unverified_edit_retracts_stale_search_content(setup):
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    async def run():
        await drain(arms)
        with pytest.raises(PermissionError):
            app = client(arms, author="999")
            await observe(app, guild="10", channel="20", message="100", edited_at=STAMP)
        await drain(arms)
    # Unavailable is intentionally indistinguishable from other permission denials.
    asyncio.run(run())
    assert spaces.open("project").search_discussions("Contrast")["items"] == []


def test_untracked_messages_are_not_fetched_and_deletion_cannot_be_undone(setup):
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    async def run():
        app = client(arms)
        await observe(app, guild="10", channel="20", message="999", edited_at=STAMP)
        assert app.rest.calls == 0
        arms.discussion_message_deleted(guild_id="10", channel_id="20", message_id="100")
        await observe(app, guild="10", channel="20", message="100", edited_at=STAMP)
        await drain(arms)
    asyncio.run(run())
    assert spaces.open("project").search_discussions("geometry")["items"] == []


def test_stale_edit_cannot_replace_newer_verified_revision(setup):
    arms, spaces = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    async def run():
        app = client(arms)
        await observe(app, guild="10", channel="20", message="100", edited_at=STAMP)
        app.rest.data.update(content="Obsolete content", edited_timestamp="2026-09-07T12:59:00+00:00")
        await observe(app, guild="10", channel="20", message="100", edited_at=app.rest.data["edited_timestamp"])
        await drain(arms)
    asyncio.run(run())
    assert spaces.open("project").search_discussions("Obsolete")["items"] == []
    assert len(spaces.open("project").search_discussions("geometry")["items"]) == 1


def test_attachment_answer_edit_indexes_file_and_preserves_original_pi_answer(setup, monkeypatch):
    from research_arms import discussion_edits
    arms, spaces = setup
    turn, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    data = b"Revised answer about eigenvectors"
    async def chunks(url): yield data
    monkeypatch.setattr(discussion_edits, "download_chunks", chunks)
    async def run():
        app = client(arms, author="123", content="WRAPPER_NOT_THE_ANSWER", attachments=[
            {"filename": "answer.md", "size": len(data), "url": "https://cdn.discordapp.com/attachments/21/42/answer.md"}])
        app.rest.data.update(id="200", channel_id="21")
        await observe(app, guild="10", channel="21", message="200", edited_at=STAMP)
        await drain(arms)
    asyncio.run(run())
    result = spaces.open("project").search_discussions("eigenvectors")["items"][0]
    assert result["answer"] == data.decode() and result["answer_edited_at"]
    assert spaces.open("project").search_discussions("WRAPPER_NOT_THE_ANSWER")["items"] == []
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT answer FROM turns WHERE id=?", (turn,)).fetchone()[0] == "A recorded answer"


def test_edit_ledger_and_projection_roll_back_together(setup):
    arms, _ = setup
    _, delivery = answered(arms)
    arms.confirm_delivery(delivery["delivery_id"], "200")
    with arms.connect() as db:
        db.execute("CREATE TRIGGER fail_edit BEFORE INSERT ON discussion_jobs BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        asyncio.run(observe(client(arms), guild="10", channel="20", message="100", edited_at=STAMP))
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM discussion_edits").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM discussion_jobs").fetchone()[0] == 1
