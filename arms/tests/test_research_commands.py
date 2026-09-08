import asyncio
import json

import pytest

from research_arms.research_commands import recall
from research_arms.research_commands import compare
from research_arms.research_commands import frontier
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


def paper(brain, revision, text):
    import io
    import tarfile
    from research_brain.ingest import ResolvedSource
    stream = io.BytesIO()
    data = ("\\documentclass{article}\n\\begin{document}\n\\section{Method}\n" + text + "\n\\end{document}").encode()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        member = tarfile.TarInfo("main.tex")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    return brain.ingestor._ingest_resolved(ResolvedSource(data=stream.getvalue(),
        uri="https://arxiv.org/src/2506.24056" + revision, name="synthetic.tar.gz", content_type="application/gzip",
        kind="source_archive", canonical_document_uri="https://arxiv.org/abs/2506.24056", version_label=revision,
        external_ids={"arxiv": "2506.24056"}))


def test_compare_pins_coexisting_revisions_and_does_not_mutate(setup):
    arms, spaces = setup
    brain = spaces.open("alice")
    first = paper(brain, "v1", "Geometry OLD_EVIDENCE")
    paper(brain, "v2", "Geometry NEW_EVIDENCE")
    with brain.store.connect() as db: before = list(db.iterdump())
    pins = first.document_id + "@v1 " + first.document_id + "@v2"
    result = asyncio.run(compare(arms, "1", {"space_id": "alice"}, pins, "Geometry"))
    a, b = result["papers"]
    assert a["revision"] == "v1" and b["revision"] == "v2"
    assert "OLD_EVIDENCE" in json.dumps(a) and "NEW_EVIDENCE" not in json.dumps(a)
    assert "NEW_EVIDENCE" in json.dumps(b) and "OLD_EVIDENCE" not in json.dumps(b)
    assert a["document_version_id"] != b["document_version_id"]
    assert str(brain.root) not in json.dumps(result)
    with brain.store.connect() as db: assert list(db.iterdump()) == before
    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(compare(arms, "1", {"space_id": "project"}, pins, "Geometry"))
    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(compare(arms, "1", {"space_id": "alice"}, pins.replace("@v2", "@v3"), "Geometry"))


@pytest.mark.parametrize("pins", ["doc_x", "doc_x@latest doc_y@v2", "doc_x@v1 doc_x@v1", "../private@v1 doc_x@v2"])
def test_compare_requires_explicit_distinct_pins(setup, pins):
    arms, _ = setup
    with pytest.raises(ValueError): asyncio.run(compare(arms, "1", {"space_id": "alice"}, pins, "Geometry"))


def test_frontier_paginates_current_labeled_records_without_snapshot_writes(setup):
    arms, spaces = setup
    brain = spaces.open("alice")
    thread = brain.create_thread("Private frontier", goal="PRIVATE_FRONTIER_CANARY", unknown=["Mechanism?"], pending_experiments=["Proposed control"])
    identities = []
    for index in range(7):
        identities.append(brain.create_hypothesis("Hypothesis " + str(index), thread_id=thread.id,
            origin="AGENT_PROPOSED", review_state="UNREVIEWED").id)
    brain.create_research_object(kind="frontier_snapshot", body="STALE_SNAPSHOT_CANARY", structured={"thread_id": thread.id})
    with brain.store.connect() as db: before = list(db.iterdump())
    async def run():
        first = await frontier(arms, "1", {"space_id": "alice"}, thread.id)
        value = first["result"]
        assert value["omitted_records"] == 2 and value["next_cursor"]
        assert value["thread"]["structured"]["unknown"] == ["Mechanism?"]
        assert value["thread"]["structured"]["pending_experiments"] == ["Proposed control"]
        second = (await frontier(arms, "1", {"space_id": "alice"}, thread.id, after_id=value["next_cursor"]))["result"]
        combined = value["records"] + second["records"]
        assert [r["id"] for r in combined] == sorted(identities)
        assert all(r["review_state"] == "UNREVIEWED" and r["origin"] == "AGENT_PROPOSED" for r in combined)
        assert "STALE_SNAPSHOT_CANARY" not in json.dumps(first)
        assert second["next_cursor"] is None
        with pytest.raises(LookupError): await frontier(arms, "1", {"space_id": "project"}, thread.id)
        with pytest.raises(PermissionError): await frontier(arms, "2", {"space_id": "alice"}, thread.id)
    asyncio.run(run())
    with brain.store.connect() as db: assert list(db.iterdump()) == before
