from dataclasses import asdict
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from research_brain import Brain
from research_brain.cli import main, build_parser, run
from research_brain.frontier_evaluation import (
    evaluate_frontier,
    seed_frontier_fixture,
    check_packet,
    load_spec,
)

SPEC = Path(__file__).parents[1] / "evals" / "frontier-v1.json"


@pytest.fixture
def seeded_frontier(tmp_path):
    brain = Brain(tmp_path / "brain")
    return brain, seed_frontier_fixture(brain)


def test_all_eight_contracts_pass_without_providers_or_database_writes(seeded_frontier):
    brain, thread = seeded_frontier
    with patch(
        "socket.socket.connect", side_effect=AssertionError("network forbidden")
    ):
        report = evaluate_frontier(
            brain, SPEC, thread_id=thread, fixture="synthetic-frontier-v1"
        )
    assert report.passed, [(r["name"], r["failures"]) for r in report.results]
    assert report.cases == 8
    assert report.database_unchanged
    assert any(r["raw_missing_kinds"] for r in report.results)
    again = evaluate_frontier(brain, SPEC, thread_id=thread)
    assert [r["packet"] for r in report.results] == [r["packet"] for r in again.results]


def test_evaluator_detects_missing_content_labels_relations_and_locator_corruption(
    seeded_frontier,
):
    brain, thread = seeded_frontier
    report = evaluate_frontier(brain, SPEC, thread_id=thread)
    cases = load_spec(SPEC)["cases"]
    for case, result in zip(cases, report.results):
        packet = dict(result["packet"])
        packet.update(
            {
                key: []
                for key in (
                    "relevant_memory",
                    "tensions",
                    "historical_attempts",
                    "counterevidence",
                )
            }
        )
        assert check_packet(brain, packet, case), case["name"]
    packet = report.results[5]["packet"]
    question = next(r for r in packet["relevant_memory"] if r.get("question_links"))
    question["question_links"][0]["target_id"] = "obj_fabricated"
    assert "question genealogy does not match stored links" in check_packet(
        brain, packet, cases[5]
    )
    packet = report.results[3]["packet"]
    assert packet["evidence_refs"]
    packet["evidence_refs"][0]["source_member"] = "forged.tex"
    packet["relevant_memory"][0]["origin"] = "SOURCE_EXPLICIT"
    errors = check_packet(brain, packet, cases[3])
    assert any("locator" in e for e in errors)
    assert any("label mismatch" in e for e in errors)


def test_cli_fixture_does_not_touch_selected_root_and_failures_exit_nonzero(
    tmp_path, capsys
):
    root = tmp_path / "must-not-exist"
    output = tmp_path / "report.json"
    args = [
        "--root",
        str(root),
        "evaluate-frontier",
        str(SPEC),
        "--fixture",
        "--output",
        str(output),
    ]
    assert main(args) == 0
    assert not root.exists()
    assert json.loads(capsys.readouterr().out)["fixture"] == "synthetic-frontier-v1"
    assert json.loads(output.read_text())["passed"]
    original = output.read_bytes()
    assert main(args) == 2
    assert output.read_bytes() == original
    capsys.readouterr()
    spec = json.loads(SPEC.read_text())
    spec["cases"][0]["must_include_kinds"].append("missing_test_kind")
    path = tmp_path / "failing.json"
    path.write_text(json.dumps(spec))
    assert main(["evaluate-frontier", str(path), "--fixture"]) == 1
    assert not json.loads(capsys.readouterr().out)["passed"]


def test_spec_validation_and_existing_target_fail_closed(tmp_path):
    spec = json.loads(SPEC.read_text())
    spec["cases"][0]["must_include_field"] = ["typo"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="Unknown"):
        load_spec(path)
    root = tmp_path / "missing"
    with pytest.raises(Exception):
        run(
            build_parser().parse_args(
                [
                    "--root",
                    str(root),
                    "evaluate-frontier",
                    str(SPEC),
                    "--thread",
                    "obj_missing",
                ]
            )
        )
    assert not root.exists()


def test_small_budgets_and_foreign_result_references(seeded_frontier):
    brain, thread = seeded_frontier
    for mode in ("analysis", "critique", "recall", "decision"):
        for limit in (1, 3, 5, 10):
            packet = asdict(
                brain.context(
                    "E42 covariance hypothesis",
                    thread_id=thread,
                    mode=mode,
                    limit=limit,
                )
            )
            ids = [
                r["record_id"]
                for key in (
                    "relevant_memory",
                    "counterevidence",
                    "tensions",
                    "historical_attempts",
                )
                for r in packet[key]
            ]
            assert len(ids) == len(set(ids)) <= limit
            for key in (
                "relevant_memory",
                "counterevidence",
                "tensions",
                "historical_attempts",
            ):
                for item in packet[key]:
                    assert item["structured"].get("thread_id") in {None, thread}
    foreign = brain.create_thread("Foreign", goal="foreign result")
    result = brain.create_research_object(
        kind="experiment_result",
        body="E99 actual result",
        structured={"thread_id": foreign.id},
    )
    brain.record_observation(
        "E99 actually observed",
        thread_id=thread,
        conditions={"synthetic": True},
        evidence_refs=[result.id],
    )
    packet = brain.context("E99 actually observed", thread_id=thread, limit=10)
    assert result.id not in [r["record_id"] for r in packet.relevant_memory]
