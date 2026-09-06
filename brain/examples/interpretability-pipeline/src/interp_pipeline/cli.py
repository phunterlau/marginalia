import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys

import numpy as np

from .core import budget, digest, directions, summarize, validate


def write_json(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def seal(output):
    write_json(output / "checksums.json", {p.name: file_hash(p) for p in sorted(output.iterdir()) if p.is_file()})


def verify(output):
    expected = json.loads((output / "checksums.json").read_text())
    actual = {p.name: file_hash(p) for p in output.iterdir() if p.is_file() and p.name != "checksums.json"}
    if expected != actual:
        raise ValueError("Artifact integrity check failed")
    return {"integrity": "verified", "files": len(actual), "status": json.loads((output / "status.json").read_text())["status"]}


def run(spec, output, cache_dir):
    validate(spec)
    # Offline flags also suppress background hub telemetry in supporting libraries.
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    import torch
    from .model import QwenRunner
    output.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    write_json(output / "config.json", spec)
    sources = {p.name: p.read_text() for p in Path(__file__).parent.glob("*.py")}
    write_json(output / "implementation.json", sources)
    write_json(output / "manifest.json", {
        "schema": "interpretability-run/v1", "started_at": started,
        "config_digest": digest(spec), "implementation_digest": digest(sources),
        "model_id": spec["model_id"], "resolved_revision": spec["revision"],
        "python": sys.version, "platform": platform.platform(),
        "packages": {p: importlib.metadata.version(p) for p in ("torch", "transformers", "numpy", "tokenizers", "huggingface-hub")},
        "budget": budget(spec), "dtype": "float32", "attention": "eager",
        "hook": f"model.layers.{spec['layer']} output, final prompt token only",
        "interpretation_status": "UNREVIEWED", "cache_dir": cache_dir,
    })
    runner = None
    try:
        runner = QwenRunner(spec, cache_dir)
        if hasattr(runner, "snapshot"):
            snapshot = Path(runner.snapshot)
            write_json(output / "model-files.json", {
                str(p.relative_to(snapshot)): file_hash(p) for p in sorted(snapshot.rglob("*"))
                if p.is_file() and (p.suffix in (".json", ".safetensors") or p.name in ("merges.txt", "vocab.txt"))})
        positive, negative, calibration = [], [], []
        for pair in spec["calibration"]:
            for label, destination in (("positive", positive), ("negative", negative)):
                activation, logits, ids = runner.forward(pair[label])
                destination.append(activation)
                calibration.append({"pair_id": pair["id"], "label": label, "input_ids": ids,
                                    "gap": float(logits[runner.targets[0]] - logits[runner.targets[1]])})
        vectors, calibration_summary = directions(positive, negative, spec["random_directions"], spec["seed"])
        write_json(output / "calibration.json", {"prompts": calibration, "summary": calibration_summary,
                                                "target_token_ids": runner.targets})
        np.savez(output / "directions.npz", **vectors)
        np.savez(output / "calibration-activations.npz", positive=positive, negative=negative)
        rows, baselines = [], []
        scale = calibration_summary["calibration_mean_residual_norm"]
        for pair in spec["evaluation"]:
            for label in ("positive", "negative"):
                _, baseline, ids = runner.forward(pair[label])
                base_gap = float(baseline[runner.targets[0]] - baseline[runner.targets[1]])
                baselines.append({"pair_id": pair["id"], "label": label, "input_ids": ids, "gap": base_gap,
                                  "top_token_ids": baseline.topk(min(5, len(baseline))).indices.tolist(),
                                  "top_token_text": ([runner.tokenizer.decode([int(i)]) for i in baseline.topk(5).indices]
                                                     if hasattr(runner, "tokenizer") else None),
                                  "correct": (base_gap > 0) if label == "positive" else (base_gap < 0)})
                for method, vector in vectors.items():
                    for alpha in spec["alphas"]:
                        injection = torch.tensor(vector * alpha * scale, dtype=torch.float32)
                        _, changed, _ = runner.forward(pair[label], injection)
                        gap = float(changed[runner.targets[0]] - changed[runner.targets[1]])
                        kl = float((baseline.exp() * (baseline - changed)).sum())
                        if alpha == 0 and not torch.allclose(baseline, changed, atol=1e-6, rtol=0):
                            raise ValueError("Zero-intervention control changed outputs")
                        rows.append({"pair_id": pair["id"], "label": label, "method": method, "alpha": alpha,
                                     "injection_l2": float(torch.linalg.vector_norm(injection)),
                                     "baseline_gap": base_gap, "gap": gap, "gap_delta": gap - base_gap,
                                     "kl_from_baseline": kl,
                                     "correct": (gap > 0) if label == "positive" else (gap < 0)})
                print(f"Evaluated {pair['id']} / {label}; {runner.calls} forwards", flush=True)
        write_json(output / "measurements.json", {"baselines": baselines, "interventions": rows})
        stats = summarize(rows, spec["seed"])
        write_json(output / "observations.json", {
            "kind": "MEASURED", "baseline_label_accuracy": float(np.mean([b["correct"] for b in baselines])),
            "calibration": calibration_summary, "conditions": stats,
            "forward_passes": runner.calls, "backward_passes": 0, "provider_calls": 0})
        write_json(output / "interpretations.json", {
            "kind": "PROPOSED", "review_state": "UNREVIEWED", "claims": [],
            "limitations": ["Handwritten pilot: four held-out topic pairs are not a representative benchmark.",
                "Held-out topics share the calibration template; unseen-template transfer is untested.",
                "Measures one-step label logits, not open-ended behavior or a uniquely identified circuit.",
                "Direction construction needs activation and model execution access, but no gradients or training.",
                "Bootstrap resamples topic pairs, conditional on this calibration set and fixed random directions.",
                "Only one model, layer, seed and small magnitude grid; no layer selection on evaluation results.",
                "Positive-direction intervention is not expected to improve negative-label accuracy.",
                "Matched random controls and KL describe specificity, not a proof of a semantic mechanism."],
            "next_tests": ["Freeze a larger independent topic and template holdout before tuning.",
                           "Replicate with additional calibration sets and random seeds.",
                           "Add unrelated-task retention and multi-token behavior measures."]})
        report = ["# Sentiment direction pilot", "", "Measured results; scientific interpretation remains unreviewed.", "",
                  f"Baseline two-label accuracy: {np.mean([b['correct'] for b in baselines]):.1%} on {len(baselines)} prompts. This does not measure open-ended task success.", "",
                  "| Method | Alpha | Mean logit-gap change | Excess over random mean |", "|---|---:|---:|---:|"]
        for s in stats:
            if not s["method"].startswith("random_"):
                report.append(f"| {s['method']} | {s['alpha']} | {s['gap_delta']['mean']:.4f} | {s['excess_over_random_mean']['mean']:.4f} |")
        report += ["", "Gap = log P(positive) - log P(negative). Alpha multiplies the calibration mean residual L2 norm.",
                   "", "Intervals, every random control, accuracy and KL are in observations.json; raw per-prompt results are in measurements.json.",
                   "The two candidate directions may be nearly collinear; see mean_pca_cosine in calibration.json.",
                   "This is a small diagnostic experiment, not evidence of general interpretability success."]
        with (output / "report.md").open("x") as stream:
            stream.write("\n".join(report) + "\n")
        write_json(output / "status.json", {"status": "complete", "completed_at": datetime.now(timezone.utc).isoformat()})
    except Exception as exc:
        write_json(output / "status.json", {"status": "failed", "error": str(exc),
                   "forward_passes": runner.calls if runner else 0, "completed_at": datetime.now(timezone.utc).isoformat()})
        raise
    finally:
        seal(output)
    return verify(output)


def main():
    parser = argparse.ArgumentParser(description="Offline forward-only interpretability pilot")
    sub = parser.add_subparsers(dest="command", required=True)
    dry = sub.add_parser("plan")
    dry.add_argument("config", type=Path)
    execute = sub.add_parser("run")
    execute.add_argument("config", type=Path)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--cache-dir")
    check = sub.add_parser("verify")
    check.add_argument("output", type=Path)
    research = sub.add_parser("research", help="Run an isolated Brain-backed baseline diagnostic (default: dry-run)")
    research.add_argument("--source-brain", type=Path, required=True)
    research.add_argument("--pilot", type=Path, required=True)
    research.add_argument("--protocol", type=Path, required=True)
    research.add_argument("--output", type=Path, required=True)
    research.add_argument("--cache-dir")
    research.add_argument("--live", action="store_true", help="Authorize local forward passes, never provider calls")
    research_check = sub.add_parser("research-verify")
    research_check.add_argument("output", type=Path)
    pi_review = sub.add_parser("pi-review", help="Read-only Pi synthesis; default dry-run, --live uses the existing login")
    pi_review.add_argument("run", type=Path)
    pi_review.add_argument("--brain-repo", type=Path, required=True)
    pi_review.add_argument("--brain-python", type=Path, required=True)
    pi_review.add_argument("--output", type=Path, required=True)
    pi_review.add_argument("--live", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "pi-review":
            from .pi_review import review
            result = review(args.run, args.brain_repo, args.brain_python, args.output, live=args.live)
        elif args.command == "research":
            from .research import run_research
            result = run_research(args.source_brain, args.pilot, json.loads(args.protocol.read_text()),
                                  args.output, args.cache_dir, live=args.live)
        elif args.command == "research-verify":
            from .research import verify_research
            result = verify_research(args.output)
        elif args.command == "verify":
            result = verify(args.output)
        else:
            spec = validate(json.loads(args.config.read_text()))
            result = ({"question": spec["question"], "config_digest": digest(spec), **budget(spec)}
                      if args.command == "plan" else run(spec, args.output, args.cache_dir))
        print(json.dumps(result, indent=2))
        if args.command == "pi-review" and result.get("returncode", 0) != 0:
            parser.exit(1)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
