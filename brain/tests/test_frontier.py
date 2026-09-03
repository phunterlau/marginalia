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
