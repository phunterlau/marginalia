"""Deterministic correctness and warm-latency benchmark for a local corpus."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from typing import Any

from .brain import Brain


@dataclass(frozen=True)
class CorpusQueryBenchmarkV1:
    papers: int
    blocks: int
    cases: int
    iterations: int
    recall_at_5: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    passed: bool
    results: tuple[dict[str, Any], ...]


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile + 0.999999) - 1))
    return ordered[index]


def _hit_arxiv(brain: Brain, record_id: str) -> str | None:
    evidence = brain.get_evidence(record_id)
    if evidence is None:
        record = brain.get_research_object(record_id)
        references = record.get("evidence", []) if record else []
        document_id = next((item.get("document_id") for item in references if item.get("document_id")), None)
    else:
        document_id = evidence.get("document_id")
    document = brain.get_document(document_id) if document_id else None
    return document.get("external_ids", {}).get("arxiv") if document else None


def benchmark_corpus_queries(
    brain: Brain,
    spec_path: str | Path,
    *,
    iterations: int = 25,
) -> CorpusQueryBenchmarkV1:
    if not 1 <= iterations <= 10_000:
        raise ValueError("iterations must be between 1 and 10000")
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    cases = spec.get("cases") if isinstance(spec, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("benchmark spec must contain a non-empty cases array")
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("query"), str):
            raise ValueError("every benchmark case requires a query")
        if not isinstance(case.get("target_arxiv"), str):
            raise ValueError("every benchmark case requires target_arxiv")

    # Warm SQLite pages and prepared FTS structures before measuring steady-state queries.
    for case in cases:
        brain.search(case["query"], limit=5)

    timings: list[float] = []
    for _ in range(iterations):
        for case in cases:
            started = perf_counter_ns()
            brain.search(case["query"], limit=5)
            timings.append((perf_counter_ns() - started) / 1_000_000)

    correct = 0
    results: list[dict[str, Any]] = []
    for case in cases:
        hits = brain.search(case["query"], limit=5)
        arxiv_ids = [_hit_arxiv(brain, hit.record_id) for hit in hits]
        rank = arxiv_ids.index(case["target_arxiv"]) + 1 if case["target_arxiv"] in arxiv_ids else None
        correct += rank is not None
        results.append({
            "name": case.get("name"), "query": case["query"],
            "target_arxiv": case["target_arxiv"], "rank": rank,
            "returned_arxiv": arxiv_ids,
        })

    with brain.store.connect() as connection:
        papers = int(connection.execute("SELECT count(*) FROM documents").fetchone()[0])
        blocks = int(connection.execute("SELECT count(*) FROM document_blocks").fetchone()[0])
    recall = correct / len(cases)
    p50 = median(timings)
    p95 = _percentile(timings, 0.95)
    maximum = max(timings)
    minimum_recall = float(spec.get("minimum_recall_at_5", 1.0))
    maximum_p95 = float(spec.get("maximum_p95_ms", 100.0))
    return CorpusQueryBenchmarkV1(
        papers, blocks, len(cases), iterations, recall,
        round(p50, 3), round(p95, 3), round(maximum, 3),
        recall >= minimum_recall and p95 <= maximum_p95,
        tuple(results),
    )
