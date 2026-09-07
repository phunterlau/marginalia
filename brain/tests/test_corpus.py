from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

import pytest

from research_brain import Brain
from research_brain.benchmark import benchmark_corpus_queries


def test_pinned_corpus_ingests_offline_with_exact_versions_and_thresholds() -> None:
    fixture_root = Path(os.getenv("RESEARCH_PAPER_FIXTURES", str(Path(__file__).parent / "fixtures/papers")))
    manifests = sorted((fixture_root / "manifests").glob("*.json"))
    missing = [
        manifest.name
        for manifest in manifests
        if not (manifest.parent / json.loads(manifest.read_text(encoding="utf-8"))["source_file"]).resolve().is_file()
    ]
    if missing:
        pytest.skip(
            "pinned paper archives are local-only; run "
            "scripts/fetch_paper_fixtures.py before the full corpus gate"
        )
    with tempfile.TemporaryDirectory() as temporary:
        brain = Brain(Path(temporary) / "brain")
        for manifest_path in manifests:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            first = brain.ingest_manifest(manifest_path)
            second = brain.ingest_manifest(manifest_path)
            assert first.document_version_id == second.document_version_id
            assert first.compilation_id == second.compilation_id
            assert first.block_count >= manifest["minimum_blocks"]
            assert sum(block["block_type"] == "equation" for block in brain.store.get_blocks(first.document_id)) >= manifest["minimum_equations"]
            document = brain.get_document(first.document_id)
            assert document["external_ids"]["arxiv"] == manifest["arxiv_id"]
            assert document["versions"][0]["version_label"] == manifest["version"]
            assert document["versions"][0]["resolution_state"] == "resolved"
            from research_brain.parsing import _safe_tex_members
            import hashlib
            members = _safe_tex_members((manifest_path.parent / manifest["source_file"]).read_bytes())
            decoded = {name: data.decode("utf-8", errors="replace") for name, data in members.items()}
            for block in brain.store.get_blocks(first.document_id):
                source = decoded[block["source_member"]]
                span = source[block["char_start"]:block["char_end"]]
                assert span == block["raw_text"]
                assert hashlib.sha256(span.encode()).hexdigest() == block["raw_sha256"]
        report = benchmark_corpus_queries(
            brain, Path(__file__).parents[1] / "evals" / "corpus-query-v1.json",
            iterations=20,
        )
        assert report.passed
        assert report.papers == len(manifests)
        assert report.recall_at_5 == 1.0
