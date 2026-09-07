import pytest

from research_brain import Brain
from research_brain.store.sqlite import utc_now


def request():
    return dict(run_id="gen-test", task="methods", provider="openai", model="fixture",
                reasoning_effort="medium", prompt_version="test", schema_version="test",
                input_digest="fixed", block_ids=[], request={})


def test_failed_retry_has_new_identity_and_keeps_history(tmp_path):
    store = Brain(tmp_path).store
    first, created = store.begin_generation(**request())
    assert created
    store.finish_generation(first, status="failed", error={"test": "failed"})
    retry, created = store.begin_generation(**request())
    assert created and retry != first
    with pytest.raises(RuntimeError, match="reconcile"):
        store.begin_generation(**request())
    store.finish_generation(retry, status="complete", output={"cards": []})
    assert store.begin_generation(**request()) == (retry, False)


def test_dispatch_survives_interruption_and_final_record_is_immutable(tmp_path):
    store = Brain(tmp_path).store
    run, _ = store.begin_generation(**request())
    started = utc_now()
    store.dispatch_attempt(run_id=run, number=1, started_at=started)
    reopened = Brain(tmp_path, initialize=False).store
    with reopened.connect() as db:
        row = db.execute("SELECT outcome,completed_at FROM generation_attempts").fetchone()
        assert tuple(row) == ("dispatched", None)
    with pytest.raises(RuntimeError):
        reopened.begin_generation(**request())
    reopened.record_attempt(run_id=run, number=1, started_at=started, outcome="success", usage={"total_tokens": 7})
    reopened.record_attempt(run_id=run, number=1, started_at=started, outcome="fatal_error")
    with reopened.connect() as db:
        assert db.execute("SELECT outcome FROM generation_attempts").fetchone()[0] == "success"
