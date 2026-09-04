from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from research_brain import Brain, RetrievalFiltersV1
from research_brain.evaluation import evaluate
from research_brain.ingest import resolve_source
from research_brain.parsing import parse_arxiv_source
from research_brain.schemas import validate_method_cards


PAPER = r"""# Direction Paper

## Method

We compute a paired contrast direction without gradients.

$$
d = h_{positive} - h_{negative}
$$

The method requires activation access but no weight update.
"""


class FakeExtractionProvider:
    def __init__(self, **_: object):
        pass

    def extract(self, *, schema_name: str, evidence: list[dict], **_: object) -> dict:
        if schema_name == "MethodCardV1":
            paragraph = next(item for item in evidence if "paired contrast" in (item.get("raw_text") or ""))
            output = {"cards": [{
                "name": "Paired contrast direction", "problem": "Construct a direction without gradients",
                "mechanism": "Subtract matched activations", "procedure": ["Collect matched activations", "Subtract"],
                "inputs": ["positive and negative activations"], "outputs": ["direction"],
                "gradients_required": False, "training_required": False,
                "activation_access": True, "weight_access": False,
                "assumptions": ["the pair isolates the feature"], "failure_modes": ["confounded pairs"],
                "scientific_moves": ["paired contrast"],
                "evidence": [{"block_id": paragraph["block_id"], "relation": "describes mechanism"}],
            }]}
        else:
            equation = next(item for item in evidence if item.get("raw_latex"))
            context_ids = [item["block_id"] for item in equation["context"]]
            output = {"cards": [{
                "name": "Contrast direction", "equation_block_id": equation["block_id"],
                "semantic_gloss": "A direction is the difference of matched activations.",
                "role": "method definition", "symbols": [
                    {"symbol": "d", "meaning": "direction"}, {"symbol": "h", "meaning": "activation"}],
                "assumptions": ["matched inputs"], "affordances": ["forward-only construction"],
                "failure_modes": ["confounding"], "math_move": "difference vector",
                "context_block_ids": context_ids,
            }]}
        return {"response_id": "resp_test", "output": output, "usage": {"input_tokens": 10, "output_tokens": 5}}


class FakeEmbeddingProvider:
    calls = 0

    def __init__(self, **_: object):
        pass

    def embed(self, texts: list[str]) -> list[list[float]]:
        type(self).calls += 1
        return [[1.0, 0.0] if "paired" in text.lower() or "perturbation" in text.lower() else [0.0, 1.0]
                for text in texts]


class FailingExtractionProvider(FakeExtractionProvider):
    def extract(self, **_: object) -> dict:
        raise RuntimeError("provider failed")


class MilestoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paper = self.root / "paper.md"
        self.paper.write_text(PAPER, encoding="utf-8")
        self.brain = Brain(self.root / "brain", extraction_provider_factory=FakeExtractionProvider,
                           embedding_provider_factory=FakeEmbeddingProvider)
        FakeEmbeddingProvider.calls = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_source_revision_compilation_and_exact_spans(self) -> None:
        first = self.brain.ingest(self.paper)
        second = self.brain.ingest(self.paper)
        self.assertEqual(first.document_version_id, second.document_version_id)
        self.assertEqual(first.compilation_id, second.compilation_id)
        self.assertTrue(first.created_compilation)
        self.assertFalse(second.created_compilation)
        with sqlite3.connect(self.brain.store.path) as connection:
            self.assertEqual(connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM document_compilations").fetchone()[0], 1)
        hit = next(item for item in self.brain.search("paired contrast") if item.record_type == "document_block")
        evidence = self.brain.get_evidence(hit.record_id)
        source = self.paper.read_text(encoding="utf-8")
        self.assertEqual(source[evidence["char_start"]:evidence["char_end"]].strip(), evidence["raw_text"])
        self.assertEqual(len(evidence["raw_sha256"]), 64)

    def test_lexical_search_excludes_superseded_compilations(self) -> None:
        first = self.brain.ingest(self.paper)
        with patch("research_brain.ingest.PARSER_VERSION", "structural-test-upgrade"):
            second = self.brain.ingest(self.paper)
        self.assertNotEqual(first.compilation_id, second.compilation_id)
        hits = [item for item in self.brain.search("paired contrast", limit=20)
                if item.record_type == "document_block"]
        assert hits
        assert {
            self.brain.get_evidence(hit.record_id)["compilation_id"] for hit in hits
        } == {second.compilation_id}

    def test_ingest_transaction_rolls_back_database_rows(self) -> None:
        with patch.object(self.brain.store, "_append_event", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.brain.ingest(self.paper)
        with self.brain.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM documents").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM document_blocks").fetchone()[0], 0)

    def test_tex_only_indexes_reachable_members(self) -> None:
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            files = {
                "main.tex": rb"\documentclass{article}\begin{document}\input{method}\end{document}",
                "method.tex": b"\\section{Used}\nUseful evidence.\n",
                "unused.tex": b"\\section{Unused}\nMust not be indexed.\n",
            }
            for name, contents in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(contents)
                archive.addfile(info, io.BytesIO(contents))
        blocks = parse_arxiv_source(archive_bytes.getvalue())
        self.assertTrue(any("Useful evidence" in item.raw_text for item in blocks))
        self.assertFalse(any("Must not be indexed" in item.raw_text for item in blocks))

    def test_main_tex_detection_ignores_commented_documentclass(self) -> None:
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            files = {
                "preamble.tex": b"% \\documentclass{article}\nNot paper content.\n",
                "paper.tex": rb"\documentclass{article}\begin{document}Actual paper content.\end{document}",
            }
            for name, contents in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(contents)
                archive.addfile(info, io.BytesIO(contents))
        blocks = parse_arxiv_source(archive_bytes.getvalue())
        self.assertTrue(any("Actual paper content" in item.raw_text for item in blocks))
        self.assertFalse(any("Not paper content" in item.raw_text for item in blocks))

    def test_explicit_main_tex_is_authoritative(self) -> None:
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            files = {
                "short.tex": rb"\documentclass{article}\begin{document}Wrong candidate.\end{document}",
                "submission.tex": rb"\documentclass{article}\begin{document}Declared paper.\end{document}",
            }
            for name, contents in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(contents)
                archive.addfile(info, io.BytesIO(contents))
        blocks = parse_arxiv_source(archive_bytes.getvalue(), main_member="submission.tex")
        self.assertTrue(any("Declared paper" in item.raw_text for item in blocks))
        self.assertFalse(any("Wrong candidate" in item.raw_text for item in blocks))

    def test_latest_arxiv_is_resolved_before_source_download(self) -> None:
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            contents = rb"\documentclass{article}\begin{document}Evidence\end{document}"
            info = tarfile.TarInfo("main.tex")
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
        responses = [
            (b"arXiv:2506.24056v1 arXiv:2506.24056v2", "https://arxiv.org/abs/2506.24056", "text/html"),
            (archive_bytes.getvalue(), "https://arxiv.org/src/2506.24056v2", "application/gzip"),
        ]
        with patch("research_brain.ingest._download", side_effect=responses) as download:
            resolved = resolve_source("https://arxiv.org/abs/2506.24056")
        self.assertEqual(download.call_args_list[1].args[0], "https://arxiv.org/src/2506.24056v2")
        self.assertEqual(resolved.version_label, "v2")

    def test_dry_run_is_free_live_cards_are_evidence_constrained_and_reviewed(self) -> None:
        ingested = self.brain.ingest(self.paper)
        dry = self.brain.extract("methods", ingested.document_id)
        self.assertIsNone(dry.run_id)
        with self.brain.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM generation_runs").fetchone()[0], 0)
        result = self.brain.extract("methods", ingested.document_id, live=True)
        self.assertEqual(len(result.object_ids), 1)
        record = self.brain.get_research_object(result.object_ids[0])
        self.assertEqual(record["origin"], "AGENT_EXTRACTED")
        self.assertEqual(record["review_state"], "UNREVIEWED")
        self.assertTrue(record["evidence"])
        self.assertEqual(self.brain.recall("paired contrast", kinds=["method_card"]), [])
        self.assertEqual(self.brain.search("paired contrast", kinds=["method_card"])[0].record_id, record["id"])
        self.brain.review_research_object(record["id"], review_state="ACCEPTED", note="fixture review")
        recalled = self.brain.recall("paired contrast", kinds=["method_card"])
        self.assertEqual(recalled[0].review_state, "ACCEPTED")
        self.assertEqual(self.brain.get_history(record["id"])[-1]["event_type"], "object_reviewed")

    def test_math_card_copies_canonical_latex(self) -> None:
        ingested = self.brain.ingest(self.paper)
        result = self.brain.extract("math", ingested.document_id, live=True)
        record = self.brain.get_research_object(result.object_ids[0])
        equation = next(item for item in record["evidence"] if item["relation"] == "defines")
        self.assertEqual(record["structured"]["exact_latex"], equation["raw_latex"])

    def test_failed_generation_writes_attempt_but_no_cards(self) -> None:
        brain = Brain(self.root / "failed", extraction_provider_factory=FailingExtractionProvider)
        ingested = brain.ingest(self.paper)
        with patch("research_brain.extraction.time.sleep", return_value=None):
            with self.assertRaises(RuntimeError):
                brain.extract("methods", ingested.document_id, live=True)
        with brain.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM generation_attempts").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT count(*) FROM research_objects").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT status FROM generation_runs").fetchone()[0], "failed")

    def test_method_validation_rejects_embedded_hallucinated_block_reference(self) -> None:
        payload = FakeExtractionProvider().extract(
            schema_name="MethodCardV1",
            evidence=[{"block_id": "block_good", "raw_text": "paired contrast"}],
        )["output"]
        payload["cards"][0]["procedure"][0] += " [block_hallucinated?]"
        with self.assertRaisesRegex(ValueError, "unknown evidence"):
            validate_method_cards(payload, {"block_good"})

    def test_method_validation_links_valid_narrative_references(self) -> None:
        payload = FakeExtractionProvider().extract(
            schema_name="MethodCardV1",
            evidence=[{"block_id": "block_good", "raw_text": "paired contrast"}],
        )["output"]
        payload["cards"][0]["procedure"][0] += " [block_context]"
        cards = validate_method_cards(payload, {"block_good", "block_context"})
        assert {ref.block_id for ref in cards[0].evidence} == {"block_good", "block_context"}

    def test_prompt_upgrade_creates_a_distinct_generation_run(self) -> None:
        ingested = self.brain.ingest(self.paper)
        first = self.brain.extract("methods", ingested.document_id, live=True)
        with patch("research_brain.extraction.PROMPT_VERSION", "evidence-cards-next"):
            second = self.brain.extract("methods", ingested.document_id, live=True)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertFalse(second.cached)
        with self.brain.store.connect() as connection:
            versions = {
                row[0] for row in connection.execute(
                    "SELECT prompt_version FROM generation_runs WHERE task='methods'"
                )
            }
        self.assertEqual(versions, {"evidence-cards-v2", "evidence-cards-next"})

    def test_embeddings_hybrid_semantic_recall_and_filters(self) -> None:
        ingested = self.brain.ingest(self.paper)
        extracted = self.brain.extract("methods", ingested.document_id, live=True)
        self.brain.review_research_object(extracted.object_ids[0], review_state="ACCEPTED")
        indexed = self.brain.index_embeddings("all", live=True)
        self.assertGreater(indexed.created_representations, 0)
        repeated = self.brain.index_embeddings("all", live=True)
        self.assertTrue(repeated.cached)
        self.assertEqual(repeated.created_representations, 0)
        hits = self.brain.find_methods(
            "principled perturbation basis", query_vector=[1.0, 0.0],
            filters=RetrievalFiltersV1(gradients_required=False),
        )
        self.assertEqual(hits[0].record_id, extracted.object_ids[0])
        blocked = self.brain.find_methods(
            "principled perturbation basis", query_vector=[1.0, 0.0],
            filters=RetrievalFiltersV1(gradients_required=True),
        )
        self.assertEqual(blocked, [])

    def test_query_embeddings_are_ledgered_cached_and_reusable_offline(self) -> None:
        self.brain.ingest(self.paper)
        self.brain.index_embeddings("all", live=True)
        index_calls = FakeEmbeddingProvider.calls
        first = self.brain.search("mechanism-level paraphrase", semantic_live=True)
        self.assertTrue(first)
        self.assertEqual(FakeEmbeddingProvider.calls, index_calls + 1)
        second = self.brain.search("mechanism-level paraphrase", semantic_live=True)
        offline = self.brain.search("mechanism-level paraphrase")
        self.assertEqual(FakeEmbeddingProvider.calls, index_calls + 1)
        self.assertEqual([hit.record_id for hit in first], [hit.record_id for hit in second])
        self.assertEqual([hit.record_id for hit in first], [hit.record_id for hit in offline])
        with self.brain.store.connect() as connection:
            run = connection.execute(
                "SELECT status FROM generation_runs WHERE task='query_embedding'"
            ).fetchone()
            self.assertEqual(run[0], "complete")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM generation_attempts WHERE run_id IN "
                "(SELECT id FROM generation_runs WHERE task='query_embedding')"
            ).fetchone()[0], 1)

    def test_retrieval_evaluation_thresholds_fail_closed(self) -> None:
        ingested = self.brain.ingest(self.paper)
        extracted = self.brain.extract("methods", ingested.document_id, live=True)
        self.brain.review_research_object(extracted.object_ids[0], review_state="ACCEPTED")
        self.brain.index_embeddings("all", live=True)
        spec = self.root / "evaluation.json"
        spec.write_text(json.dumps({
            "minimum_recall_at_5": 1.0,
            "minimum_semantic_wins_over_lexical": 1,
            "cases": [{
                "name": "semantic-only", "query": "principled perturbation basis",
                "kinds": ["method_card"], "target_title": "Paired contrast direction",
                "must_retrieve": True,
            }],
        }), encoding="utf-8")
        failed = evaluate(self.brain, spec)
        self.assertFalse(failed.passed)
        passed = evaluate(self.brain, spec, semantic_live=True)
        self.assertTrue(passed.passed)
        self.assertEqual(passed.semantic_wins_over_lexical, 1)


if __name__ == "__main__":
    unittest.main()
