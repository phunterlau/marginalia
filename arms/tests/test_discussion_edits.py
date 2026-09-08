import asyncio
from types import SimpleNamespace

import pytest

from research_arms.discussion_edits import observe
from research_arms.discussion_worker import DiscussionWorker
from test_registry import setup
from test_discussion_worker import answered


STAMP = "2026-09-07T13:00:00+00:00"


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
