from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from research_brain import Brain
from test_discussions import exchange


def test_save_is_explicit_atomic_idempotent_and_unreviewed(tmp_path):
    brain = Brain(tmp_path)
    brain.record_discussion(exchange())
    args = {"revision": 1, "start": 0, "end": 28}
    preview = brain.discussion_excerpt("turn_test", **args)
    assert brain.list_research_objects() == []
    def save(_): return brain.save_discussion_excerpt("turn_test", **args, expected_digest=preview["digest"])
    with ThreadPoolExecutor(max_workers=4) as pool: results = list(pool.map(save, range(4)))
    assert sum(r["created"] for r in results) == 1
    obj = brain.get_research_object(results[0]["object_id"])
    assert obj["body"] == exchange()["answer"][:28]
    assert obj["origin"] == "AGENT_INTERPRETED" and obj["review_state"] == "UNREVIEWED"
    assert not obj["evidence"]
    assert brain.recall("activation") == []
    assert brain.search("activation")


def test_stale_deleted_and_failed_saves_create_no_objects(tmp_path):
    brain = Brain(tmp_path)
    brain.record_discussion(exchange())
    args = {"revision": 1, "start": 0, "end": 20}
    preview = brain.discussion_excerpt("turn_test", **args)
    with brain.store.connect() as db:
        db.execute("CREATE TRIGGER fail_saved BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        brain.save_discussion_excerpt("turn_test", **args, expected_digest=preview["digest"])
    assert brain.list_research_objects() == []
    brain.record_discussion(exchange(revision=2, deleted=True, question="", answer=""))
    with pytest.raises(ValueError): brain.save_discussion_excerpt("turn_test", **args, expected_digest=preview["digest"])
    with pytest.raises(ValueError): brain.discussion_excerpt("turn_test", revision=2, start=0, end=20)
