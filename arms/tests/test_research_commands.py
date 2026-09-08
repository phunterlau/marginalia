import asyncio
import json

import pytest

from research_arms.research_commands import recall
from test_registry import setup


def test_recall_is_reliable_scoped_serializable_and_refreshes_review(setup):
    arms, spaces = setup
    brain = spaces.open("project")
    accepted = brain.create_research_object(kind="note", body="Geometry accepted", origin="AGENT_PROPOSED", review_state="ACCEPTED")
    for state in ("UNREVIEWED", "DISPUTED", "REJECTED"):
        brain.create_research_object(kind="note", body="Geometry " + state, origin="AGENT_PROPOSED", review_state=state)
    async def run():
        shared = await recall(arms, "1", {"space_id": "project"}, "Geometry")
        assert [hit["record_id"] for hit in shared["result"]] == [accepted.id]
        assert shared["result"][0]["origin"] == "AGENT_PROPOSED"
        assert shared["lane"] == "reliable"
        json.dumps(shared)
        assert not (await recall(arms, "1", {"space_id": "project"}, "PRIVATE_CANARY"))["result"]
        assert (await recall(arms, "1", {"space_id": "alice"}, "PRIVATE_CANARY"))["result"]
        with pytest.raises(PermissionError):
            await recall(arms, "2", {"space_id": "alice"}, "PRIVATE_CANARY")
        brain.review_research_object(accepted.id, review_state="DISPUTED", note="Synthetic test only")
        assert not (await recall(arms, "1", {"space_id": "project"}, "Geometry"))["result"]
    asyncio.run(run())
    with arms.connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0


@pytest.mark.parametrize("query", ["", " " * 10, "x" * 2001, "bad\x00query", None])
def test_recall_rejects_invalid_questions(setup, query):
    arms, _ = setup
    with pytest.raises(ValueError): asyncio.run(recall(arms, "1", {"space_id": "alice"}, query))


def test_no_live_query_embedding_and_no_local_paths(setup, monkeypatch):
    arms, spaces = setup
    from research_brain.embeddings import EmbeddingIndexer
    calls = []
    def query_vector(self, text, *, live=False):
        calls.append(live)
        assert live is False
        return None
    monkeypatch.setattr(EmbeddingIndexer, "query_vector", query_vector)
    spaces.open("project").create_research_object(kind="note", body="Geometry", structured={"root": "/private/canary", "nested": {"local_path": "/private/secret"}})
    result = asyncio.run(recall(arms, "1", {"space_id": "project"}, "Geometry"))
    assert calls == [False]
    assert "/private/" not in json.dumps(result)
