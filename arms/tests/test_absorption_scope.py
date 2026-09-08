from dataclasses import asdict

import pytest

from research_arms.absorption import ScopedAbsorption
from research_brain.ingest import ResolvedSource
from research_brain.jobs import SpendingLimits
from test_registry import setup


def test_only_owner_or_shared_maintainer_can_create_absorption_service(setup):
    arms, spaces = setup
    owner = ScopedAbsorption(arms, "1", "alice", create=True)
    owner._check(asdict(owner.scope))
    with pytest.raises(PermissionError): ScopedAbsorption(arms, "2", "alice", create=True)
    with pytest.raises(PermissionError): ScopedAbsorption(arms, "2", "project", create=True)
    assert not (spaces.open("project").root / "jobs.sqlite3").exists()
    spaces.set_membership("project", "bob", "maintainer")
    shared = ScopedAbsorption(arms, "2", "project", create=True)
    spaces.set_membership("project", "bob", "member")
    with pytest.raises(PermissionError): shared._check(asdict(shared.scope))


def test_exact_scoped_approval_is_audited_without_provider_calls(setup):
    arms, _ = setup
    service = ScopedAbsorption(arms, "1", "alice", create=True)
    source = ResolvedSource(b"# Synthetic paper\n\nA method uses paired representations.\n",
        "https://arxiv.org/src/2506.24056v2", "paper.md", "text/markdown", "text",
        "https://arxiv.org/abs/2506.24056v2", "v2", {"arxiv": "2506.24056"})
    ingested = service.jobs.brain.ingestor._ingest_resolved(source)
    job = service.jobs.enqueue(ingested.document_id, ingested.compilation_id, limits=SpendingLimits())
    with pytest.raises(ValueError): service.approve(job["id"], job["plan_digest"], live=False)
    with pytest.raises(ValueError): service.approve(job["id"], "wrong", live=True)
    assert service.preview(job["id"])["status"] == "WAITING_APPROVAL"
    result = service.approve(job["id"], job["plan_digest"], live=True)
    assert result["status"] == "QUEUED" and result["calls_reserved"] == 0
    ledger = service.jobs.show(job["id"])
    assert ledger["calls"] == []
    assert [event["actor"] for event in ledger["events"] if event["kind"] == "approved"] == ["discord:1"]
