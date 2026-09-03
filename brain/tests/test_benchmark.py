from __future__ import annotations

import json
from pathlib import Path
import tempfile

from research_brain import Brain
from research_brain.benchmark import benchmark_corpus_queries


def test_corpus_benchmark_measures_correctness_and_warm_latency() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        paper = root / "paper.md"
        paper.write_text("# Test paper\n\nA rare mechanism uses a paired latent contrast.\n", encoding="utf-8")
        brain = Brain(root / "brain")
        result = brain.ingest(paper)
        document = brain.get_document(result.document_id)
        # Local documents intentionally lack arXiv IDs, so attach a deterministic
        # target through the stored document metadata for this isolated benchmark.
        with brain.store.connect() as connection:
            connection.execute(
                "UPDATE documents SET external_ids_json = ? WHERE id = ?",
                (json.dumps({"arxiv": "test.00001"}), result.document_id),
            )
        spec = root / "benchmark.json"
        spec.write_text(json.dumps({
            "minimum_recall_at_5": 1.0, "maximum_p95_ms": 500.0,
            "cases": [{
                "name": "paired-contrast", "query": "rare paired contrast",
                "target_arxiv": "test.00001",
            }],
        }), encoding="utf-8")
        report = benchmark_corpus_queries(brain, spec, iterations=3)
        assert report.passed
        assert report.recall_at_5 == 1.0
        assert report.papers == 1
        assert report.blocks >= 1
        assert report.results[0]["rank"] == 1
