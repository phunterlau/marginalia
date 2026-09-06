from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from interp_pipeline.cli import seal, write_json
from interp_pipeline.development import aggregate, validate_development
from interp_pipeline.research import (choose_diagnostic, choose_next, disjoint,
    inventory, run_research, verify_research)


@pytest.fixture
def design():
    configs = Path(__file__).parents[1] / "configs"
    return (json.loads((configs / "baseline-development.json").read_text()),
            json.loads((configs / "sentiment.json").read_text()))


def fake_rows(protocol, qualified=True):
    return [{"template": name, "pair_id": p["id"], "label": label,
             "correct": qualified and name != "original", "target_probability_mass": 0.9}
            for name in protocol["templates"] for p in protocol["development"]
            for label in ("positive", "negative")]


def test_protocol_and_leakage(design):
    protocol, pilot = design
    validate_development(protocol, pilot)
    for previous in (pilot["calibration"][0], pilot["evaluation"][0]):
        changed = deepcopy(protocol)
        changed["development"][0] = previous
        with pytest.raises(ValueError, match="duplicate|leakage"):
            validate_development(changed, pilot)
    changed = deepcopy(protocol)
    changed["budget"]["forward_passes"] += 1
    with pytest.raises(ValueError, match="Budget"):
        validate_development(changed, pilot)


def test_both_orders_and_mass_are_required(design):
    protocol, _ = design
    rows = fake_rows(protocol)
    assert aggregate(rows, protocol)["few_shot_qualified"]
    for row in rows:
        if row["template"] == "negative_first":
            row["target_probability_mass"] = 0.01
    assert not aggregate(rows, protocol)["few_shot_qualified"]
    with pytest.raises(ValueError, match="Missing or duplicate"):
        aggregate(rows[:-1], protocol)


def test_policy_uses_packet_only():
    packet = {key: [] for key in ("relevant_memory", "historical_attempts", "counterevidence", "tensions")}
    assert choose_next(packet, "obj_result")["action"] == "inspect_missing_result"
    packet["relevant_memory"] = [{"record_id": "obj_result", "structured": {"few_shot_qualified": False}}]
    assert choose_next(packet, "obj_result")["action"] == "diagnose_label_readout"
    packet["relevant_memory"][0]["structured"]["few_shot_qualified"] = True
    assert choose_next(packet, "obj_result")["action"] == "freeze_fresh_steering_holdout"
    with pytest.raises(ValueError, match="does not establish"):
        choose_diagnostic(packet, "obj_observation")
    packet["counterevidence"] = [{"record_id": "obj_observation", "structured": {
        "conditions": {"baseline_label_accuracy": 0.5}}}]
    assert choose_diagnostic(packet, "obj_observation")["basis_ids"] == ["obj_observation"]


def test_path_isolation(tmp_path):
    for out in (tmp_path, tmp_path / "input", tmp_path / "input/sub"):
        with pytest.raises(ValueError, match="separate"):
            disjoint(out, [tmp_path / "input"])


@pytest.mark.parametrize("qualified", [True, False])
def test_brain_workflow_offline_and_auditable(tmp_path, design, monkeypatch, qualified):
    pytest.importorskip("research_brain", reason="Set PYTHONPATH to the local Brain src directory for integration tests")
    from research_brain import Brain
    protocol, pilot = design
    source, previous, output = (tmp_path / p for p in ("source", "pilot", "new-run"))
    Brain(source)
    previous.mkdir()
    write_json(previous / "config.json", pilot)
    write_json(previous / "observations.json", {"baseline_label_accuracy": .5, "conditions": []})
    write_json(previous / "status.json", {"status": "complete"})
    seal(previous)
    before = inventory(source)
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network access")
    monkeypatch.setattr(socket, "socket", forbidden)
    def fake_run(protocol, pilot, out, cache):
        # Verify the provenance and frozen protocol precede execution.
        assert json.loads((out.parent / "protocol.json").read_text()) == protocol
        assert json.loads((out.parent / "diagnostic-selection.json").read_text())["basis_ids"]
        out.mkdir()
        result = aggregate(fake_rows(protocol, qualified), protocol)
        write_json(out / "observations.json", result)
        return result
    monkeypatch.setattr("interp_pipeline.research.run_development", fake_run)
    plan = run_research(source, previous, protocol, output, None)
    assert plan["writes"] == 0 and not output.exists()
    result = run_research(source, previous, protocol, output, None, live=True)
    assert result["status"] == "complete"
    assert result["next_action"] == ("freeze_fresh_steering_holdout" if qualified else "diagnose_label_readout")
    assert inventory(source) == before
    assert verify_research(output) == result
    thread = json.loads((output / "thread.json").read_text())
    assert all(r["review_state"] == "UNREVIEWED" for r in thread["records"]
               if r["origin"] in {"AGENT_PROPOSED", "AGENT_INTERPRETED"})
    assert not any(e["event_type"] == "object_reviewed" for events in thread["history"].values() for e in events)
    with pytest.raises(FileExistsError):
        run_research(source, previous, protocol, output, None, live=True)
    (output / "report.md").write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        verify_research(output)


def test_provider_failure_preserves_failed_run(tmp_path, design, monkeypatch):
    pytest.importorskip("research_brain", reason="Set PYTHONPATH to the local Brain src directory for integration tests")
    from research_brain import Brain
    protocol, pilot = design
    source, previous, output = (tmp_path / p for p in ("source", "pilot", "failed"))
    Brain(source)
    previous.mkdir()
    write_json(previous / "config.json", pilot)
    write_json(previous / "observations.json", {"baseline_label_accuracy": .5, "conditions": []})
    write_json(previous / "status.json", {"status": "complete"})
    seal(previous)
    def fail(*args, **kwargs):
        raise RuntimeError("injected model failure")
    monkeypatch.setattr("interp_pipeline.research.run_development", fail)
    with pytest.raises(RuntimeError, match="injected"):
        run_research(source, previous, protocol, output, None, live=True)
    assert verify_research(output)["status"] == "failed"
