from concurrent.futures import ThreadPoolExecutor

import pytest

from research_brain import Brain
from test_spaces import registry


def exchange(**overrides):
    return {"id": "turn_test", "revision": 1, "conversation_id": "conv_test", "author": "alice",
        "question": "Did we discuss contrast directions?", "answer": "We tested activation differences; the result was negative.",
        "guild_id": "10", "channel_id": "20", "message_id": "100", "answer_message_id": "101",
        "pi_entry_id": "entry_test", "deleted": False, "recorded_at": "2026-09-07T12:00:00+00:00", **overrides}


def test_index_is_separate_from_scientific_memory_and_attribution_is_explicit(tmp_path):
    brain = Brain(tmp_path / "brain")
    value = exchange()
    assert brain.record_discussion(value)["created"]
    assert not brain.record_discussion(value)["created"]
    hits = brain.search_discussions("activation differences")["items"]
    assert len(hits) == 1 and hits[0]["question_author"] == "alice" and hits[0]["answer_author"] == "assistant"
    assert hits[0]["answer_url"] == "https://discord.com/channels/10/20/101"
    assert brain.recall("activation differences") == []
    assert brain.search("activation differences") == []
    assert brain.list_research_objects() == []


def test_edits_deletion_and_out_of_order_revisions_never_restore_old_search_text(tmp_path):
    brain = Brain(tmp_path / "brain")
    brain.record_discussion(exchange())
    brain.record_discussion(exchange(revision=3, answer="Corrected statement about geometry"))
    brain.record_discussion(exchange(revision=2, answer="Intermediate statement"))
    assert brain.search_discussions("negative")["items"] == []
    assert len(brain.search_discussions("geometry")["items"]) == 1
    brain.record_discussion(exchange(revision=4, deleted=True, question="", answer=""))
    assert brain.search_discussions("geometry")["items"] == []
    with brain.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM discussion_revisions").fetchone()[0] == 4
    with pytest.raises(ValueError): brain.record_discussion(exchange(revision=4, deleted=True, answer="changed"))
    with pytest.raises(ValueError): brain.record_discussion(exchange(revision=5, author="bob"))


def test_scope_isolation_and_attribution(registry):
    shared = registry.scope("alice", conversation_id="conv_test", writable_space="project")
    personal = registry.scope("alice", conversation_id="conv_test", writable_space="personal", read_spaces=("project",))
    registry.record_discussion(personal, exchange(guild_id=None, question="PRIVATE_CANARY research question"))
    assert registry.read(shared, "project", "search_discussions", "PRIVATE_CANARY")["result"]["items"] == []
    with pytest.raises(PermissionError): registry.read(shared, "personal", "search_discussions", "PRIVATE_CANARY")
    with pytest.raises(PermissionError): registry.record_discussion(shared, exchange(guild_id=None))
    with pytest.raises(PermissionError): registry.record_discussion(shared, exchange(author="bob"))
    with pytest.raises(PermissionError): registry.read(shared, "project", "record_discussion", exchange())
    assert len(registry.read(personal, "personal", "search_discussions", "PRIVATE_CANARY")["result"]["items"]) == 1


def test_bounded_results_and_missing_history_notice(tmp_path):
    brain = Brain(tmp_path / "brain")
    brain.record_discussion(exchange(answer="geometry " * 1000))
    result = brain.search_discussions("geometry")
    assert result["items"][0]["omitted_answer_characters"] == 7000
    assert "does not prove" in brain.search_discussions("absent")["notice"]
    with pytest.raises(ValueError): brain.search_discussions("query", limit=100)
    with pytest.raises(ValueError): brain.search_discussions("*")


def test_concurrent_duplicate_projection_records_once(tmp_path):
    brain = Brain(tmp_path / "brain")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(brain.record_discussion, [exchange(), exchange()]))
    assert sorted(value["created"] for value in results) == [False, True]
    assert len(brain.search_discussions("contrast")["items"]) == 1


def test_explicit_backed_up_migration_adds_discussions(tmp_path):
    from research_brain.maintenance import migrate
    import sqlite3
    root = tmp_path / "brain"
    brain = Brain(root)
    existing = brain.create_research_object(kind="note", body="Existing memory")
    with brain.store.connect() as db:
        db.execute("DROP TABLE discussion_fts")
        db.execute("DROP TABLE discussion_heads")
        db.execute("DROP TABLE discussion_revisions")
        db.execute("DELETE FROM schema_migrations WHERE version=3")
    with pytest.raises(ValueError, match="Incompatible"): Brain(root, initialize=False)
    migrate(root, tmp_path / "backup")
    reopened = Brain(root, initialize=False)
    assert reopened.get_research_object(existing.id)["body"] == "Existing memory"
    reopened.record_discussion(exchange())
    assert len(reopened.search_discussions("contrast")["items"]) == 1
    with sqlite3.connect(tmp_path / "backup" / "brain.sqlite3") as db:
        assert [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2]
