from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from research_brain import Brain
from research_brain.ingest import ResolvedSource
from research_brain.jobs import AbsorptionJobs, JobStopped, SpendingLimits
from test_milestones import FakeEmbeddingProvider, FakeExtractionProvider, PAPER


@pytest.fixture
def jobs(tmp_path):
    brain = Brain(tmp_path / "brain")
    source = ResolvedSource(PAPER.encode(), "https://arxiv.org/src/2506.24056v2", "paper.md",
                            "text/markdown", "text", "https://arxiv.org/abs/2506.24056", "v2", {"arxiv": "2506.24056"})
    ingested = brain.ingestor._ingest_resolved(source)
    jobs = AbsorptionJobs(brain, "personal", create=True)
    jobs.fixture_source = source
    jobs.fixture_document = ingested.document_id
    jobs.fixture_compilation = ingested.compilation_id
    return jobs


def enqueue(jobs, **kwargs):
    return jobs.enqueue(jobs.fixture_document, jobs.fixture_compilation, **kwargs)


def approve(jobs, job):
    return jobs.approve(job["id"], job["plan_digest"], live=True)


def work(jobs, **kwargs):
    return jobs.work_once(extraction_factory=kwargs.get("factory", FakeExtractionProvider),
                          embedding_factory=FakeEmbeddingProvider)


def test_offline_complete_unreviewed_and_scoped(jobs):
    job = enqueue(jobs)
    assert job["status"] == "WAITING_APPROVAL"
    assert work(jobs)["status"] == "IDLE"
    approve(jobs, job)
    result = work(jobs)
    assert result["status"] == "COMPLETE", result
    assert result["calls_reserved"] >= 3
    assert all(step["result_json"] for step in result["steps"])
    with jobs.brain.store.connect() as db:
        assert {r[0] for r in db.execute("SELECT review_state FROM research_objects")} == {"UNREVIEWED"}
        assert {r[0] for r in db.execute("SELECT status FROM generation_runs")} == {"complete"}
    assert jobs.brain.recall("paired", kinds=["method_card"]) == []
    assert enqueue(jobs)["id"] == job["id"]
    assert work(jobs)["status"] == "IDLE"


def test_stale_or_non_live_approval_fails(jobs):
    job = enqueue(jobs)
    with pytest.raises(ValueError):
        jobs.approve(job["id"], "bad", live=True)
    with pytest.raises(ValueError):
        jobs.approve(job["id"], job["plan_digest"], live=False)
    assert jobs.show(job["id"])["status"] == "WAITING_APPROVAL"


def test_duplicates_and_atomic_claim(jobs):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enqueue(jobs), range(2)))
    assert results[0]["id"] == results[1]["id"]
    assert len(jobs.list()) == 1
    approve(jobs, results[0])
    with jobs.worker_lock():
        with pytest.raises(JobStopped):
            work(jobs)


def test_token_budget_prevents_dispatch(jobs):
    job = enqueue(jobs, limits=SpendingLimits(max_calls=32, max_reserved_tokens=1))
    approve(jobs, job)
    result = work(jobs)
    assert result["status"] == "FAILED"
    assert result["calls_reserved"] == 0
    assert result["calls"] == []


def test_call_budget_keeps_completed_tasks(jobs):
    job = enqueue(jobs, limits=SpendingLimits(max_calls=1))
    approve(jobs, job)
    result = work(jobs)
    assert result["status"] == "FAILED"
    assert result["calls_reserved"] == 1
    assert next(s for s in result["steps"] if s["task"] == "methods")["result_json"]
    retry = jobs.retry(job["id"])
    assert retry["status"] == "WAITING_APPROVAL"
    assert retry["calls_reserved"] == 1  # Retrying never resets the approved budget.


def test_unknown_provider_failure_requires_ack_and_no_replay(jobs):
    class Unknown(FakeExtractionProvider):
        calls = 0
        def extract(self, **kwargs):
            type(self).calls += 1
            raise TimeoutError("unknown transport outcome")
    job = enqueue(jobs)
    approve(jobs, job)
    result = work(jobs, factory=Unknown)
    assert result["status"] == "NEEDS_ATTENTION"
    assert Unknown.calls == 1
    assert work(jobs)["status"] == "IDLE"
    with pytest.raises(ValueError, match="ack-uncertain"):
        jobs.retry(job["id"])
    retry = jobs.retry(job["id"], acknowledge_uncertain=True)
    assert retry["status"] == "WAITING_APPROVAL"
    approve(jobs, retry)
    assert work(jobs)["status"] == "COMPLETE"


def test_cancel_prevents_next_call(jobs):
    job = enqueue(jobs)
    class Cancels(FakeExtractionProvider):
        def extract(self, **kwargs):
            jobs.cancel(job["id"])
            return super().extract(**kwargs)
    approve(jobs, job)
    result = work(jobs, factory=Cancels)
    assert result["status"] == "CANCELLED"
    assert result["calls_reserved"] == 1


def test_interrupted_worker_not_reclaimed_for_paid_work(jobs):
    class Dies(FakeExtractionProvider):
        def extract(self, **kwargs):
            raise KeyboardInterrupt()
    job = enqueue(jobs)
    approve(jobs, job)
    with pytest.raises(KeyboardInterrupt):
        work(jobs, factory=Dies)
    assert jobs.show(job["id"])["status"] == "RUNNING"
    assert work(jobs)["status"] == "IDLE"
    result = jobs.show(job["id"])
    assert result["status"] == "NEEDS_ATTENTION"
    assert result["calls"][0]["status"] == "DISPATCHED"
    retry = jobs.retry(job["id"], acknowledge_uncertain=True)
    approve(jobs, retry)
    assert work(jobs)["status"] == "COMPLETE"


def test_wrong_space_database_and_missing_ids(jobs):
    with pytest.raises(ValueError, match="wrong-space"):
        AbsorptionJobs(jobs.brain, "shared")
    with pytest.raises(LookupError, match="unavailable"):
        jobs.show("job_nonexistent")
    with pytest.raises(ValueError):
        jobs.absorb("https://evil.test/2506.24056")


def test_pinned_embedding_selection_excludes_other_documents(jobs, tmp_path):
    other = jobs.brain.ingestor._ingest_resolved(replace(jobs.fixture_source,
        canonical_document_uri="https://arxiv.org/abs/2202.05262", uri="https://arxiv.org/src/2202.05262v5", version_label="v5"))
    jobs.brain.create_research_object(kind="note", body="UNRELATED_CANARY")
    job = enqueue(jobs)
    approve(jobs, job)
    assert work(jobs)["status"] == "COMPLETE"
    with jobs.brain.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM representations WHERE object_id IN (SELECT id FROM document_blocks WHERE compilation_id=?)", (other.compilation_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM representations WHERE object_id IN (SELECT id FROM research_objects WHERE kind='note')").fetchone()[0] == 0


def test_invalid_response_does_not_create_partial_cards(jobs):
    class Malformed(FakeExtractionProvider):
        def extract(self, **kwargs):
            return {"output": {"unexpected": []}, "usage": {"total_tokens": 5}}
    job = enqueue(jobs)
    approve(jobs, job)
    result = work(jobs, factory=Malformed)
    assert result["status"] == "FAILED"
    with jobs.brain.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM research_objects").fetchone()[0] == 0
    with jobs.connect() as db:
        assert json.loads(db.execute("SELECT response_json FROM calls").fetchone()[0])["usage"]["total_tokens"] == 5


@pytest.mark.parametrize("invalid", ["shape", "evidence"])
def test_invalid_chunk_stops_later_spending_and_preserves_ledger(jobs, monkeypatch, invalid):
    from research_brain import extraction
    original = extraction._method_chunks
    monkeypatch.setattr(extraction, "_method_chunks", lambda blocks: original(blocks) * 3)

    class InvalidSecond(FakeExtractionProvider):
        calls = 0

        def extract(self, **kwargs):
            type(self).calls += 1
            response = super().extract(**kwargs)
            if self.calls == 2:
                if invalid == "shape":
                    response["output"] = {"unexpected": []}
                else:
                    response["output"]["cards"][0]["mechanism"] += " [block_truncated]"
            return response

    job = enqueue(jobs)
    approve(jobs, job)
    result = work(jobs, factory=InvalidSecond)
    assert result["status"] == "FAILED"
    assert InvalidSecond.calls == result["calls_reserved"] == 2
    assert all(step["result_json"] is None for step in result["steps"])
    with jobs.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM calls WHERE status='RETURNED' AND response_json IS NOT NULL").fetchone()[0] == 2
    with jobs.brain.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM research_objects").fetchone()[0] == 0
        run = db.execute("SELECT status,raw_output_json,usage_json FROM generation_runs").fetchone()
        assert run[0] == "failed"
        assert len(json.loads(run[1])["chunk_outputs"]) == 2
        assert json.loads(run[2])["input_tokens"] == 20


def test_real_process_kill_leaves_dispatch_for_explicit_recovery(jobs):
    job = enqueue(jobs)
    approve(jobs, job)
    code = """
import os, signal, sys
from research_brain import Brain
from research_brain.jobs import AbsorptionJobs
class Die:
    def __init__(self, **kw): pass
    def extract(self, **kw): os.kill(os.getpid(), signal.SIGKILL)
AbsorptionJobs(Brain(sys.argv[1], initialize=False), 'personal').work_once(extraction_factory=Die)
"""
    result = subprocess.run([sys.executable, "-c", code, str(jobs.brain.root)], env=os.environ.copy(), timeout=15, capture_output=True)
    assert result.returncode == -9, result.stderr
    assert jobs.show(job["id"])["status"] == "RUNNING"
    assert work(jobs)["status"] == "IDLE"
    assert jobs.show(job["id"])["status"] == "NEEDS_ATTENTION"
    assert len(jobs.show(job["id"])["calls"]) == 1


def test_canonical_commit_and_job_checkpoint_gap_does_not_respend(jobs):
    job = enqueue(jobs)
    approve(jobs, job)
    original = jobs._event
    def fail_checkpoint(db, job_id, kind, **kwargs):
        if kind == "task_complete":
            raise OSError("injected checkpoint failure")
        return original(db, job_id, kind, **kwargs)
    with patch.object(jobs, "_event", side_effect=fail_checkpoint):
        result = work(jobs)
    assert result["status"] == "FAILED"
    assert result["calls_reserved"] == 1
    retry = jobs.retry(job["id"])
    approve(jobs, retry)
    result = work(jobs)
    assert result["status"] == "COMPLETE"
    assert len([c for c in result["calls"] if c["task"] == "methods"]) == 1


def test_extraction_commit_rolls_back_cards_and_events_together(jobs):
    job = enqueue(jobs)
    approve(jobs, job)
    with jobs.brain.store.connect() as db:
        db.execute("CREATE TRIGGER inject_generation_failure BEFORE UPDATE OF status ON generation_runs WHEN NEW.status='complete' BEGIN SELECT RAISE(ABORT,'injected'); END")
    result = work(jobs)
    assert result["status"] == "FAILED"
    with jobs.brain.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM research_objects").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM events WHERE event_type='object_created'").fetchone()[0] == 0


def test_cli_absorb_approve_status_is_json_and_requires_named_space(jobs, tmp_path, capsys):
    from research_brain.cli import main
    from research_brain.spaces import SpaceRegistry
    registry = SpaceRegistry(tmp_path / "registry", create=True)
    registry.register("personal", kind="personal", owner="owner", root=jobs.brain.root)
    base = ["--registry", str(registry.root), "--space", "personal"]
    with patch("research_brain.ingest.resolve_source", return_value=jobs.fixture_source):
        assert main(base + ["absorb", "https://arxiv.org/abs/2506.24056v2"]) == 0
    job = json.loads(capsys.readouterr().out)
    assert job["status"] == "WAITING_APPROVAL"
    assert main(base + ["jobs", "approve", job["id"], "--plan-digest", job["plan_digest"], "--live"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "QUEUED"
    assert main(["--root", str(jobs.brain.root), "worker", "--once"]) == 2


def test_transient_retries_are_separate_budgeted_calls(jobs):
    class RateLimit(Exception):
        status_code = 429
    class Transient(FakeExtractionProvider):
        calls = 0
        def extract(self, **kwargs):
            type(self).calls += 1
            if type(self).calls <= 2:
                raise RateLimit()
            return super().extract(**kwargs)
    job = enqueue(jobs)
    approve(jobs, job)
    with patch("research_brain.extraction.time.sleep"):
        result = work(jobs, factory=Transient)
    assert result["status"] == "COMPLETE"
    assert [c["status"] for c in result["calls"][:3]] == ["ERROR", "ERROR", "RETURNED"]
    assert result["calls_reserved"] == 5


def test_approved_model_is_not_replaced_by_environment(jobs, monkeypatch):
    job = enqueue(jobs)
    approve(jobs, job)
    seen = []
    class RecordsModel(FakeExtractionProvider):
        def __init__(self, **config):
            seen.append(config["model"])
    monkeypatch.setenv("RESEARCH_EXTRACT_MODEL", "changed-after-approval")
    assert work(jobs, factory=RecordsModel)["status"] == "COMPLETE"
    assert set(seen) == {job["plan"]["extract_model"]}


def test_changed_schema_fails_before_paid_dispatch(jobs, monkeypatch):
    from research_brain import extraction
    job = enqueue(jobs)
    approve(jobs, job)
    monkeypatch.setattr(extraction, "METHOD_INSTRUCTIONS", "changed")
    result = work(jobs)
    assert result["status"] == "FAILED"
    assert result["calls_reserved"] == 0


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_provider_strict_schema_is_also_checked_locally(jobs, mutation):
    class Invalid(FakeExtractionProvider):
        def extract(self, **kwargs):
            result = super().extract(**kwargs)
            if mutation == "extra":
                result["output"]["unapproved_field"] = True
            else:
                del result["output"]["cards"][0]["gradients_required"]
            return result
    job = enqueue(jobs)
    approve(jobs, job)
    assert work(jobs, factory=Invalid)["status"] == "FAILED"
    with jobs.brain.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM research_objects").fetchone()[0] == 0
