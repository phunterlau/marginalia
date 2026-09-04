from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from research_brain import Brain
from research_brain.cli import build_parser, run


@pytest.fixture
def brain() -> Brain:
    with tempfile.TemporaryDirectory() as temporary:
        yield Brain(Path(temporary) / "brain")


def test_thread_frontier_update_is_validated_searchable_and_audited(brain: Brain) -> None:
    thread = brain.create_thread(
        "Principled perturbation directions",
        goal="Derive directions from transformer structure.",
        known=["Magnitude matters"],
        unknown=["Which basis is causally relevant?"],
        constraints=["low compute", "prefer no gradients"],
    )
    updated = brain.update_frontier(
        thread.id,
        {
            "goal": "Compare candidate bases under controlled interventions.",
            "unknown": ["Does covariance track causal relevance?"],
            "pending_decisions": ["Test covariance basis"],
        },
    )
    assert updated.structured["schema"] == "ResearchThreadV1"
    assert updated.structured["pending_decisions"] == ["Test covariance basis"]
    assert updated.body == "Compare candidate bases under controlled interventions."
    assert brain.get_thread(thread.id)["structured"]["unknown"] == ["Does covariance track causal relevance?"]
    assert brain.search("covariance causal", kinds=["research_thread"])[0].record_id == thread.id

    history = brain.get_history(thread.id)
    assert [event["event_type"] for event in history] == ["object_created", "frontier_updated"]
    assert history[-1]["payload"]["before"]["unknown"] == ["Which basis is causally relevant?"]
    assert history[-1]["payload"]["after"]["unknown"] == ["Does covariance track causal relevance?"]
    with pytest.raises(ValueError, match="unknown frontier fields"):
        brain.update_frontier(thread.id, {"corpus_summary": "must not be injected"})


def test_frontier_update_and_event_are_atomic(brain: Brain) -> None:
    thread = brain.create_thread("Atomic frontier", goal="Keep state and history together.")
    before = brain.get_thread(thread.id)["structured"]
    with patch.object(brain.store, "_append_event", side_effect=RuntimeError("injected")):
        with pytest.raises(RuntimeError, match="injected"):
            brain.update_frontier(thread.id, {"known": ["partial write must roll back"]})
    assert brain.get_thread(thread.id)["structured"] == before
    assert len(brain.get_history(thread.id)) == 1


def test_observation_and_interpretation_are_separate_objects(brain: Brain) -> None:
    thread = brain.create_thread("Observation discipline", goal="Keep results separate from meaning.")
    result = brain.create_research_object(
        kind="experiment_result", title="E42 result", body="Target logit gap changed by 2.31.",
        structured={"schema": "ExperimentResultV1", "experiment": "E42"},
        origin="EXPERIMENT_OBSERVED", review_state="ACCEPTED",
    )
    observation = brain.record_observation(
        "Direction X changed the target logit gap by 2.31.",
        thread_id=thread.id,
        conditions={"model": "fixture-model", "layer": 12, "perturbation_norm": 1.0},
        evidence_refs=[result.id],
    )
    interpretation = brain.record_interpretation(
        "This is consistent with Direction X being behaviorally relevant.",
        thread_id=thread.id,
        derived_from=[observation.id],
    )
    assert observation.kind == "observation"
    assert observation.origin == "EXPERIMENT_OBSERVED"
    assert "interpretation" not in observation.structured
    assert interpretation.kind == "interpretation"
    assert interpretation.origin == "AGENT_INTERPRETED"
    assert interpretation.review_state == "UNREVIEWED"
    assert interpretation.structured["derived_from"] == [observation.id]
    with pytest.raises(ValueError, match="Expected observation"):
        brain.record_interpretation("Invalid derivation", thread_id=thread.id, derived_from=[result.id])


def test_tension_hypothesis_and_negative_usage_are_typed_and_retrievable(brain: Brain) -> None:
    thread = brain.create_thread("Covariance basis", goal="Test whether variance yields useful directions.")
    hypothesis = brain.create_hypothesis(
        "High-variance residual directions are useful perturbation candidates.", thread_id=thread.id,
    )
    result = brain.create_research_object(
        kind="experiment_result", body="Covariance directions were weak under matched norm.",
        title="Weak covariance result", structured={"schema": "ExperimentResultV1"},
        origin="EXPERIMENT_OBSERVED", review_state="ACCEPTED",
    )
    tension = brain.record_tension(
        "High activation variance but weak behavioral perturbation effect.",
        thread_id=thread.id,
        side_a=[hypothesis.id],
        side_b=[result.id],
        possible_explanations=["Variance is observational rather than causal"],
    )
    episode = brain.record_usage_episode(
        "The variance-derived basis did not discriminate the hypotheses.",
        candidate="covariance perturbation directions",
        disposition="insufficient_evidence",
        reason="High activation variance does not establish causal relevance.",
        what_would_reconsider="Consistent gains over norm-matched random directions.",
        thread_id=thread.id,
    )
    assert tension.structured["status"] == "unresolved"
    assert episode.structured["disposition"] == "insufficient_evidence"
    hits = brain.recall("variance derived basis insufficient evidence", kinds=["usage_episode"])
    assert hits[0].record_id == episode.id
    with pytest.raises(ValueError, match="unknown usage disposition"):
        brain.record_usage_episode(
            "bad", candidate="x", disposition="forgotten", reason="x",
            what_would_reconsider="y", thread_id=thread.id,
        )


def test_question_and_cli_attach_to_thread(brain: Brain) -> None:
    thread = brain.create_thread("Question genealogy", goal="Attach questions to frontier state.")
    question = brain.create_research_question(
        "Which basis should be tested next?", thread_id=thread.id,
        constraints=["equal norm"], available_access=["hidden activations"],
    )
    assert question.structured["thread_id"] == thread.id

    parser = build_parser()
    cli_thread = run(parser.parse_args([
        "--root", str(brain.root), "thread", "add", "CLI thread", "--goal", "Exercise CLI",
        "--constraints", json.dumps(["low compute"]),
    ]))
    shown = run(parser.parse_args(["--root", str(brain.root), "thread", "show", cli_thread.id]))
    assert shown["id"] == cli_thread.id
    changed = run(parser.parse_args([
        "--root", str(brain.root), "thread", "update", cli_thread.id,
        "--changes", json.dumps({"pending_decisions": ["Run E43"]}),
    ]))
    assert changed.structured["pending_decisions"] == ["Run E43"]
    history = run(parser.parse_args(["--root", str(brain.root), "history", "--thread", cli_thread.id]))
    assert history[-1]["event_type"] == "frontier_updated"


def test_hypothesis_v2_is_falsifiable_searchable_and_cli_compatible(brain: Brain) -> None:
    thread = brain.create_thread("Causal basis", goal="Discriminate causal from correlational bases.")
    hypothesis = brain.create_hypothesis(
        "A covariance eigenvector is a useful intervention direction.",
        thread_id=thread.id,
        critical_unknowns=["Does the effect survive norm-matched controls?"],
        what_would_strengthen=["Replication across three model families"],
        what_would_weaken=["No gain over random directions"],
        killer_test="A preregistered intervention shows no effect beyond random controls.",
    )
    assert hypothesis.structured["schema"] == "HypothesisV2"
    assert hypothesis.structured["killer_test"].startswith("A preregistered")
    assert brain.search("preregistered random controls", kinds=["hypothesis"])[0].record_id == hypothesis.id

    parser = build_parser()
    created = run(parser.parse_args([
        "--root", str(brain.root), "hypothesis", "add", "A second falsifiable claim",
        "--thread", thread.id,
        "--critical-unknowns", json.dumps(["Which layer?"]),
        "--what-would-strengthen", json.dumps(["Held-out replication"]),
        "--what-would-weaken", json.dumps(["Sign reversal"]),
        "--killer-test", "No effect in a powered test",
    ]))
    assert created.structured["critical_unknowns"] == ["Which layer?"]


def test_question_genealogy_is_typed_audited_and_idempotent(brain: Brain) -> None:
    thread = brain.create_thread("Question graph", goal="Keep sparse durable question history.")
    original = brain.create_research_question("Which basis is causally useful?", thread_id=thread.id)
    refinement = brain.create_research_question("Which basis beats matched random controls?", thread_id=thread.id)
    observation = brain.create_research_object(
        kind="experiment_result", title="E43", body="The activation basis beat controls.",
        structured={"schema": "ExperimentResultV1"}, origin="EXPERIMENT_OBSERVED",
    )
    link = brain.link_question(refinement.id, "REFINES", original.id, metadata={"reason": "testable"})
    brain.link_question(refinement.id, "ANSWERED_BY", observation.id)
    repeated = brain.link_question(refinement.id, "REFINES", original.id, metadata={"reason": "testable"})
    assert link == repeated
    genealogy = brain.get_question_genealogy(original.id)
    assert genealogy["links"][0]["direction"] == "incoming"
    assert genealogy["links"][0]["source_id"] == refinement.id
    assert [event["event_type"] for event in brain.get_history(refinement.id)].count("link_created") == 2
    with pytest.raises(ValueError, match="target must be a research_question"):
        brain.link_question(original.id, "SPLITS_INTO", observation.id)
    with pytest.raises(ValueError, match="cannot link to itself"):
        brain.link_question(original.id, "REFINES", original.id)

    parser = build_parser()
    child = brain.create_research_question("Which layer is most causal?", thread_id=thread.id)
    linked = run(parser.parse_args([
        "--root", str(brain.root), "question", "link", original.id, "SPLITS_INTO", child.id,
        "--metadata", json.dumps({"branch": "layer"}),
    ]))
    assert linked["metadata"] == {"branch": "layer"}
    shown = run(parser.parse_args([
        "--root", str(brain.root), "question", "genealogy", child.id,
    ]))
    assert shown["links"][0]["direction"] == "incoming"


def test_transfer_hypothesis_keeps_mapping_mismatch_and_test_explicit(brain: Brain) -> None:
    thread = brain.create_thread("Transfer", goal="Test a borrowed mathematical basis.")
    question = brain.create_research_question("Can covariance produce useful directions?", thread_id=thread.id)
    method = brain.create_research_object(
        kind="math_card", title="Covariance eigenspace", body="Project onto dominant eigenvectors.",
        structured={"schema": "MathCardV1"}, origin="SOURCE_EXPLICIT", review_state="ACCEPTED",
    )
    transfer = brain.create_transfer_hypothesis(
        target_question_id=question.id,
        source_object_id=method.id,
        mapping_claims={"dominant_eigenvectors": {"corresponds_to": "candidate directions"}},
        why_promising=["forward-only", "ranked orthogonal directions"],
        mismatches=["variance is not causal relevance"],
        proposed_test="Compare against norm-matched random directions.",
        thread_id=thread.id,
    )
    assert transfer.origin == "AGENT_PROPOSED"
    assert transfer.review_state == "UNREVIEWED"
    assert transfer.structured["mismatches"] == ["variance is not causal relevance"]
    with pytest.raises(ValueError, match="method_card or math_card"):
        brain.create_transfer_hypothesis(
            target_question_id=question.id, source_object_id=thread.id,
            mapping_claims={"x": "y"}, why_promising=["cheap"], mismatches=["weak"],
            proposed_test="Run a control.", thread_id=thread.id,
        )

    parser = build_parser()
    created = run(parser.parse_args([
        "--root", str(brain.root), "transfer", "add", question.id, method.id,
        "--mapping", json.dumps({"samples": {"corresponds_to": "activations"}}),
        "--why-promising", json.dumps(["no gradients"]),
        "--mismatches", json.dumps(["distribution-sensitive"]),
        "--proposed-test", "Hold out a prompt distribution", "--thread", thread.id,
    ]))
    assert created.structured["mapping"]["samples"]["corresponds_to"] == "activations"


def test_frontier_snapshots_are_deterministic_derived_materializations(brain: Brain) -> None:
    thread = brain.create_thread(
        "Snapshot", goal="Track the live frontier.", known=["Forward passes are available"],
        unknown=["Which layer matters?"], pending_experiments=["Sweep layer under equal norm"],
    )
    hypothesis = brain.create_hypothesis(
        "Middle layers are most causally useful.", thread_id=thread.id,
        killer_test="No layer beats random controls.",
    )
    result = brain.create_research_object(
        kind="experiment_result", body="Layer 12 changed the logit gap.",
        structured={"schema": "ExperimentResultV1"}, origin="EXPERIMENT_OBSERVED",
    )
    brain.record_observation(
        "Layer 12 changed the logit gap by 1.2.", thread_id=thread.id,
        conditions={"norm": 1.0}, evidence_refs=[result.id],
    )
    brain.record_tension(
        "High variance appears in a different layer.", thread_id=thread.id,
        side_a=[hypothesis.id], side_b=[result.id],
    )
    brain.record_usage_episode(
        "An earlier random search did not discriminate layers.",
        candidate="random search", disposition="did_not_discriminate_hypotheses",
        reason="too few prompts", what_would_reconsider="larger prompt set", thread_id=thread.id,
    )
    first = brain.create_frontier_snapshot(thread.id)
    second = brain.create_frontier_snapshot(thread.id)
    assert first.structured["snapshot_number"] == 1
    assert second.structured["snapshot_number"] == 2
    assert first.structured["established"] == second.structured["established"]
    assert first.structured["source_object_ids"] == second.structured["source_object_ids"]
    assert brain.get_latest_frontier_snapshot(thread.id)["id"] == second.id
    assert first.origin == "SYSTEM_DERIVED"
    assert "Which layer matters?" in first.body
    packet = brain.context("What is unresolved and worth testing?", thread_id=thread.id, limit=10)
    packet_ids = [item["record_id"] for item in packet.relevant_memory]
    assert second.id in packet_ids
    assert first.id not in packet_ids

    parser = build_parser()
    latest = run(parser.parse_args([
        "--root", str(brain.root), "thread", "snapshot", thread.id, "--latest",
    ]))
    assert latest["id"] == second.id
