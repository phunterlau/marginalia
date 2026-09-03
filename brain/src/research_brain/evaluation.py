"""Machine-readable retrieval evaluation for the pinned research corpus."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from .brain import Brain
from .models import RetrievalFiltersV1


@dataclass(frozen=True)
class EvaluationReport:
    cases: int
    must_retrieve_recall_at_5: float
    mean_reciprocal_rank: float
    constraint_violations: int
    unreviewed_leakage: int
    semantic_wins_over_lexical: int
    results: tuple[dict[str, Any], ...]


def evaluate(brain: Brain, spec_path: str | Path, *, semantic_live: bool = False) -> EvaluationReport:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    cases = spec.get("cases") if isinstance(spec, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("evaluation spec must contain a non-empty cases array")
    retrieved = 0
    reciprocal = 0.0
    violations = 0
    leakage = 0
    semantic_wins = 0
    results: list[dict[str, Any]] = []
    must_count = sum(bool(case.get("must_retrieve", True)) for case in cases)
    for case in cases:
        filters = RetrievalFiltersV1(**case.get("filters", {}))
        kinds = case.get("kinds")
        lexical = brain.recall(case["query"], kinds=kinds, filters=filters, limit=5)
        hits = brain.recall(case["query"], kinds=kinds, filters=filters, limit=5,
                            semantic_live=semantic_live)
        titles = [hit.title for hit in hits]
        target = case.get("target_title")
        rank = titles.index(target) + 1 if target in titles else None
        if case.get("must_retrieve", True) and rank:
            retrieved += 1
            reciprocal += 1.0 / rank
        lexical_titles = [hit.title for hit in lexical]
        if rank and target not in lexical_titles:
            semantic_wins += 1
        absent = case.get("must_exclude_title")
        if absent and absent in titles:
            violations += 1
        for hit in hits:
            if hit.record_type == "research_object" and hit.review_state != "ACCEPTED":
                leakage += 1
            structured = hit.structured or {}
            for name in ("gradients_required", "training_required", "activation_access", "weight_access"):
                wanted = getattr(filters, name)
                if wanted is not None and structured.get(name) is not wanted:
                    violations += 1
        results.append({"name": case.get("name"), "query": case["query"], "target": target,
                        "rank": rank, "lexical_rank": lexical_titles.index(target) + 1 if target in lexical_titles else None,
                        "returned_titles": titles})
    return EvaluationReport(len(cases), retrieved / must_count if must_count else 1.0,
                            reciprocal / must_count if must_count else 1.0,
                            violations, leakage, semantic_wins, tuple(results))
