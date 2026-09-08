import json

import pytest

from research_arms.papers import read_paper
from test_registry import setup


def test_scoped_paper_reads_are_bounded_and_do_not_mutate(setup, tmp_path):
    arms, spaces = setup
    source = tmp_path / "paper.tex"
    source.write_text("\\documentclass{article}\n\\begin{document}\n\\section{Method}\nA synthetic method paragraph.\n\\begin{equation}x=y\\end{equation}\n\\end{document}")
    brain = spaces.open("alice")
    ingested = brain.ingest(source)
    document = ingested.document_id
    blocks = brain.store.get_blocks(document)
    with brain.store.connect() as db:
        before = list(db.iterdump())
    destination = {"space_id": "alice"}
    overview = read_paper(arms, "1", destination, "brief", document)
    assert overview["result"]["source_ready"]
    assert overview["result"]["summary_kind"] == "source_metadata_only"
    assert read_paper(arms, "1", destination, "cards", document)["result"]["cards"] == []
    evidence = read_paper(arms, "1", destination, "evidence", blocks[0]["id"])
    assert evidence["result"]["source_locator"]["compilation_id"]
    assert str(tmp_path) not in json.dumps(evidence)
    conv = arms.new_conversation("1", channel_id="30")
    turn = arms.enqueue(conv, "1", channel_id="30", message_id="100", prompt="evidence")
    arms.claim()
    tool_result = arms.read(turn, "alice", "get_evidence", blocks[0]["id"])
    assert "source_path" not in tool_result["result"]
    with pytest.raises((LookupError, PermissionError)):
        read_paper(arms, "1", {"space_id": "project"}, "brief", document)
    with pytest.raises(PermissionError): read_paper(arms, "2", destination, "brief", document)
    with brain.store.connect() as db:
        assert list(db.iterdump()) == before
