"""Deterministic design validation and analysis; no model or network calls."""
import hashlib
import json
import re

import numpy as np


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def validate(spec):
    required = {"schema_version", "question", "model_id", "revision", "layer", "seed",
                "random_directions", "alphas", "device", "max_tokens", "template",
                "positive_target", "negative_target", "calibration", "evaluation"}
    if set(spec) != required or spec["schema_version"] != 1:
        raise ValueError("Unsupported schema or missing/unknown fields")
    if spec["model_id"] != "Qwen/Qwen3-0.6B" or not re.fullmatch(r"[a-f0-9]{40}", spec["revision"]):
        raise ValueError("V1 requires Qwen/Qwen3-0.6B and a pinned commit hash")
    for key, low, high in [("layer", 0, 27), ("seed", 0, 2**32-1),
                           ("random_directions", 2, 100), ("max_tokens", 16, 2048)]:
        if type(spec[key]) is not int or not low <= spec[key] <= high:
            raise ValueError(f"Invalid {key}")
    if spec["device"] not in ("cpu", "mps", "cuda"):
        raise ValueError("Unsupported device")
    a = spec["alphas"]
    if (not isinstance(a, list) or not 3 <= len(a) <= 21 or
            any(type(x) not in (int, float) or not np.isfinite(x) or abs(x) > 1 for x in a) or
            0 not in a or len(set(a)) != len(a) or any(-x not in a for x in a)):
        raise ValueError("Alphas must be unique, finite, symmetric, contain zero, and be within [-1,1]")
    if not isinstance(spec["template"], str) or spec["template"].count("{text}") != 1:
        raise ValueError("Template must contain exactly one {text}")
    spec["template"].format(text="test")
    for key in ("question", "positive_target", "negative_target"):
        if not isinstance(spec[key], str) or not spec[key].strip():
            raise ValueError(f"Invalid {key}")
    if spec["positive_target"] == spec["negative_target"]:
        raise ValueError("Targets must differ")
    seen_ids, seen_text = set(), set()
    for split in ("calibration", "evaluation"):
        pairs = spec[split]
        if not isinstance(pairs, list) or not 2 <= len(pairs) <= 200:
            raise ValueError("Each split requires 2..200 paired groups")
        for pair in pairs:
            if set(pair) != {"id", "positive", "negative"}:
                raise ValueError("Pair requires id/positive/negative")
            if not isinstance(pair["id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", pair["id"]) or pair["id"] in seen_ids:
                raise ValueError("Invalid or duplicate group ID")
            seen_ids.add(pair["id"])
            for label in ("positive", "negative"):
                text = pair[label]
                if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                    raise ValueError("Invalid prompt text")
                normalized = " ".join(text.lower().split())
                if normalized in seen_text:
                    raise ValueError("Duplicate text or calibration/evaluation leakage")
                seen_text.add(normalized)
    return spec


def budget(spec):
    calibration = 2 * len(spec["calibration"])
    evaluation = 2 * len(spec["evaluation"])
    return {"calibration_forwards": calibration, "baseline_forwards": evaluation,
            "intervention_forwards": evaluation * (2 + spec["random_directions"]) * len(spec["alphas"]),
            "backward_passes": 0, "provider_calls": 0, "downloads": 0}


def directions(positive, negative, count, seed):
    positive, negative = np.asarray(positive), np.asarray(negative)
    if positive.shape != negative.shape or positive.ndim != 2 or not np.isfinite([positive, negative]).all():
        raise ValueError("Invalid paired activations")
    delta = (positive - negative).mean(axis=0)
    pooled = np.concatenate([positive, negative])
    centered = pooled - pooled.mean(axis=0)
    _, singular, vh = np.linalg.svd(centered, full_matrices=False)
    if np.linalg.norm(delta) < 1e-10 or singular[0] < 1e-10:
        raise ValueError("Degenerate calibration activations")
    pc = vh[0]
    # PCA is unsupervised in axis selection; orientation uses calibration labels only.
    if pc @ delta < 0:
        pc = -pc
    vectors = {"mean_difference": delta, "pooled_pca": pc}
    rng = np.random.default_rng(seed)
    vectors.update({f"random_{i:03d}": rng.normal(size=delta.shape) for i in range(count)})
    vectors = {k: v / np.linalg.norm(v) for k, v in vectors.items()}
    return vectors, {"calibration_mean_residual_norm": float(np.linalg.norm(pooled, axis=1).mean()),
                     "pca_explained_variance_fraction": float(singular[0]**2 / (singular**2).sum()),
                     "mean_pca_cosine": float(vectors["mean_difference"] @ vectors["pooled_pca"])}


def interval(values, seed):
    values = np.asarray(values, dtype=float)
    means = np.random.default_rng(seed).choice(values, (2000, len(values))).mean(axis=1)
    return {"mean": float(values.mean()), "pair_bootstrap_95_percentile": np.quantile(means, [.025, .975]).tolist()}


def summarize(rows, seed):
    result = []
    for method, alpha in sorted({(r["method"], r["alpha"]) for r in rows}):
        subset = [r for r in rows if (r["method"], r["alpha"]) == (method, alpha)]
        groups = sorted({r["pair_id"] for r in subset})
        paired = [np.mean([r["gap_delta"] for r in subset if r["pair_id"] == group]) for group in groups]
        item = {"method": method, "alpha": alpha, "n_pairs": len(groups),
                "gap_delta": interval(paired, seed),
                "mean_kl_from_baseline": float(np.mean([r["kl_from_baseline"] for r in subset])),
                "label_accuracy": float(np.mean([r["correct"] for r in subset]))}
        if not method.startswith("random_"):
            contrasts = []
            for group, effect in zip(groups, paired):
                controls = [r["gap_delta"] for r in rows if r["method"].startswith("random_")
                            and r["alpha"] == alpha and r["pair_id"] == group]
                contrasts.append(effect - np.mean(controls))
            item["excess_over_random_mean"] = interval(contrasts, seed)
        result.append(item)
    return result
