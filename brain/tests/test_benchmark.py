from __future__ import annotations

import json
from pathlib import Path
import tempfile

from research_brain import Brain
from research_brain.benchmark import benchmark_corpus_queries
from research_brain.cli import main


class FixedEmbeddingProvider:
    def __init__(self, **_: object):
        pass

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


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
        assert main(["--root", str(root / "brain"), "corpus", "benchmark", str(spec),
                     "--iterations", "1"]) == 0


def test_failed_benchmark_is_reported_with_nonzero_exit(capsys: object) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        brain_root = root / "brain"
        Brain(brain_root)
        spec = root / "benchmark.json"
        spec.write_text(json.dumps({
            "cases": [{"query": "missing evidence", "target_arxiv": "none.00000"}],
        }), encoding="utf-8")
        assert main(["--root", str(brain_root), "corpus", "benchmark", str(spec),
                     "--iterations", "1"]) == 1
        output = capsys.readouterr().out  # type: ignore[attr-defined]
        assert '"passed": false' in output


def test_semantic_benchmark_materializes_queries_then_times_offline() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        paper = root / "paper.md"
        paper.write_text("# Hidden mechanism\n\nA paired latent contrast is useful.\n", encoding="utf-8")
        brain = Brain(root / "brain", embedding_provider_factory=FixedEmbeddingProvider)
        result = brain.ingest(paper)
        with brain.store.connect() as connection:
            connection.execute(
                "UPDATE documents SET external_ids_json = ? WHERE id = ?",
                (json.dumps({"arxiv": "test.semantic"}), result.document_id),
            )
        brain.index_embeddings("all", live=True)
        spec = root / "semantic.json"
        spec.write_text(json.dumps({
            "minimum_recall_at_5": 1.0, "maximum_p95_ms": 500.0,
            "cases": [{
                "name": "semantic-only", "query": "distant paraphrase",
                "target_arxiv": "test.semantic",
            }],
        }), encoding="utf-8")
        report = benchmark_corpus_queries(brain, spec, iterations=2, semantic_live=True)
        assert report.passed
        assert report.semantic_cached
        assert report.results[0]["rank"] == 1
        # A fresh Brain has no provider but can use both cached corpus and query vectors.
        offline = benchmark_corpus_queries(Brain(root / "brain"), spec, iterations=1)
        assert offline.results[0]["rank"] == 1
