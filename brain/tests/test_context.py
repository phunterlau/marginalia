from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile

import pytest

from research_brain import Brain, RetrievalFiltersV1


@pytest.fixture
def seeded() -> tuple[Brain, dict[str, str]]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        paper = root / "paper.md"
        paper.write_text(
            "# Covariance method\n\nA covariance basis ranks high-variance activation directions.\n",
            encoding="utf-8",
        )
        brain = Brain(root / "brain")
        brain.ingest(paper)
        block = next(hit for hit in brain.search("covariance basis") if hit.record_type == "document_block")
        method = brain.create_research_object(
            kind="method_card", title="Covariance basis", body="Rank activation directions by covariance.",
            structured={
                "schema": "MethodCardV1", "gradients_required": False, "training_required": False,
                "activation_access": True, "weight_access": False,
                "failure_modes": ["Variance may not imply causal relevance"],
            },
            origin="AGENT_EXTRACTED", review_state="ACCEPTED",
            evidence=[(block.record_id, "defines", None)],
        )
        brain.create_research_object(
            kind="method_card", title="Unreviewed covariance claim", body="covariance basis speculation",
            structured={"schema": "MethodCardV1", "gradients_required": False},
            origin="AGENT_EXTRACTED", review_state="UNREVIEWED",
        )
        thread = brain.create_thread(
            "Principled perturbation directions", goal="Compare principled covariance directions.",
            constraints=["low compute", "prefer no gradients"],
            unknown=["Does variance track causal relevance?"],
            pending_decisions=["Choose covariance or contrastive basis"],
        )
        question = brain.create_research_question(
            "Which covariance basis should be tested next?", thread_id=thread.id,
        )
        hypothesis = brain.create_hypothesis(
            "High-variance directions are useful perturbation candidates.", thread_id=thread.id,
        )
        result = brain.create_research_object(
            kind="experiment_result", title="Matched-norm result",
            body="Covariance directions produced weak behavioral effects.",
            structured={"schema": "ExperimentResultV1"},
            origin="EXPERIMENT_OBSERVED", review_state="ACCEPTED",
        )
        observation = brain.record_observation(
            "Covariance directions were weak under matched norm.", thread_id=thread.id,
            conditions={"norm": 1.0, "prompt_set": "fixture"}, evidence_refs=[result.id],
        )
        tension = brain.record_tension(
            "High variance but weak behavioral effect.", thread_id=thread.id,
            side_a=[hypothesis.id], side_b=[observation.id],
            possible_explanations=["Variance is observational rather than causal"],
        )
        episode = brain.record_usage_episode(
            "The covariance basis did not discriminate hypotheses.",
            candidate="covariance basis", disposition="insufficient_evidence",
            reason="Weak effect under matched norm.",
            what_would_reconsider="Replicated gains over random controls.", thread_id=thread.id,
        )
        yield brain, {
            "thread": thread.id, "method": method.id, "question": question.id,
            "hypothesis": hypothesis.id, "observation": observation.id,
            "tension": tension.id, "episode": episode.id,
        }


def packet_ids(packet: object) -> list[str]:
    data = asdict(packet)
    result = []
    for name in (
        "relevant_memory", "historical_attempts", "counterevidence", "tensions",
        "optional_distant_connections",
    ):
        result.extend(item["record_id"] for item in data[name])
    return result


def test_analysis_packet_is_frontier_first_compact_and_deduplicated(seeded: tuple[Brain, dict[str, str]]) -> None:
    brain, ids = seeded
    packet = brain.context("covariance perturbation basis", thread_id=ids["thread"], mode="analysis", limit=8)
    assert packet.frontier["record_id"] == ids["thread"]
    assert packet.frontier["state"]["unknown"] == ["Does variance track causal relevance?"]
    assert ids["question"] in packet_ids(packet)
    assert ids["method"] in packet_ids(packet)
    assert len(packet_ids(packet)) <= 8
    assert len(packet_ids(packet)) == len(set(packet_ids(packet)))
    assert "Unreviewed covariance claim" not in {item["title"] for item in packet.relevant_memory}
    assert len(json.dumps(asdict(packet))) < 64_000


def test_critique_prioritizes_tension_negative_history_and_observation(seeded: tuple[Brain, dict[str, str]]) -> None:
    brain, ids = seeded
    packet = brain.context("why might covariance be wrong", thread_id=ids["thread"], mode="critique", limit=6)
    assert [item["record_id"] for item in packet.tensions] == [ids["tension"]]
    assert ids["episode"] in [item["record_id"] for item in packet.counterevidence]
    assert ids["observation"] in [item["record_id"] for item in packet.counterevidence]
    assert packet.counterevidence[0]["structured"]["what_would_reconsider"] == "Replicated gains over random controls."


def test_recall_returns_old_history_and_filters_cards_fail_closed(seeded: tuple[Brain, dict[str, str]]) -> None:
    brain, ids = seeded
    packet = brain.context("covariance basis", thread_id=ids["thread"], mode="recall", limit=5)
    assert ids["episode"] in [item["record_id"] for item in packet.historical_attempts]
    blocked = brain.context(
        "covariance basis", filters=RetrievalFiltersV1(activation_access=False), mode="recall", limit=5,
    )
    assert ids["method"] not in packet_ids(blocked)


def test_brainstorm_requires_and_preserves_blind_first_pass(seeded: tuple[Brain, dict[str, str]]) -> None:
    brain, ids = seeded
    with pytest.raises(ValueError, match="blind_first"):
        brain.context("other bases", thread_id=ids["thread"], mode="brainstorm")
    packet = brain.context(
        "covariance perturbation basis", thread_id=ids["thread"], mode="brainstorm",
        blind_first="Consider contrasts, random bases, and local finite differences.", limit=6,
    )
    assert packet.blind_first.startswith("Consider contrasts")
    assert ids["method"] in [item["record_id"] for item in packet.optional_distant_connections]


def test_empty_context_returns_explicit_corpus_mismatch() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        brain = Brain(Path(temporary) / "brain")
        packet = brain.context("globally optimal assumption-free direction", mode="analysis")
        assert packet.corpus_mismatch == "No compatible frontier object or reviewed memory was found in the local corpus."
        assert packet_ids(packet) == []
