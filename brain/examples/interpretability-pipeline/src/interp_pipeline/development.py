"""Frozen, offline baseline diagnostic. No fitting or activation intervention."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import importlib.metadata
import os
from pathlib import Path
import platform
import sys

from .cli import file_hash, seal, verify, write_json
from .core import validate


def validate_development(protocol, pilot):
    validate(pilot)
    if set(protocol) != {"schema", "question", "templates", "development", "gate", "selection_rule", "budget"}:
        raise ValueError("Invalid development protocol fields")
    if protocol["schema"] != "BaselineDevelopmentV1":
        raise ValueError("Unknown development schema")
    if set(protocol["templates"]) != {"original", "positive_first", "negative_first"}:
        raise ValueError("Expected original and both few-shot orders")
    if protocol["templates"]["original"] != pilot["template"]:
        raise ValueError("Original template must match the archived pilot")
    # Reuse split validation against BOTH earlier splits, not just calibration.
    spec = deepcopy(pilot)
    spec["calibration"] = pilot["calibration"] + pilot["evaluation"]
    spec["evaluation"] = protocol["development"]
    for template in protocol["templates"].values():
        spec["template"] = template
        validate(spec)
    n = len(protocol["development"])
    gate = protocol["gate"]
    if set(gate) != {"minimum_correct", "minimum_correct_per_label", "minimum_mean_target_mass"}:
        raise ValueError("Invalid gate")
    if (type(gate["minimum_correct"]) is not int or not 1 <= gate["minimum_correct"] <= 2*n
            or type(gate["minimum_correct_per_label"]) is not int
            or not 1 <= gate["minimum_correct_per_label"] <= n
            or type(gate["minimum_mean_target_mass"]) not in (float, int)
            or not 0 < gate["minimum_mean_target_mass"] <= 1):
        raise ValueError("Invalid gate thresholds")
    if protocol["budget"] != {"forward_passes": 6*n, "backward_passes": 0, "provider_calls": 0}:
        raise ValueError("Budget does not match frozen design")
    return protocol


def aggregate(rows, protocol):
    conditions = []
    for name in protocol["templates"]:
        selected = [r for r in rows if r["template"] == name]
        expected = {(p["id"], label) for p in protocol["development"] for label in ("positive", "negative")}
        if len(selected) != len(expected) or {(r["pair_id"], r["label"]) for r in selected} != expected:
            raise ValueError("Missing or duplicate development measurements")
        correct = sum(r["correct"] for r in selected)
        per_label = {label: sum(r["correct"] for r in selected if r["label"] == label)
                     for label in ("positive", "negative")}
        mass = sum(r["target_probability_mass"] for r in selected) / len(selected)
        gate = protocol["gate"]
        passed = (correct >= gate["minimum_correct"]
                  and min(per_label.values()) >= gate["minimum_correct_per_label"]
                  and mass >= gate["minimum_mean_target_mass"])
        conditions.append({"template": name, "correct": correct, "n": len(selected),
                           "correct_per_label": per_label, "mean_target_probability_mass": mass,
                           "gate_passed": passed})
    return {"conditions": conditions, "few_shot_qualified": all(
        c["gate_passed"] for c in conditions if c["template"] != "original"),
        "selection_rule": protocol["selection_rule"], "scope": "development_only"}


def run_development(protocol, pilot, output, cache_dir):
    validate_development(protocol, pilot)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    from .model import QwenRunner
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "protocol.json", protocol)
    write_json(output / "pilot-config.json", pilot)
    write_json(output / "implementation.json", {p.name: p.read_text() for p in Path(__file__).parent.glob("*.py")})
    write_json(output / "runtime.json", {"python": sys.version, "platform": platform.platform(),
        "packages": {p: importlib.metadata.version(p) for p in ("torch", "transformers", "numpy", "tokenizers", "huggingface-hub")},
        "dtype": "float32", "attention": "eager", "device": pilot["device"],
        "model_id": pilot["model_id"], "resolved_revision": pilot["revision"],
        "gradients": False, "interventions": False, "downloads": False})
    runner = None
    try:
        runner = QwenRunner(pilot, cache_dir)
        if hasattr(runner, "snapshot"):
            snapshot = Path(runner.snapshot)
            write_json(output / "model-files.json", {str(p.relative_to(snapshot)): file_hash(p)
                for p in sorted(snapshot.rglob("*")) if p.is_file()
                and (p.suffix in (".json", ".safetensors") or p.name in ("merges.txt", "vocab.txt"))})
        rows = []
        for name, template in protocol["templates"].items():
            runner.spec = {**pilot, "template": template}
            for pair in protocol["development"]:
                for label in ("positive", "negative"):
                    _, lp, ids = runner.forward(pair[label])
                    pos, neg = runner.targets
                    gap = float(lp[pos] - lp[neg])
                    top = lp.topk(5).indices.tolist()
                    rows.append({"template": name, "pair_id": pair["id"], "label": label,
                                 "input_ids": ids, "target_ids": runner.targets, "gap": gap,
                                 "correct": gap > 0 if label == "positive" else gap < 0,
                                 "target_probability_mass": float(lp[[pos, neg]].exp().sum()),
                                 "top_token_ids": top,
                                 "top_tokens": [runner.tokenizer.decode([i]) for i in top]})
            print(f"Development {name}: {runner.calls} forwards", flush=True)
        if runner.calls != protocol["budget"]["forward_passes"]:
            raise ValueError("Forward budget mismatch")
        write_json(output / "measurements.json", rows)
        result = aggregate(rows, protocol)
        write_json(output / "observations.json", result)
        write_json(output / "status.json", {"status": "complete", **protocol["budget"],
                   "completed_at": datetime.now(timezone.utc).isoformat()})
    except Exception as exc:
        write_json(output / "status.json", {"status": "failed", "error": str(exc),
                   "forward_passes": runner.calls if runner else 0})
        raise
    finally:
        seal(output)
    verify(output)
    return result
